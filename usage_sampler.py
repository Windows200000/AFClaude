#!/usr/bin/env python3
"""
Usage data collector for AFClaude (run from cron every 15 min).

Goal: collect enough data to later build a model of expected usage, so the
keep-alive/dispatcher can aim at ~100% weekly usage without getting in the way
of normal work. Everything goes to data/ as JSONL, one row per run.

Per run:
  - fresh /usage (`claude -p --no-session-persistence /usage`, local command,
    no model call): session + weekly %, reset times, raw text (incl. the
    "what's contributing" breakdown). If /usage does not refresh the cache,
    keepalive.refresh_usage_checked retries after one tiny token-refresh request;
    a cache still older than usage_stale.USAGE_STALE_AFTER gives a row marked
    usage.stale = true (kept, but ignored by pacing.py / limit_ratio.py), and
    after STALE_ALERT_AFTER of that, one ALERTS.md entry per episode
  - incremental scan of all transcripts (subagents included) since the last run:
    tokens by model (input / cache write / cache read / output / thinking),
    assistant turns, human prompts, limit hits, active sessions, split into
    "own" (keep-alive / AFClaude / probes) vs "other" (the user's normal work)
  - `claude agents` snapshot (counts by kind/status)
  - weekly cycle bookkeeping: every weekly reset seen is recorded, and `at`
    jobs are scheduled for reset-4min (pre_reset sample = how close the week
    got to 100%) and reset+6min (post_reset sample)
  - at most hourly PER active non-own session: one Haiku call judging that
    session's category (work/school/private-project/quick-question) and load,
    given that session's last 4 human prompts, the tokens generated in each
    gap between them (by model), and its total token usage since the last
    check (by model). A separate, purely algorithmic load estimate (model-
    weighted token volume vs. fixed thresholds) is computed and logged
    alongside Haiku's answer for comparison. Haiku's own cost is recorded too.
  - the AFClaude runs' fill-time rows (run_metrics.py, D-207): data/afclaude_runs.jsonl,
    recomputed until each run's row is final

Times in rows are UTC ISO plus Berlin-local convenience fields.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (usage cache parsing, scrubbed env)
import host  # noqa: E402  (host calls: local on the host, the SSH bridge inside the container)
import limit_ratio  # noqa: E402  (session->weekly ratio snapshot, kept per-sample)
import usage_stale  # noqa: E402  (stale /usage cache: rows marked, consumers ignore them)

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "sampler_state.json")
SAMPLES = os.path.join(DATA, "samples.jsonl")
HAIKU_LOG = os.path.join(DATA, "haiku.jsonl")
CYCLES = os.path.join(DATA, "weekly_cycles.json")
OWN_LIST = os.path.join(DATA, "own_sessions.txt")   # extra UUIDs to exclude, one per line
PROJECTS = os.path.expanduser("~/.claude/projects")

HAIKU_EVERY = timedelta(minutes=55)
RECENT = timedelta(hours=6)          # sessions shown to Haiku
OWN_CWD_MARKERS = ("/work/AFClaude", "AFClaude")
OWN_NAME_RE = re.compile(r"^(ka-|task-manager keep-alive|guard-bypass-test|RC PoC test|"
                         r"limit continue test)", re.I)


def now_utc():
    return datetime.now(UTC)


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def dump(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, default=str)
    os.replace(tmp, path)


def append(path, row):
    with open(path, "a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def local_fields(t):
    lt = t.astimezone(BERLIN)
    return {"berlin": lt.strftime("%Y-%m-%d %H:%M"), "weekday": lt.strftime("%a"), "hour": lt.hour}


# ------------------------------------------------------------------ usage

def usage_now(t=None):
    """/usage (with the shared retry, keepalive.refresh_usage_checked) -> the row's usage dict.
    A cache older than usage_stale.USAGE_STALE_AFTER is kept but marked stale: true."""
    t = t or now_utc()
    res = ka.refresh_usage_checked(cwd=DATA, now=t)
    rc, out = res["rc"], res["out"]
    u = res["usage"] or {}
    row = {"rc": rc, "fetched_at": u.get("fetched_at")}
    for k in ("session", "weekly"):
        if k in u:
            row[k] = u[k]
    row["stale"] = usage_stale.is_stale(u.get("fetched_at"), t)
    if row["stale"] and u.get("fetched_at"):
        row["stale_min"] = round((t - u["fetched_at"]).total_seconds() / 60)
    if res["retried"]:
        row["refresh_retry"] = {"ok": res["ok"], "poke": res["poke"]}
    row["text"] = out.strip()[:3000]
    try:
        cache = host.usage_cache() if host.in_container() else json.load(open(ka.CLAUDE_JSON)).get("cachedUsageUtilization", {})
        raw = cache.get("utilization", {})
        row["limits_raw"] = raw.get("limits")
        row["extra_usage_enabled"] = (raw.get("extra_usage") or {}).get("is_enabled")
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        pass
    return row


# ------------------------------------------------------------------ transcripts

def session_of(path):
    """<proj>/<uuid>.jsonl or <proj>/<uuid>/subagents/<x>.jsonl -> (uuid, is_subagent)"""
    rel = os.path.relpath(path, PROJECTS).split(os.sep)
    if len(rel) == 2:
        return rel[1][:-6], False
    return rel[1], True


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def merge_tok(dst, model, u):
    tk = dst.setdefault(model or "?", {"in": 0, "cache_w": 0, "cache_r": 0, "out": 0, "think": 0})
    tk["in"] += u.get("input_tokens") or 0
    tk["cache_w"] += u.get("cache_creation_input_tokens") or 0
    tk["cache_r"] += u.get("cache_read_input_tokens") or 0
    tk["out"] += u.get("output_tokens") or 0
    tk["think"] += (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0


def model_weight(model):
    """Rough per-model weighting so token volume is comparable across models."""
    m = (model or "").lower()
    if "opus" in m:
        return 3.0
    if "haiku" in m:
        return 0.3
    return 1.0  # sonnet and anything unrecognized


def weighted_tokens(tok_by_model):
    return sum(model_weight(model) * ((tk.get("out") or 0) + (tk.get("think") or 0))
               for model, tk in (tok_by_model or {}).items())


def algo_load(weighted):
    if weighted < 2000:
        return "light"
    if weighted < 15000:
        return "medium"
    return "heavy"


def scan(st, baseline):
    """Read only bytes appended since the last run."""
    offsets = st.setdefault("offsets", {})
    sessions = st.setdefault("sessions", {})   # uuid -> {title, cwd, last_activity, own}
    agg = {}
    seen_ids = set()
    for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        off = offsets.get(path, 0)
        if size < off:
            off = 0
        if size == off:
            continue
        sid, is_sub = session_of(path)
        meta = sessions.setdefault(sid, {})
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(size - off)
        # only consume complete lines
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            continue
        offsets[path] = off + last_nl + 1
        a = agg.setdefault(sid, {"tokens": {}, "assistant_turns": 0, "human_prompts": 0,
                                 "limit_hits": 0, "subagent_turns": 0, "last_ts": None})
        for line in chunk[:last_nl].splitlines():
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            t = e.get("type")
            if t == "custom-title":
                meta["title"] = e.get("customTitle")
            elif t == "agent-name" and not meta.get("title"):
                meta["title"] = e.get("agentName")
            elif t == "ai-title" and not meta.get("title"):
                meta["title"] = e.get("aiTitle")
            if e.get("cwd") and not is_sub:
                meta["cwd"] = e["cwd"]
            ts = e.get("timestamp")
            if ts and (not a["last_ts"] or ts > a["last_ts"]):
                a["last_ts"] = ts
            m = e.get("message") or {}
            if t == "assistant":
                if m.get("model") == "<synthetic>":
                    if e.get("error") == "rate_limit":
                        a["limit_hits"] += 1
                    continue
                mid = m.get("id")
                if mid in seen_ids:     # streamed messages repeat the same id
                    continue
                seen_ids.add(mid)
                a["subagent_turns" if is_sub else "assistant_turns"] += 1
                u = m.get("usage") or {}
                model = m.get("model") or "?"
                merge_tok(a["tokens"], model, u)
                merge_tok(meta.setdefault("pending", {}), model, u)
                merge_tok(meta.setdefault("tokens_since_haiku", {}), model, u)
            elif t == "user" and not is_sub and not e.get("isMeta") and not e.get("toolUseResult"):
                c = m.get("content")
                txt = text_of(c)
                if txt and not txt.lstrip().startswith(("<local-command", "<command-", "<system-reminder", "<task-notification")) \
                        and not (isinstance(c, list) and any(x.get("type") == "tool_result" for x in c if isinstance(x, dict))):
                    a["human_prompts"] += 1
                    prompts = meta.setdefault("prompts", [])
                    gaps = meta.setdefault("gaps", [])
                    if prompts:
                        gaps.append(meta.get("pending", {}))
                    meta["pending"] = {}
                    prompts.append({"ts": ts, "text": txt.strip()[:300]})
                    if len(prompts) > 4:
                        prompts[:] = prompts[-4:]
                    if len(gaps) > 3:
                        gaps[:] = gaps[-3:]
        if a["last_ts"]:
            meta["last_activity"] = a["last_ts"]
    own_extra = set()
    if os.path.exists(OWN_LIST):
        own_extra = {l.strip() for l in open(OWN_LIST) if l.strip()}
    for sid, meta in sessions.items():
        meta["own"] = (sid in own_extra
                       or any(mk in (meta.get("cwd") or "") for mk in OWN_CWD_MARKERS)
                       or bool(OWN_NAME_RE.match(meta.get("title") or "")))
    if baseline:
        return None
    return agg


def summarize(agg, sessions):
    out = {"own": {}, "other": {}}
    for sid, a in agg.items():
        bucket = out["own" if sessions.get(sid, {}).get("own") else "other"]
        bucket.setdefault("sessions_active", 0)
        if a["assistant_turns"] or a["subagent_turns"] or a["human_prompts"]:
            bucket["sessions_active"] += 1
        for k in ("assistant_turns", "subagent_turns", "human_prompts", "limit_hits"):
            bucket[k] = bucket.get(k, 0) + a[k]
        for model, tk in a["tokens"].items():
            bt = bucket.setdefault("tokens", {}).setdefault(model, {})
            for k, v in tk.items():
                bt[k] = bt.get(k, 0) + v
    return out


# ------------------------------------------------------------------ agents

def agents_snapshot():
    try:
        r = host.run_on_host(["claude", "agents", "--json"], timeout=60, env=ka.SCRUBBED_ENV)
        rows = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    snap = {}
    for a in rows:
        key = f"{a.get('kind')}:{a.get('status') or a.get('state')}"
        snap[key] = snap.get(key, 0) + 1
    return snap


# ------------------------------------------------------------------ weekly cycles

def track_cycle(usage, t, tag):
    w = usage.get("weekly") or {}
    if not w.get("resets_at"):
        return
    cycles = load(CYCLES, {})
    # resets_at jitters by milliseconds between fetches -> round to the minute
    r0 = w["resets_at"] + timedelta(seconds=30)
    key = r0.replace(second=0, microsecond=0).isoformat()
    c = cycles.setdefault(key, {"reset_at": key, "first_seen": t.isoformat(), "samples": 0,
                                "max_pct": 0, "scheduled": False})
    c["samples"] += 1
    c["max_pct"] = max(c["max_pct"], w["percent"])
    c["last_pct"], c["last_at"] = w["percent"], t.isoformat()
    if tag == "pre_reset":
        c["pre_reset_pct"], c["pre_reset_at"] = w["percent"], t.isoformat()
    if tag == "post_reset":
        # the previous cycle ended; this sample belongs to the new cycle
        prev = [k for k in cycles if k < key]
        if prev:
            cycles[max(prev)]["post_reset_seen_at"] = t.isoformat()
    if not c["scheduled"]:
        reset = datetime.fromisoformat(key)
        ok = True
        for label, when in (("pre_reset", reset - timedelta(minutes=4)),
                            ("post_reset", reset + timedelta(minutes=6))):
            if when <= t:
                continue
            at_time = when.astimezone(UTC).strftime("%Y%m%d%H%M")
            cmd = f"python3 {os.path.join(HERE, 'usage_sampler.py')} --tag {label} >> {os.path.join(DATA, 'sampler.log')} 2>&1\n"
            r = subprocess.run(["at", "-t", at_time], input=cmd, text=True, capture_output=True,
                               env=dict(ka.SCRUBBED_ENV, TZ="UTC"))
            ok = ok and r.returncode == 0
            c.setdefault("at_jobs", []).append({label: when.isoformat(), "rc": r.returncode,
                                                "out": (r.stderr or r.stdout).strip()[-120:]})
        c["scheduled"] = ok
    dump(CYCLES, cycles)


# ------------------------------------------------------------------ haiku

HAIKU_PROMPT = open(os.path.join(HERE, "prompts", "usage_haiku.md")).read().rstrip("\n")  # default prompt, editable


def fmt_tokens(tok_by_model):
    if not tok_by_model:
        return "(none)"
    parts = []
    for model, tk in tok_by_model.items():
        total = sum(tk.get(k, 0) for k in ("in", "cache_w", "cache_r", "out", "think"))
        parts.append(f"{model}: {total} tok total (out {tk.get('out', 0)}, think {tk.get('think', 0)})")
    return "; ".join(parts)


def haiku_judgement(st, t):
    rows = []
    for sid, meta in st.get("sessions", {}).items():
        if meta.get("own"):
            continue
        la = meta.get("last_activity")
        if not la or t - datetime.fromisoformat(la.replace("Z", "+00:00")) > RECENT:
            continue
        last_call = meta.get("last_haiku_at")
        if last_call and t - datetime.fromisoformat(last_call) < HAIKU_EVERY:
            continue
        prompts = meta.get("prompts") or []
        if not prompts:
            continue  # nothing said in this session yet, nothing to judge
        gaps = meta.get("gaps") or []
        since = meta.get("tokens_since_haiku") or {}
        name = meta.get("title") or f"(untitled {sid[:8]})"
        row = {"at": t.isoformat(), **local_fields(t), "session": sid, "name": name,
               "algo_load": algo_load(weighted_tokens(since)),
               "gap_tokens": gaps, "tokens_since_last": since}
        prompt_txt = "\n".join(f"{i + 1}. {p['text']}" for i, p in enumerate(prompts))
        gaps_txt = "\n".join(f"after msg {i + 1}: {fmt_tokens(g)}" for i, g in enumerate(gaps)) or "(none yet)"
        prompt = HAIKU_PROMPT.format(name=name, n=len(prompts), prompts=prompt_txt,
                                     gaps=gaps_txt, totals=fmt_tokens(since))
        try:
            # prompt via stdin: --tools is variadic and would swallow a positional prompt
            r = host.run_on_host(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                                  "--output-format", "json", "--tools=", "--strict-mcp-config", "--permission-mode", "dontAsk",
                                  "--disable-slash-commands"], input=prompt,
                                 cwd=DATA, env=ka.SCRUBBED_ENV, timeout=180)
            out = json.loads(r.stdout)
            row["cost_usd"] = out.get("total_cost_usd")
            row["usage"] = out.get("usage")
            res = out.get("result", "")
            m = re.search(r"\{.*\}", res, re.S)
            row["answer"] = json.loads(m.group(0)) if m else None
            if not m:
                row["raw"] = res[:500]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
            row["error"] = f"{type(e).__name__}: {e}"[:300]
        meta["last_haiku_at"] = t.isoformat()
        meta["tokens_since_haiku"] = {}
        append(HAIKU_LOG, row)
        rows.append(row)
    if not rows:
        row = {"at": t.isoformat(), **local_fields(t), "skipped": "no non-own session with prompts active in last 6h"}
        append(HAIKU_LOG, row)
        rows.append(row)
    return rows


# ------------------------------------------------------------------ derived (additive) fields

SESSION_LEN = timedelta(hours=5)
SERIES = os.path.join(DATA, "weekly_series.jsonl")   # compact long-term history, never pruned
RESET_JUMP = timedelta(minutes=30)                   # resets_at jitters by ms; a real reset moves it by hours


def limits_kinds(usage):
    """Sorted limit kinds present in this row's /usage data (spots new meters)."""
    return sorted({l["kind"] for l in (usage.get("limits_raw") or []) if isinstance(l, dict) and l.get("kind")})


def token_totals(activity):
    """Scalar token totals for the interval, split own / other (0 when idle)."""
    out = {}
    for who in ("own", "other"):
        toks = ((activity or {}).get(who) or {}).get("tokens") or {}
        out[f"{who}_w_tokens"] = weighted_tokens(toks)
        out[f"{who}_cache_read"] = sum(tk.get("cache_r") or 0 for tk in toks.values())
        out[f"{who}_cache_write"] = sum(tk.get("cache_w") or 0 for tk in toks.values())
    return out


def _dt(x):
    if isinstance(x, datetime):
        return x
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pct_state(usage):
    """Minimal JSON-serialisable snapshot of the meters, kept in sampler state."""
    snap = {}
    for k in ("session", "weekly"):
        m = usage.get(k) or {}
        r = _dt(m.get("resets_at"))
        snap[k] = {"percent": m.get("percent"), "resets_at": r.isoformat() if r else None}
    return snap


def pct_deltas(usage, prev):
    """weekly/session % delta vs the previous sample; None if there is no previous
    or a reset happened in between (resets_at moved, or the percentage went down).
    pct_step: the integer weekly % changed."""
    res = {"weekly_pct_delta": None, "session_pct_delta": None, "pct_step": False}
    cur = pct_state(usage)
    for k in ("weekly", "session"):
        p = (prev or {}).get(k) or {}
        c = cur[k]
        if p.get("percent") is None or c["percent"] is None:
            continue
        pr, cr = _dt(p.get("resets_at")), _dt(c["resets_at"])
        if pr and cr and abs(cr - pr) > RESET_JUMP:
            continue
        if c["percent"] < p["percent"]:
            continue
        res[f"{k}_pct_delta"] = round(c["percent"] - p["percent"], 2)
    if res["weekly_pct_delta"] is not None:
        res["pct_step"] = int(cur["weekly"]["percent"]) != int(prev["weekly"]["percent"])
    return res


def session_start(usage):
    r = _dt((usage.get("session") or {}).get("resets_at"))
    return (r - SESSION_LEN).isoformat() if r else None


def parse_breakdown(text):
    """Numbers out of the /usage 'what is contributing' text; missing -> absent keys."""
    bd = {}
    try:
        num = r"(\d+(?:\.\d+)?)"
        for label, key in (("Last 24h", "24h"), ("Last 7d", "7d")):
            m = re.search(label + r"[^\d\n]*(\d+) requests?[^\d\n]*(\d+) sessions?", text or "")
            if m:
                bd[f"requests_{key}"], bd[f"sessions_{key}"] = int(m.group(1)), int(m.group(2))
            m = re.search(label + r"(.*?)(?=\n\s*\n|\n\s*Last \d|\Z)", text or "", re.S)
            sec = m.group(1) if m else ""
            m = re.search(num + r"% of your usage came from subagent-heavy", sec)
            if m:
                bd[f"subagent_heavy_pct_{key}"] = float(m.group(1))
            m = re.search(num + r"% of your usage was at >(\d+)k context", sec)
            if m:
                bd[f"over_ctx_pct_{key}"] = float(m.group(1))
                bd["over_ctx_threshold_k"] = int(m.group(2))
            m = re.search(num + r"% of your usage came from sessions active for (\d+)\+ hours", sec)
            if m:
                bd[f"long_session_pct_{key}"] = float(m.group(1))
                bd["long_session_hours"] = int(m.group(2))
            m = re.search(r"Top subagents:\s*(.+)", sec)
            if m:
                bd[f"top_subagents_{key}"] = {n.strip(): float(p) for n, p in
                                              re.findall(r"([^,%]+?)\s+(\d+(?:\.\d+)?)%", m.group(1))}
    except Exception:  # never fail a sample over text parsing
        pass
    return bd


def derived_fields(usage, activity, prev):
    """All additive per-sample fields; each guarded so one failure can't sink the row."""
    out = {}
    for name, fn in (("limits_kinds", lambda: {"limits_kinds": limits_kinds(usage)}),
                     ("tokens", lambda: token_totals(activity)),
                     ("deltas", lambda: pct_deltas(usage, prev)),
                     ("session_start", lambda: {"session_start": session_start(usage)}),
                     ("breakdown", lambda: {"breakdown": parse_breakdown(usage.get("text"))})):
        try:
            out.update(fn())
        except Exception:
            pass
    return out


def series_line(row):
    u = row.get("usage") or {}
    w, s = u.get("weekly") or {}, u.get("session") or {}
    line = {"at": row.get("at"), "weekly_pct": w.get("percent"), "weekly_resets_at": w.get("resets_at"),
            "session_pct": s.get("percent"), "session_resets_at": s.get("resets_at"),
            "own_w_tokens": row.get("own_w_tokens"), "other_w_tokens": row.get("other_w_tokens")}
    if row.get("activity"):
        line["human_prompts"] = sum((row["activity"].get(x) or {}).get("human_prompts", 0)
                                    for x in ("own", "other"))
    if usage_stale.row_stale(row):
        line["stale"] = True          # the meters are the frozen cache, not a reading
    return line


SESSION_WINDOWS = os.path.join(DATA, "session_windows.jsonl")  # one row per completed 5-h window, never pruned


def ratio_snapshot(rows, t, windows_path=None):
    """limit_ratio snapshot for this row. First appends every session window
    that has completed by `t` and isn't in data/session_windows.jsonl yet (so a
    window is recorded on the first run after it ends; a missed run catches
    up), then computes with the stored windows. A failure in the window file
    never sinks the sample: the snapshot is then built from `rows` alone."""
    windows_path = windows_path or SESSION_WINDOWS
    stored = None
    try:
        stored, _new = limit_ratio.append_new_windows(rows, path=windows_path, now=t)
    except Exception:   # noqa: BLE001
        stored = None
    return limit_ratio.compute(rows, now=t, windows=stored)


# ------------------------------------------------------------------ stale-usage alert

STALE_ALERT_AFTER = timedelta(hours=3)   # the cache stale this long -> one ALERTS.md entry


def stale_alert(st, usage, t, notify_fn=None):
    """One alert (ALERTS.md, D-049) per stale episode once the usage cache has been stale for
    more than STALE_ALERT_AFTER; the episode is keyed by the frozen fetch time, so the 15-min
    runs don't repeat it. A fresh sample ends the episode. -> True if it alerted."""
    if not usage.get("stale"):
        st.pop("stale_since", None)
        st.pop("stale_alerted", None)
        return False
    since = _dt(st.get("stale_since")) or t
    st["stale_since"] = since.isoformat()
    f = _dt(usage.get("fetched_at"))
    start = min(f, since) if f else since
    key = f.isoformat() if f else "no-cache"
    if st.get("stale_alerted") == key or t - start < STALE_ALERT_AFTER:
        return False
    if notify_fn is None:
        from notify import notify as notify_fn
    hours = (t - start).total_seconds() / 3600
    subject = f"usage data stale for {hours:.0f} h: /usage does not refresh the cache"
    body = (f"`claude -p /usage` has not refreshed ~/.claude.json since "
            f"{f.astimezone(BERLIN).strftime('%a %d.%m. %H:%M') if f else 'n/a'} (Berlin), also not "
            f"after a token-refresh request ({(usage.get('refresh_retry') or {}).get('poke') or 'not tried'}). "
            f"Sampler rows are kept but marked stale (ignored by the forecast and the ratio); autonomous "
            f"starts hold (fail-safe). Last /usage output: {(usage.get('text') or '').strip()[:200]!r}")
    try:
        notify_fn(subject, body)
    except Exception as e:   # noqa: BLE001 - an alert failure must not sink the sample
        print(f"stale alert failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    st["stale_alerted"] = key
    return True


# ------------------------------------------------------------------ AFClaude runs (D-207)

RUNS = os.path.join(DATA, "afclaude_runs.jsonl")      # one row per AFClaude run (run_metrics.py)
RUN_USAGE = os.path.join(DATA, "run_usage.jsonl")     # the watcher's extra readings during runs


def update_runs(t):
    """The fill-time rows of every AFClaude run not final yet, with this sample included
    (run_metrics.update; the first call backfills all past runs). Never sinks the sample."""
    try:
        import run_metrics
        rows = run_metrics.update(now=t, out_path=RUNS, samples_path=SAMPLES, watch_path=RUN_USAGE)
        return len(rows)
    except Exception as e:   # noqa: BLE001 - advisory
        print(f"run metrics failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="cron", help="cron | pre_reset | post_reset | manual")
    ap.add_argument("--no-haiku", action="store_true")
    args = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    try:   # the sampler starts nothing; as a frequent cron job it reports a broken AFClaude DB
        import store   # (alert once per episode) and its recovery (design §7.8, store.db_gate)
        problem = store.db_gate("sampler")
        if problem is not None:
            print(f"database problem ({problem.kind}): {problem}; {problem.escalation}", file=sys.stderr)
    except Exception as e:   # noqa: BLE001 - never sinks the sample
        print(f"database check failed: {type(e).__name__}: {e}", file=sys.stderr)
    t = now_utc()
    st = load(STATE, {})
    baseline = not st.get("offsets")
    usage = usage_now(t)
    stale = bool(usage.get("stale"))
    agg = scan(st, baseline)
    row = {"at": t.isoformat(), **local_fields(t), "tag": args.tag, "baseline": baseline,
           "since": st.get("last_sample_at"), "usage": usage,
           "activity": summarize(agg, st["sessions"]) if agg is not None else None,
           "agents": agents_snapshot()}
    w = usage.get("weekly") or {}
    if w.get("resets_at") and not stale:
        start = w["resets_at"] - ka.WEEK
        row["week_elapsed_frac"] = round((t - start) / ka.WEEK, 4)
        row["projected_linear"] = round(ka.project_weekly(w["percent"], w["resets_at"], t), 1)
    # Cheap: reuse the samples already on disk plus this row, so the ratio
    # snapshot's own history is kept alongside every sample (limit_ratio.py).
    row["limit_ratio"] = ratio_snapshot(limit_ratio.load_samples(SAMPLES) + [row], t)
    # stale meters: no deltas from / against them (a 34 h-old % is no previous reading)
    row.update(derived_fields(usage, row["activity"], None if stale else st.get("prev_pct")))
    try:
        if stale:
            st.pop("prev_pct", None)
        else:
            st["prev_pct"] = pct_state(usage)
    except Exception:
        st.pop("prev_pct", None)
    st["last_sample_at"] = t.isoformat()
    append(SAMPLES, row)
    try:
        append(SERIES, series_line(row))
    except Exception:
        pass
    if not stale:                     # a frozen % would fake the cycle's max / pre-reset value
        track_cycle(usage, t, args.tag)
    update_runs(t)
    stale_alert(st, usage, t)
    if not args.no_haiku and args.tag == "cron":
        haiku_judgement(st, t)
    dump(STATE, st)
    print(f"{t.isoformat()} tag={args.tag} baseline={baseline} weekly={w.get('percent')} "
          f"session={(usage.get('session') or {}).get('percent')}")


if __name__ == "__main__":
    main()
