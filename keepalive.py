#!/usr/bin/env python3
"""
Keep-alive for the AFClaude task-manager session (no dashboard).

Runs begin only at STARTS (D-205): each session-window start of the night window (cron
--window-start, 23:00 and 04:00 Berlin for 23:00 x 10 h, D-202/D-203; a start postponed for
an active user is re-decided by the watcher before the next start, D-018) and each last-stretch
slot start (D-020). A start that passes the budget rule continues the task-manager session with

    ka_resume.sh --session <full-uuid> --message "<continue msg>"   (tmux, no --bg)

(a non-bg resume continues under the same ID and reconnects Remote Control; it
would only fork if another live process held the session, which preflight refuses).
Today only AFClaude has a task-manager, so a start = this session (picking the project by
rank/priority among several task-managers is design phase 3b).

No continue after a limit (D-204): when the session stops on the synthetic "You've hit your
... limit" notice, the run is done. The watcher only detects and logs it (once per stall: a
PROGRESS note); it never continues the session at the limit reset. The next session-window
start (or the next last-stretch slot start) decides by its own check; that start may well
find the previous run stopped at a limit and continue it (e.g. limit hit before 04:00, next
run at 04:00). Approved stalled sessions of the owner are the dispatcher's job (D-206).

Budget rule: by default pacing.py (the original linear rule evolved into a night gate: a
night runs a FULL session window only if the week is then predicted to end at or below the
reserve threshold (default: one session window left), w + session cost + the user's forecast
use until the weekly reset; re-checked per session window; the final <= 2 session windows
before the weekly reset fill to 100%; AFClaude yields to an active user by POSTPONING to last
activity + 60 min).
data/afclaude.json "usage_model": "linear" selects the original linear rule, which is also
the fallback if pacing.py fails:
  projected end-of-week usage < 90%                          -> continue
  else weekly reset <= 11:00 Berlin after the current window -> continue
  else                                                       -> hold back
Forecast (my choice): linear extrapolation of the weekly % over the elapsed
part of the week, with elapsed floored at 24h so an early-week burst doesn't
explode the projection.

Default is DRY-RUN (decides and logs "WOULD FIRE", never resumes anything).
Pass --arm to actually fire. Create a file named STOP next to this script to
make a running watcher exit.

All log times are Europe/Berlin.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import afclaude_config
import usage_stale
import host  # host calls: local subprocess on the host, the SSH bridge inside the container

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.environ.get("KEEPALIVE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
JOBS_DIR = os.path.expanduser("~/.claude/jobs")
# Per-process registry every live claude writes (<pid>.json: pid, sessionId, procStart,
# kind, entrypoint, ...). Both paths are injectable so tests can use a fake /proc.
SESSIONS_DIR = os.environ.get("KEEPALIVE_SESSIONS_DIR", os.path.expanduser("~/.claude/sessions"))
PROC_DIR = os.environ.get("KEEPALIVE_PROC_DIR", "/proc")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
STATE_DIR = os.environ.get("KEEPALIVE_STATE_DIR", HERE)
STATE_FILE = os.path.join(STATE_DIR, "keepalive_state.json")
LOCK_FILE = os.path.join(STATE_DIR, "keepalive.lock")
STOP_FILE = os.path.join(STATE_DIR, "STOP")
PROGRESS_FILE = os.environ.get("KEEPALIVE_PROGRESS_FILE", os.path.join(HERE, "PROGRESS.md"))
KA_RESUME = os.environ.get("KEEPALIVE_KA_RESUME", os.path.join(HERE, "ka_resume.sh"))  # tests use a stub

# Automation window, Europe/Berlin wall clock: default 23:00-09:00 (spans midnight), from
# data/afclaude.json window_start / window_hours (afclaude_config.window()). The watcher
# re-reads it every loop iteration (reload_window), cron runs read it at start.
WINDOW_START, WINDOW_END = afclaude_config.window()   # END exclusive; END < START = spans midnight
WEEKLY_CUTOFF = dtime(11, 0)        # "reset no later than 11:00 after the window" (on the window-end day)
PROJECTION_THRESHOLD = 90.0         # percent
WEEK = timedelta(days=7)
MIN_ELAPSED = timedelta(hours=24)   # forecast floor

POLL_SECONDS = 30
RESET_GRACE = timedelta(seconds=90)       # fire this long after the reset time
USAGE_MAX_AGE = timedelta(minutes=10)     # refresh /usage if cache older than this
REFRESH_OK_AGE = timedelta(minutes=2)     # /usage left the cache this young: OK (claude skips the rewrite < 60 s)
POKE_MIN_INTERVAL = timedelta(minutes=30) # at most one token-refresh request per this (sampler + watcher + cron)
USAGE_ALERT_AFTER = 2                     # failed usage checks in a row at one window start -> one ALERT
USAGE_STATE_FILE = os.path.join(STATE_DIR, "usage_refresh_state.json")
HOLD_RECHECK = timedelta(minutes=15)      # re-evaluate a HOLD decision this often
MAX_FIRES_PER_WINDOW = 4                  # safety cap per night
VERIFY_TIMEOUT = timedelta(minutes=10)

IGNORE_WINDOW = False  # --now: skip the window-start-hour gate (budget rule still applies)
BACKLOG_FILE = os.path.join(HERE, "BACKLOG.md")
TAKE_OVER_IDLE = False  # opt-in: SIGTERM an idle interactive holder (e.g. an open terminal)
# Core AFClaude sessions always run on Opus 5.5 with high effort (user rule, 2026-09-26);
# subagents they spawn may use other models.
LAUNCH = {"model": "claude-opus-5-5", "effort": "high", "name": "task-manager keep-alive build"}

SCRUBBED_ENV = {
    "HOME": os.path.expanduser("~"),
    "USER": "opc",
    "LOGNAME": "opc",
    "SHELL": "/bin/bash",
    "PATH": "/home/opc/.local/bin:/home/opc/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin",
    "LANG": "C.UTF-8",
    "TERM": "xterm-256color",
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
}

def load_prompt(name):
    """prompts/<name>.md, the default prompts (the dashboard will surface/edit these)."""
    with open(os.path.join(HERE, "prompts", name + ".md")) as fh:
        return fh.read()


def session_message(name, afclaude=True, manager=True, **fields):
    """One-line message for a session: <name>.md + guard_respect.md + manager.md
    + manager_afclaude.md (the AFClaude-repo rules). afclaude=False leaves those
    rules out for sessions working elsewhere; manager=False leaves out both
    manager files (the user's own sessions get a neutral message). One line
    because tmux send-keys would submit at the first newline."""
    parts = [load_prompt(name).format(**fields), load_prompt("guard_respect")]
    if manager:
        parts.append(load_prompt("manager"))
        if afclaude:
            parts.append(load_prompt("manager_afclaude"))
    return " ".join(" ".join(parts).split())


LIMIT_TEXT_RE = re.compile(r"hit your (?P<kind>[\w ]*?)\s*limit", re.I)
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[^)]+)\)",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def berlin(dt):
    return dt.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    print(f"[{datetime.now(BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# ---------------------------------------------------------------- transcript

def transcript_path(session_id):
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def iter_entries(path):
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written


def message_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def last_message(path):
    """Last user/assistant entry. Metadata lines (system, last-prompt,
    cost-state, atis-latch, ...) routinely FOLLOW a limit notice, so the
    literal last line is not good enough."""
    last = None
    for e in iter_entries(path):
        if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
            last = e
    return last


def stall_info(entry):
    """Return a dict if `entry` is a synthetic usage-limit notice, else None."""
    if not entry or entry.get("type") != "assistant":
        return None
    msg = entry.get("message") or {}
    if msg.get("model") != "<synthetic>":
        return None
    text = message_text(entry)
    m = LIMIT_TEXT_RE.search(text)
    if not (entry.get("error") == "rate_limit" or m):
        return None
    kind_word = (m.group("kind") if m else "").lower()
    kind = "weekly" if "week" in kind_word else "session" if "session" in kind_word else kind_word or "unknown"
    ts = parse_ts(entry.get("timestamp"))
    return {
        "uuid": entry.get("uuid"),
        "timestamp": ts,
        "text": text,
        "kind": kind,
        "reset_from_text": parse_reset_text(text, ts or datetime.now(UTC)),
    }


def parse_reset_text(text, ref):
    """'resets 5pm (UTC)', 'resets 9:30am (UTC)', 'resets Oct 1, 4:59pm (UTC)'.
    Without a date: the first such clock time AFTER `ref` (the notice's own
    timestamp, not "now", so a watcher started late still gets it right)."""
    m = RESET_RE.search(text or "")
    if not m:
        return None
    tzname = m.group("tz").strip()
    try:
        tz = UTC if tzname.upper() == "UTC" else ZoneInfo(tzname)
    except Exception:
        return None
    h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    mi = int(m.group("m") or 0)
    ref_local = ref.astimezone(tz)
    if m.group("mon"):
        mon = MONTHS.get(m.group("mon").lower())
        if not mon:
            return None
        cand = datetime(ref_local.year, mon, int(m.group("day")), h, mi, tzinfo=tz)
        if cand < ref_local - timedelta(days=180):   # year wrap
            cand = cand.replace(year=cand.year + 1)
        return cand.astimezone(UTC)
    cand = ref_local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if cand <= ref_local:
        cand += timedelta(days=1)
    return cand.astimezone(UTC)


# ---------------------------------------------------------------- window

def reload_window():
    """Re-read the window from data/afclaude.json (the dashboard will edit it later)."""
    global WINDOW_START, WINDOW_END
    WINDOW_START, WINDOW_END = afclaude_config.window()


def in_window(now):
    t = now.astimezone(BERLIN).time()
    if WINDOW_START <= WINDOW_END:            # window within one calendar day
        return WINDOW_START <= t < WINDOW_END
    return t >= WINDOW_START or t < WINDOW_END   # window spans midnight


def current_window_end(now):
    """WINDOW_END (Berlin) of the window `now` is in, or of the next window.
    Its date names the window ("night"): fire caps and window-start dedup keys use it."""
    local = now.astimezone(BERLIN)
    d = local.date() if local.time() < WINDOW_END else local.date() + timedelta(days=1)
    return datetime.combine(d, WINDOW_END, tzinfo=BERLIN)


def next_window_start(now):
    """`now` if inside the window, else the next WINDOW_START (Berlin)."""
    if in_window(now):
        return now
    local = now.astimezone(BERLIN)
    start = datetime.combine(local.date(), WINDOW_START, tzinfo=BERLIN)
    return start if start > local else start + timedelta(days=1)


def _win():
    return (WINDOW_START, WINDOW_END)


def session_window_starts(now):
    """The session-window starts (D-202) of the night window `now` is in, else of the latest one
    (Berlin wall clock): window start + k x 5 h while the session window ends by the window
    end, i.e. 23:00 and 04:00 for the default 23:00 x 10 h (pacing.session_starts)."""
    import pacing
    return pacing.session_starts(pacing.latest_window_start(now, _win()), _win())


def session_start_at(now):
    """The session-window start whose hour `now` is in (the cron runs --window-start at :00 of
    each start's hour), else None."""
    if not in_window(now):
        return None
    return next((s for s in session_window_starts(now) if s <= now < s + timedelta(hours=1)), None)


def is_window_start_hour(now):
    """--window-start acts only in the Berlin hour of a session-window start (D-202: 23:xx and
    04:xx by default). Cron fires it at `0 2,3,21,22 * * *` (UTC); exactly one of 21/22 is 23:xx
    Berlin (21:00 in CEST, 22:00 in CET) and one of 2/3 is 04:xx (02:00 in CEST, 03:00 in CET),
    also on the DST nights. A different window_start needs different cron hours."""
    return session_start_at(now) is not None


def latest_session_start(now):
    """The latest session-window start <= now inside the window, else None."""
    import pacing
    return pacing.latest_session_start(now, _win())


def next_session_start(now):
    """The first session-window start after `now`."""
    import pacing
    return pacing.next_session_start(now, _win())


def window_start_key(now):
    """Dedup key of the window-start continue, one per SESSION-WINDOW START (D-202): the
    window's END date (its "night", as the fire cap uses), so the 23:00 start on 29.09. is
    window-start-2026-09-30 (the old once-per-night key) and the 04:00 start the night's
    second session window, window-start-2026-09-30-s2. Outside the window: the next night's
    first start."""
    s = latest_session_start(now)
    k = session_window_starts(now).index(s) if s is not None else 0
    return f"window-start-{current_window_end(now).date()}" + (f"-s{k + 1}" if k else "")


# ---------------------------------------------------------------- usage

def read_usage_cache():
    """Parse ~/.claude.json cachedUsageUtilization -> dict or None."""
    try:
        if host.in_container():
            c = host.usage_cache()
        else:
            with open(CLAUDE_JSON) as fh:
                c = json.load(fh).get("cachedUsageUtilization") or {}
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    if not c.get("fetchedAtMs"):
        return None
    out = {"fetched_at": datetime.fromtimestamp(c["fetchedAtMs"] / 1000, UTC)}
    for lim in (c.get("utilization") or {}).get("limits") or []:
        key = {"session": "session", "weekly_all": "weekly"}.get(lim.get("kind"))
        if key:
            out[key] = {"percent": float(lim.get("percent") or 0),
                        "resets_at": parse_ts(lim.get("resets_at"))}
    util = (c.get("utilization") or {})
    for key, src in (("session", "five_hour"), ("weekly", "seven_day")):
        if key not in out and util.get(src):
            out[key] = {"percent": float(util[src].get("utilization") or 0),
                        "resets_at": parse_ts(util[src].get("resets_at"))}
    return out


def refresh_usage(cwd=HERE):
    """`claude -p /usage` is a local slash command (no model call); it prints
    fresh numbers and rewrites the ~/.claude.json cache. Run it scrubbed, with
    no session persistence so it leaves no transcript behind."""
    try:
        r = host.run_on_host(["claude", "-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"],
                             cwd=cwd, env=SCRUBBED_ENV, timeout=120)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


# A minimal real model request (Haiku, no tools, no session file). Its argv must stay one of the
# shapes docker/host_exec.py whitelists for `claude -p --model haiku` (prompt on stdin).
POKE_ARGV = ["claude", "-p", "--model", "haiku", "--no-session-persistence", "--output-format", "json",
             "--tools=", "--strict-mcp-config", "--permission-mode", "dontAsk", "--disable-slash-commands"]
POKE_PROMPT = "Reply with the single word OK."
LAST_REFRESH = {}   # the latest refresh_usage_checked() result (for log lines and alerts)


def _load_usage_state():
    try:
        with open(USAGE_STATE_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_usage_state(d):
    try:
        tmp = USAGE_STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1, default=str)
        os.replace(tmp, USAGE_STATE_FILE)
    except OSError as e:
        log(f"usage state not saved: {e}")


def poke_auth(cwd=HERE, now=None):
    """One minimal real model request, to renew the OAuth access token. `-p /usage` is a local
    command: on 03./04.10.2026 it printed only the cost summary and refreshed nothing for
    34 h, until the first real request renewed the token (see usage_stale.py). Throttled to
    one per POKE_MIN_INTERVAL across all callers. -> short result text."""
    now = now or datetime.now(UTC)
    st = _load_usage_state()
    last = usage_stale.ts(st.get("poke_at"))
    if last and timedelta(0) <= now - last < POKE_MIN_INTERVAL:
        return f"skipped (last token-refresh request {berlin(last)})"
    st["poke_at"] = now.isoformat()
    _save_usage_state(st)
    try:
        r = host.run_on_host(POKE_ARGV, input=POKE_PROMPT, cwd=cwd, env=SCRUBBED_ENV, timeout=180)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return f"failed: {type(e).__name__}: {e}"[:200]
    try:
        o = json.loads(r.stdout or "{}")
        res = f"is_error={o.get('is_error')} result={str(o.get('result'))[:60]!r}"
    except ValueError:
        res = f"output {(r.stdout or r.stderr or '').strip()[:80]!r}"
    return f"rc={r.returncode} {res}"


def _refreshed(u, before, now):
    """/usage did its job: the cache's fetch time moved on, or it is very young (claude does not
    rewrite a cache younger than 60 s, so 'unchanged' alone is no failure)."""
    if not u:
        return False
    f = u["fetched_at"]
    return (before is not None and f > before) or timedelta(0) <= now - f <= REFRESH_OK_AGE


def refresh_usage_checked(cwd=HERE, now=None):
    """The ONE usage refresh with retry, used by the sampler and the watcher / window-start paths:
    /usage; if the cache did not refresh, one cheap real request (poke_auth: renews an expired
    OAuth token) and /usage again. -> dict rc, out (last /usage output), usage (cache after,
    or None), ok (refreshed), retried, poke (poke_auth result or None)."""
    now = now or datetime.now(UTC)
    before = (read_usage_cache() or {}).get("fetched_at")
    rc, out = refresh_usage(cwd)
    u = read_usage_cache()
    res = {"rc": rc, "out": out, "usage": u, "ok": _refreshed(u, before, now), "retried": False, "poke": None}
    if not res["ok"]:
        log(f"/usage did not refresh the cache (rc={rc}, cache from "
            f"{berlin(u['fetched_at']) if u else 'n/a'}): {out.strip()[:120]!r}; trying a token-refresh request")
        res["poke"] = poke_auth(cwd, now)
        res["retried"] = True
        log(f"token-refresh request: {res['poke']}")
        rc, out = refresh_usage(cwd)
        u = read_usage_cache()
        res.update(rc=rc, out=out, usage=u, ok=_refreshed(u, before, max(now, datetime.now(UTC))))
    LAST_REFRESH.clear()
    LAST_REFRESH.update(res, at=now)
    return res


def fresh_usage(now, force=False):
    u = read_usage_cache()
    if force or not u or now - u["fetched_at"] > USAGE_MAX_AGE:
        r = refresh_usage_checked(now=now)
        u = r["usage"]
        if not u or now - u["fetched_at"] > USAGE_MAX_AGE:
            log(f"usage refresh failed or cache still stale (rc={r['rc']}, retried={r['retried']}): "
                f"{r['out'].strip()[:200]!r}")
            return None
    return u


def note_window_usage(key, ok, now, final=False):
    """Window-start usage bookkeeping: count failed (unknown / stale) usage checks in a row per
    window-start key; after USAGE_ALERT_AFTER of them append ONE alert to ALERTS.md (D-048 /
    D-049), not one every 15 min; at once if `final` (no recheck follows tonight). A good check
    resets the count. The fail-safe itself is unchanged: unknown usage never starts work.
    -> True if it alerted."""
    st = _load_usage_state()
    ent = (st.get("window_start") or {}).get(key) or {"fails": 0, "alerted": False}
    ent["fails"] = 0 if ok else ent.get("fails", 0) + 1
    alerted = False
    if not ok and (ent["fails"] >= USAGE_ALERT_AFTER or final) and not ent.get("alerted"):
        cache = (LAST_REFRESH.get("usage") or read_usage_cache() or {}).get("fetched_at")
        alert(f"weekly usage unknown at the window start ({ent['fails']} checks in a row): "
              "no autonomous start (fail-safe)",
              f"`claude -p /usage` did not refresh ~/.claude.json (cache from "
              f"{berlin(cache) if cache else 'n/a'}), also not after a token-refresh request "
              f"({LAST_REFRESH.get('poke') or 'not tried'}). Last /usage output: "
              f"{(LAST_REFRESH.get('out') or '').strip()[:300]!r}. The watcher rechecks every 15 min; "
              "a real Claude request (or `claude /login`) on the host usually fixes it.")
        ent["alerted"] = alerted = True
    st["window_start"] = {key: ent}      # older nights are over
    _save_usage_state(st)
    return alerted


# ---------------------------------------------------------------- budget rule

def project_weekly(pct, resets_at, now):
    week_start = resets_at - WEEK
    elapsed = max(now - week_start, MIN_ELAPSED)
    remaining = max(resets_at - now, timedelta(0))
    return pct + pct * (remaining / elapsed)


def _budget_model():
    """pacing.py (forecast-driven pacing) unless data/afclaude.json selects "linear"."""
    if afclaude_config.usage_model() == "linear":
        return None
    import pacing
    return pacing


def _ratio_text(extra, ratio):
    return f" ≈ {extra / ratio:.0f}% of a session window" if ratio and extra is not None else ""


BUDGET_ADVICE = "; check usage_report.py / keepalive.py --decide and stop before exceeding it"


def budget_eval(usage, now):
    """THE budget evaluation every caller uses (watcher, window-start, last mile, deferred
    window-start, --decide, --work-on, the dispatcher gate, the quickview), so they all show
    the same number. -> dict(go, reason, headroom, text, postpone, recheck_at, last_mile,
    session_cap). Model: afclaude_config.usage_model(); any failure of pacing.py -> the
    original linear rule (which HOLDs inside the last mile: it cannot see the user)."""
    try:
        m = _budget_model()
        if m:
            d = m.decide(usage, now)
            pct = ((usage or {}).get("weekly") or {}).get("percent")
            text = m.budget_text(d, pct)
            if d["headroom"] is not None:
                text += BUDGET_ADVICE
            return dict(d, text=text, last_mile=d.get("mode") == "last_mile")
    except Exception as e:   # noqa: BLE001 - any model failure falls back to the linear rule
        log(f"budget model failed ({type(e).__name__}: {e}); falling back to the linear rule")
        return fallback_eval(usage, now)
    return linear_eval(usage, now)


def budget_decision(usage, now):
    """-> (go: bool, reason: str), from budget_eval()."""
    d = budget_eval(usage, now)
    return d["go"], d["reason"]


def budget_headroom(usage, now):
    """How much more weekly % this run may use -> (extra_weekly_pct | None, text), from
    budget_eval(): the same number as in the decision reason."""
    d = budget_eval(usage, now)
    return d["headroom"], d["text"]


def _limit_ratio():
    try:
        import limit_ratio
        r = limit_ratio.compute(limit_ratio.load_samples(limit_ratio.SAMPLES), now=datetime.now(UTC))
        return limit_ratio.preferred_ratio(r).get("value")   # per-window estimate when available
    except Exception:   # noqa: BLE001 - the ratio text is advisory
        return None


def linear_eval(usage, now, reason_suffix=""):
    go, reason = linear_budget_decision(usage, now)
    extra, text = linear_budget_headroom(usage, now)
    if extra is not None:
        reason += f"; budget for this run +{extra:.1f}%"
        text += _ratio_text(extra, _limit_ratio()) + BUDGET_ADVICE
    lm = _fallback_last_mile(usage, now)
    return {"go": go, "reason": reason + reason_suffix, "headroom": extra, "text": text, "postpone": False,
            "recheck_at": None, "last_mile": lm, "session_cap": 100.0 if lm else 85.0, "mode": "linear"}


def fallback_eval(usage, now):
    """Linear rule after a budget-model failure. The linear rule cannot see the user, so the
    last mile (where it would spend up to 100%) holds instead of continuing."""
    if _fallback_last_mile(usage, now):
        return {"go": False, "reason": "HOLD: budget model failed and the linear fallback does not spend the "
                                       "last mile; budget for this run +0.0%",
                "headroom": 0.0, "text": "budget for this run: none (budget model failed; the last mile is not "
                                         "spent)", "postpone": False, "recheck_at": None, "last_mile": True,
                "session_cap": 85.0, "mode": "fallback"}
    return linear_eval(usage, now, " [linear fallback: budget model failed]")


def last_mile_hours(weekly_pct, resets_at=None, now=None):
    """Last-stretch length in hours: data/afclaude.json last_mile_hours, by default "auto" =
    min(ceil(session windows of quota left), 2) x session length (pacing.py)."""
    setting = afclaude_config.last_mile_setting()
    if setting != "auto":
        return setting
    import pacing
    if weekly_pct is None:
        return pacing.SESSION_H
    ratio, _ = pacing.ratio_info()
    return pacing.last_mile_hours(weekly_pct, ratio, "auto")


def last_mile_left(resets_at, now, weekly_pct=None):
    """Time left until the weekly reset if `now` is inside the last-mile period, else None."""
    if not resets_at:
        return None
    lm = timedelta(hours=last_mile_hours(weekly_pct, resets_at, now))
    if lm <= timedelta(0):
        return None
    left = resets_at.astimezone(UTC) - now.astimezone(UTC)   # UTC: DST-safe
    return left if timedelta(0) < left <= lm else None


def in_last_mile(now, usage=None):
    """Last-mile check from given usage or the cached usage (no refresh)."""
    w = (usage or read_usage_cache() or {}).get("weekly") or {}
    return last_mile_left(w.get("resets_at"), now, w.get("percent")) is not None


def _fallback_last_mile(usage, now):
    try:
        return in_last_mile(now, usage or {"weekly": {}})
    except Exception:   # noqa: BLE001 - unknown: assume the last mile (the safe side)
        return True


def fallback_budget_decision(usage, now):
    d = fallback_eval(usage, now)
    return d["go"], d["reason"]


def linear_budget_headroom(usage, now):
    """Linear rule: how much more weekly % can be used before the projection reaches
    PROJECTION_THRESHOLD (in the last mile: up to 100%). -> (extra | None, text)."""
    w = (usage or {}).get("weekly") or {}
    if w.get("percent") is None or not w.get("resets_at"):
        return None, "budget unknown"
    pct, reset = float(w["percent"]), w["resets_at"]
    if last_mile_left(reset, now, pct) is not None:
        extra, target = max(100.0 - pct, 0.0), "100% (last mile)"
    else:
        elapsed = max(now - (reset - WEEK), MIN_ELAPSED)
        remaining = max(reset - now, timedelta(0))
        extra = max(PROJECTION_THRESHOLD * elapsed / (elapsed + remaining) - pct, 0.0)
        target = f"a projected {PROJECTION_THRESHOLD:.0f}%"
    return extra, f"budget for this run: about +{extra:.1f} weekly % (now {pct:.0f}%) before reaching {target}"


def linear_budget_decision(usage, now):
    """The linear rule (projection < 90%, 11:00 reset cutoff, last mile) -> (go, reason)."""
    if not usage or "weekly" not in usage or not usage["weekly"]["resets_at"]:
        return False, "HOLD: weekly usage unknown (fail-safe)"
    w = usage["weekly"]
    if w["percent"] >= 100 and w["resets_at"] > now:
        return False, f"HOLD: weekly limit exhausted until {berlin(w['resets_at'])}"
    left = last_mile_left(w["resets_at"], now, w["percent"])
    if left is not None:
        # last-mile rule (owner, 30.09.): the remaining quota would expire unused
        return True, (f"CONTINUE: last mile, weekly reset in {int(left.total_seconds() // 3600)}h"
                      f"{int(left.total_seconds() % 3600 // 60):02d}m ({w['percent']:.0f}% used): using the remaining quota")
    proj = project_weekly(w["percent"], w["resets_at"], now)
    base = f"week {w['percent']:.0f}% used, projected {proj:.0f}% at reset {berlin(w['resets_at'])}"
    if proj < PROJECTION_THRESHOLD:
        return True, f"CONTINUE: {base} < {PROJECTION_THRESHOLD:.0f}%"
    cutoff = datetime.combine(current_window_end(now).date(), WEEKLY_CUTOFF, tzinfo=BERLIN)
    if w["resets_at"] <= cutoff:
        return True, f"CONTINUE: {base} >= {PROJECTION_THRESHOLD:.0f}%, but weekly reset <= {berlin(cutoff)}"
    return False, f"HOLD: {base} >= {PROJECTION_THRESHOLD:.0f}% and weekly reset after {berlin(cutoff)}"


# ---------------------------------------------------------------- session / firing

def agent_entries(session_id):
    """All `claude agents` rows for this session (a session can show up twice,
    e.g. a dead bg row plus a live interactive one)."""
    try:
        r = host.run_on_host(["claude", "agents", "--json", "--all"], timeout=60, env=SCRUBBED_ENV)
        return [a for a in json.loads(r.stdout or "[]") if a.get("sessionId") == session_id]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def job_state(session_id):
    try:
        with open(os.path.join(JOBS_DIR, session_id[:8], "state.json")) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def tmux_name(session_id):
    return f"ka-{session_id[:8]}"


def tmux_alive(session_id):
    return host.run_on_host(["tmux", "has-session", "-t", "=" + tmux_name(session_id)],
                            timeout=30).returncode == 0


def tmux_pids(session_id):
    """Pane pids of our own tmux session ka-<id8> (empty if none)."""
    try:
        r = host.run_on_host(["tmux", "list-panes", "-s", "-t", "=" + tmux_name(session_id), "-F", "#{pane_pid}"],
                             timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(x) for x in r.stdout.split() if x.isdigit()} if r.returncode == 0 else set()


def pid_alive(pid):
    if host.in_container():   # the container's /proc is not the host's
        return bool(pid) and host.proc_info(pid) is not None
    return bool(pid) and os.path.exists(os.path.join(PROC_DIR, str(pid)))


ARCHIVED_MARK = "this session was ended or archived from another device"
RC_HELD = "held by a `claude rc` server"
OTHER_HELD = "held by another live process"


def proc_argv(pid):
    if host.in_container():   # argv of claude / keepalive.py processes only
        return list((host.proc_info(pid) or {}).get("argv") or [])
    try:
        with open(os.path.join(PROC_DIR, str(int(pid)), "cmdline"), "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except (OSError, ValueError):
        return []


def _proc_stat(pid):
    """Fields after the `(comm)` of /proc/<pid>/stat: [0]=state, [1]=ppid, [19]=starttime."""
    try:
        with open(os.path.join(PROC_DIR, str(int(pid)), "stat")) as fh:
            return fh.read().rsplit(")", 1)[1].split()
    except (OSError, ValueError, IndexError):
        return []


def proc_ppid(pid):
    if host.in_container():
        return (host.proc_info(pid) or {}).get("ppid")
    try:
        return int(_proc_stat(pid)[1])
    except (ValueError, IndexError):
        return None


def proc_starttime(pid):
    if host.in_container():
        return (host.proc_info(pid) or {}).get("start")
    try:
        return _proc_stat(pid)[19]
    except IndexError:
        return None


def session_entry(pid):
    """The ~/.claude/sessions/<pid>.json registry entry of a process, or {}."""
    try:
        with open(os.path.join(SESSIONS_DIR, f"{int(pid)}.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def registry_holders(session_id):
    """Live pids whose ~/.claude/sessions/<pid>.json holds `session_id`. This sees
    holders `claude agents --json` may not list. An entry whose procStart does
    not match the live process's start time is a stale file of a reused pid."""
    out = set()
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path) as fh:
                d = json.load(fh)
            pid = int(d.get("pid") or os.path.basename(path)[:-5])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if d.get("sessionId") != session_id or not pid_alive(pid):
            continue
        start = d.get("procStart")
        if start is not None and proc_starttime(pid) not in (None, str(start)):
            continue
        out.add(pid)
    return out


def is_ours(pid, ours, depth=32):
    """True if `pid` is one of the pane pids `ours` (our tmux ka-<id8>) or runs under one."""
    while pid and depth > 0:
        if pid in ours:
            return True
        pid, depth = proc_ppid(pid), depth - 1
    return False


def is_rc_server(argv):
    """`claude rc` / `claude remote-control`: the server whose per-thread children
    (`--print --sdk-url ...`) hold the user's Remote Control threads."""
    args = argv[1:]
    return bool(args) and args[0] in ("rc", "remote-control") and "--print" not in args


def is_sdk_child(argv):
    """An rc-server thread child: `.../versions/<v> --print --sdk-url https://.../code/sessions/cse_...`."""
    return any(a == "--sdk-url" or a.startswith("--sdk-url=") for a in argv[1:])


def rc_held(pid):
    """True if `pid` is a `claude rc` server or one of its per-thread children:
    the server itself, anything with `--sdk-url` in its argv, anything whose
    registry entry says entrypoint "sdk-cli", or a child of an rc server.
    Taking such a holder over + resuming does NOT move the thread: killing the
    child does not free it either (the rc server re-serves the thread on the next
    app message and rebuilds the local transcript), so any take-over + resume
    makes a second RC thread with the same uuid (two writers on one transcript;
    live tests 29.09.). So never take it over."""
    argv = proc_argv(pid)
    if is_rc_server(argv) or is_sdk_child(argv) or session_entry(pid).get("entrypoint") == "sdk-cli":
        return True
    ppid = proc_ppid(pid)
    return bool(ppid and is_rc_server(proc_argv(ppid)))


def latest_entrypoint(path):
    """`entrypoint` of the session's latest user/assistant entry: "sdk-cli" for
    turns written by an rc-server thread child (or another SDK host), "cli" for
    terminal / tmux turns. Only the LATEST counts: a session can carry old
    "sdk-cli" entries (e.g. from a former --bg start) and be a terminal one now."""
    if not path:
        return None
    return (last_message(path) or {}).get("entrypoint")


def archived_since_last_message(path):
    """True if an RC 'ended or archived from another device' notice is newer than
    the session's last user/assistant entry."""
    if not path:
        return False
    archived = False
    for e in iter_entries(path):
        t = e.get("type")
        if t in ("user", "assistant") and not e.get("isSidechain"):
            archived = False
        elif t == "system" and ARCHIVED_MARK in str(e.get("content", "")):
            archived = True
    return archived


def preflight(session_id):
    """_preflight(), failing safe (refuse) if the host's process info is unavailable
    (inside the container it comes over the SSH bridge)."""
    try:
        return _preflight(session_id)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, [f"host process info unavailable ({type(e).__name__}: {str(e)[:200]}); not touching the session"], None


def _preflight(session_id):
    """-> (ok, problems[], plan). No --bg anymore: the session is continued in
    its tmux session (send-keys) or resumed into a new one. A live process
    elsewhere would make a fresh resume FORK a copy, so (rc / registry checks first):
      - live in our tmux            -> plan 'send-keys'
      - live interactive elsewhere  -> refuse (someone's terminal), unless archived
                                       or --take-over-idle: SIGTERM it, then resume
      - held by a `claude rc` server -> always refuse (a take-over would fork the
                                       user's RC thread): the server, a child of it,
                                       argv with --sdk-url, or registry entrypoint sdk-cli
      - any other live pid in ~/.claude/sessions/*.json on this uuid that is not our
        tmux ka-<id8> process and not a `claude agents` row -> refuse (OTHER_HELD)
      - live bg worker              -> plan 'stop-bg-then-resume'
      - nothing alive, latest user/assistant entry has entrypoint "sdk-cli"
                                    -> refuse (RC_HELD: an idle rc-server/SDK thread)
      - nothing alive               -> plan 'resume'"""
    in_tmux = tmux_alive(session_id)
    ours = tmux_pids(session_id) if in_tmux else set()
    # Every live process the session registry says holds this uuid, except our own
    # tmux ka-<id8> process (and its children).
    reg = sorted(p for p in registry_holders(session_id) if not is_ours(p, ours))
    rows = [] if in_tmux else [a for a in agent_entries(session_id)
                               if pid_alive(a.get("pid")) and not is_ours(a.get("pid"), ours)]
    row_pids = [a["pid"] for a in rows]
    held = sorted({p for p in row_pids + reg if rc_held(p)})
    if held:
        return False, [f"{RC_HELD} thread (pid {held}); a take-over would fork the user's Remote Control "
                       "thread, so it is left alone"], None
    extra = [p for p in reg if p not in row_pids]
    if extra:
        return False, [f"{OTHER_HELD} {extra} (~/.claude/sessions" + (", outside our tmux" if in_tmux else
                       ", not listed by `claude agents`") + "); a send or resume would make two writers "
                       "on one transcript, so it is left alone"], None
    if in_tmux:
        return True, [], "send-keys"
    if any(a.get("state") == "working" or a.get("status") == "busy" for a in rows):
        return False, ["session is busy in another process (someone is using it)"], None
    inter = [a for a in rows if a.get("kind") == "interactive"]
    if inter and archived_since_last_message(transcript_path(session_id)):
        # The user archived it in the RC/web UI: that ends Remote Control but leaves the
        # local process idle. Archiving counts as closing (user rule, 29.09.), so take it over.
        return True, [], "take-over:" + ",".join(str(a["pid"]) for a in inter)
    if inter and not TAKE_OVER_IDLE:
        return False, [f"held by an idle interactive process {[a['pid'] for a in inter]} outside tmux; "
                       "a resume would FORK a copy (pass --take-over-idle to SIGTERM it first)"], None
    if inter:
        # An IDLE interactive holder (e.g. a terminal left open) would make a
        # fresh resume fork. Take it over: SIGTERM it, then resume into tmux.
        return True, [], "take-over:" + ",".join(str(a["pid"]) for a in inter)
    if any(a.get("kind") == "background" for a in rows):
        return True, [], "stop-bg-then-resume"
    if latest_entrypoint(transcript_path(session_id)) == "sdk-cli":
        # Nothing alive, but the last turn came from an rc-server/SDK host: the rc
        # server re-serves the thread on the next app message, so a resume now would
        # FORK the user's Remote Control thread once it's used again.
        return False, [f"{RC_HELD}: rc-server/SDK-hosted thread (idle; latest transcript entry has "
                       "entrypoint sdk-cli); a resume would fork the user's Remote Control thread, "
                       "so it is left alone"], None
    return True, [], "resume"


def fire(session_id, cwd, message, plan, new=False, name=None, model=None):
    """The one state-changing action: hand over to ka_resume.sh (tmux, no --bg).
    name/model override LAUNCH's for this launch."""
    if plan.startswith("take-over:"):
        pids = [int(x) for x in plan.split(":", 1)[1].split(",")]
        for pid in pids:
            try:
                host.kill_claude(pid)
            except ProcessLookupError:
                pass
        for _ in range(30):
            if not any(pid_alive(p) for p in pids):
                break
            time.sleep(1)
    if plan == "stop-bg-then-resume":
        host.run_on_host(["claude", "stop", session_id[:8]], env=SCRUBBED_ENV, timeout=60)
        time.sleep(5)
    cmd = [KA_RESUME, "--session", session_id, "--message", message,
           "--cwd", cwd]
    for k, v in {**LAUNCH, **({"name": name} if name else {}), **({"model": model} if model else {})}.items():
        if v:
            cmd += [f"--{k}", v]
    if new:
        cmd.append("--new")
    r = host.run_on_host(cmd, env=SCRUBBED_ENV, timeout=120)
    return r.returncode, r.stdout, r.stderr


def verify_reply(path, since, timeout=VERIFY_TIMEOUT):
    """Wait for a real (non-synthetic) assistant message newer than `since`."""
    deadline = time.time() + timeout.total_seconds()
    while time.time() < deadline:
        for e in iter_entries(path):
            ts = parse_ts(e.get("timestamp"))
            m = e.get("message") or {}
            if (e.get("type") == "assistant" and ts and ts > since
                    and m.get("model") not in (None, "<synthetic>")):
                return e
        time.sleep(10)
    return None


def process_env_flags(pid):
    env = host.claude_env(pid)
    if env is None:
        return None
    wanted = ("CLAUDE_GUARD_DISABLE=", "CLAUDE_EFFORT=", "CLAUDECODE=", "CLAUDE_CODE_CHILD_SESSION=",
              "CLAUDE_CODE_MESSAGING_SOCKET=")
    return [x for x in env if x.startswith(wanted)]


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"handled": {}, "fires": {}}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1, default=str)
    os.replace(tmp, STATE_FILE)


RUN_IDLE = timedelta(minutes=15)      # run_active(): the run's transcripts were written this recently
RUN_RESULTS = ("continued-in-place", "no-reply-within-timeout")   # fires that started a run
RUN_READING_EVERY = timedelta(minutes=5)  # = run_metrics.RUN_SAMPLE_EVERY: extra usage readings during a run


def run_active(session_id, now, st=None):
    """A keep-alive run is going right now (read-only; the quickview's next run, D-202): a real
    fire for this session (a window-start, postponed start or last-stretch slot; not a dry-run or
    a refused preflight) less than one session length ago, and the session's transcript or one of
    its subagents' written within RUN_IDLE, and the session not stopped at a limit (D-204: a limit
    hit ends the run). -> the fire time or None."""
    st = st if st is not None else load_state()
    fired = [parse_ts(v.get("at")) for v in (st.get("handled") or {}).values()
             if isinstance(v, dict) and v.get("result") in RUN_RESULTS]
    fired = [t for t in fired if t and now - afclaude_config.SESSION_LENGTH <= t <= now]
    if not fired:
        return None
    path = transcript_path(session_id)
    if not path or stall_info(last_message(path)):
        return None
    files = [path] + glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl"))
    try:
        last = max(os.path.getmtime(f) for f in files if os.path.exists(f))
    except ValueError:
        return None
    return max(fired) if now - datetime.fromtimestamp(last, UTC) <= RUN_IDLE else None


def progress_note(line):
    host.append_note(PROGRESS_FILE, f"- {datetime.now(BERLIN).strftime('%H:%M')} [keepalive.py] {line}\n")


# ---------------------------------------------------------------- main loop

def stall_status(session_id, now):
    """Limit detection only (no window, no budget, no usage fetch). -> (action, detail, stall),
    action one of NO_TRANSCRIPT, OTHER_ERROR, NO_STALL, WAIT_RESET (the limit has not reset +
    RESET_GRACE yet), RESET_PASSED; stall["reset"] = the reset time."""
    path = transcript_path(session_id)
    if not path:
        return "NO_TRANSCRIPT", "transcript not found", None
    last = last_message(path)
    stall = stall_info(last)
    if not stall:
        if last and (last.get("message") or {}).get("model") == "<synthetic>" and last.get("isApiErrorMessage"):
            return "OTHER_ERROR", f"stopped on a non-limit API error (not handled): {message_text(last)[:120]!r}", None
        return "NO_STALL", "last message is not a limit notice", None
    # The notice text comes from the server and is authoritative. The usage
    # cache is only a fallback: it was a day stale when the rc_poc watcher
    # trusted it and fired 80 min early.
    reset = stall["reset_from_text"]
    if reset is None:
        u = (read_usage_cache() or {}).get(stall["kind"]) or {}
        reset = u.get("resets_at") if u.get("resets_at") and u["resets_at"] > stall["timestamp"] \
            else stall["timestamp"] + timedelta(hours=5)
    stall["reset"] = reset
    not_before = reset + RESET_GRACE
    if now < not_before:
        return "WAIT_RESET", f"{stall['kind']} limit resets {berlin(reset)}; not before {berlin(not_before)}", stall
    return "RESET_PASSED", f"{stall['kind']} limit reset {berlin(reset)} has passed", stall


def evaluate(session_id, now, usage_getter=fresh_usage):
    """Continue decision for an APPROVED stalled session (the owner's own, D-206; the
    dispatcher's 10-min duty): right at its limit reset + RESET_GRACE, any time of day, with no
    window and no AFClaude budget/pacing gate; only a live session limit still at 100% waits.
    The AFClaude task-manager and every other AFClaude run never get this (D-204: a limit hit
    ends the run; the watcher only logs it). -> (action, detail, stall), action one of
    NO_TRANSCRIPT, OTHER_ERROR, NO_STALL, WAIT_RESET, FIRE."""
    action, detail, stall = stall_status(session_id, now)
    if action != "RESET_PASSED":
        return action, detail, stall
    usage = usage_getter(now)   # only fetched when a fire is actually on the table
    s = (usage or {}).get("session") or {}
    if s.get("percent", 0) >= 100 and s.get("resets_at") and s["resets_at"] > now:
        return "WAIT_RESET", f"live usage still shows session limit 100% until {berlin(s['resets_at'])}", stall
    w = (usage or {}).get("weekly") or {}
    if w.get("percent", 0) >= 100 and w.get("resets_at") and w["resets_at"] > now:
        return "WAIT_RESET", f"live usage still shows weekly limit 100% until {berlin(w['resets_at'])}", stall
    return "FIRE", f"{detail}; approved stall, continued at its reset (D-206)", stall


LIMIT_HIT_NOTE = ("the run is over: no continue at the limit reset (D-204); the next session-window start or "
                  "last-stretch slot start decides by its own check")


def note_limit_hit(sid, stall, st, now):
    """Once per stall of the watched (task-manager) session: log + PROGRESS note that the run
    ended at the limit (D-204: if the session is full, that is the job done for the run)."""
    key = stall.get("uuid") or str(stall.get("timestamp"))
    if key in st.setdefault("limit_hits", {}):
        return False
    # merge into the file as it is now: the cron --window-start process writes it too, and the
    # watcher's in-memory state may be days old (never overwrite its handled keys)
    disk = load_state()
    hits = disk.setdefault("limit_hits", {})
    hits.update(st["limit_hits"])
    hits[key] = now.isoformat()
    for k in sorted(hits, key=lambda k: hits[k])[:-20]:     # keep the last 20
        hits.pop(k, None)
    save_state(disk)
    st["limit_hits"] = dict(hits)
    nxt = next_session_start(now)
    progress_note(f"{sid[:8]} hit its {stall.get('kind')} limit at {berlin(stall['timestamp']) if stall.get('timestamp') else '?'}"
                  f" (resets {berlin(stall['reset']) if stall.get('reset') else '?'}): {LIMIT_HIT_NOTE}; "
                  f"next session-window start {berlin(nxt)}")
    return True


def run(args):
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another keepalive.py instance holds the lock; exiting")
    sid = args.session
    mode = "ARMED" if args.arm else "DRY-RUN"
    log(f"keepalive start ({mode}) target={sid} window {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M} Europe/Berlin pid={os.getpid()}")
    st = load_state()
    last_line = None
    next_eval = next_reading = datetime.min.replace(tzinfo=UTC)
    while True:
        if os.path.exists(STOP_FILE):
            log("STOP file present, exiting")
            return
        now = datetime.now(UTC)
        if now >= next_eval:
            reload_window()
            # Detection + logging only: a limit hit ends the run, the watcher never continues the
            # session at the reset (D-204). Runs begin only at starts: the session-window starts
            # (cron --window-start, a postponed one below) and the last-stretch slot starts.
            action, detail, stall = stall_status(sid, now)
            if stall:
                action, detail = "LIMIT_HIT", f"{detail}; {LIMIT_HIT_NOTE}"
                note_limit_hit(sid, stall, st, now)
            line = f"{action}: {detail}"
            if line != last_line:
                log(line)
                last_line = line
            next_eval = now + timedelta(seconds=POLL_SECONDS)
            # a last-stretch slot start decides by its own check, stalled or not (D-204)
            next_lm = last_mile_pass(sid, now, st, args)
            if next_lm:
                next_eval = max(next_eval, next_lm)
        # a postponed session-window start (D-018/D-202), also when the previous run ended at a
        # limit (D-204: "limit hit before 4am and the next run starts at 4am")
        deferred_window_start_pass(sid, now, st, args)
        if now >= next_reading:   # the fill-time measurement's extra readings (D-207)
            next_reading = now + RUN_READING_EVERY
            watch_run_usage(sid, now)
        if args.once:
            return
        time.sleep(POLL_SECONDS)


LAST_MILE_RECHECK = timedelta(minutes=15)


def _round_reset(resets_at):
    """resets_at jitters by milliseconds between usage fetches (16:59:59.557 vs
    17:00:00.320): round to the nearest minute, like usage_sampler.track_cycle."""
    r = resets_at.astimezone(UTC) + timedelta(seconds=30)
    return r.replace(second=0, microsecond=0)


def last_mile_slot(left):
    """Session slot of the last mile, counted back from the weekly reset: 1 = the final
    session length before it, 2 = the one before, ... (the "auto" last mile is a whole
    number of slots, so each slot is one session window that ends on a slot boundary)."""
    return max(1, -int(-left.total_seconds() // afclaude_config.SESSION_LENGTH.total_seconds()))


def last_mile_key(resets_at, slot=None):
    k = f"last-mile-{_round_reset(resets_at).isoformat()}"
    return k if slot is None else f"{k}-s{slot}"


def last_mile_handled(key, handled):
    """True if this slot of this weekly cycle's last mile was already handled, also under
    an older raw (un-rounded) reset time such as last-mile-2026-10-01T16:59:59.557562+00:00-s1."""
    if key in handled:
        return True
    want = re.fullmatch(r"last-mile-(.+?)(-s\d+)?", key)
    for k in handled:
        m = re.fullmatch(r"last-mile-(.+?)(-s\d+)?", k)
        if not (m and want) or m.group(2) != want.group(2):
            continue
        try:
            t = parse_ts(m.group(1))
        except ValueError:
            continue
        if t and t.tzinfo and last_mile_key(t) + (m.group(2) or "") == key:
            return True
    return False


def last_mile_next_slot(now, st=None):
    """Read-only (the quickview's next run, D-204): inside the last stretch, None while the
    current slot's start is still due (not handled yet), else the next slot start, or False if
    the final slot was handled (no more run before the weekly reset: nothing continues at a
    limit reset in between). Outside the last stretch: None."""
    w = (read_usage_cache() or {}).get("weekly") or {}
    left = last_mile_left(w.get("resets_at"), now, w.get("percent"))
    if left is None:
        return None
    slot = last_mile_slot(left)
    st = st if st is not None else load_state()
    if not last_mile_handled(last_mile_key(w["resets_at"], slot), st.get("handled") or {}):
        return None
    return _round_reset(w["resets_at"]) - (slot - 1) * afclaude_config.SESSION_LENGTH if slot > 1 else False


def last_mile_pass(sid, now, st, args):
    """Once per last-mile slot (one session length, counted back from the weekly reset):
    continue the session when the last mile opens and again at each later slot start, even
    outside the night window, so the remaining quota gets used (D-020). A slot start is a
    start like a session-window start (D-204): it decides by its own check whether or not the
    previous slot's run ended at a limit, and nothing continues at a limit reset in between
    (a slot already handled waits for the next slot). A limit that has not reset yet makes the
    slot's start wait for that reset. Returns the next re-check time after a HOLD, else None."""
    w = (read_usage_cache() or {}).get("weekly") or {}
    left = last_mile_left(w.get("resets_at"), now, w.get("percent"))
    if left is None:
        return None
    key = last_mile_key(w["resets_at"], last_mile_slot(left))
    if last_mile_handled(key, st["handled"]):
        return None
    action, detail, stall = stall_status(sid, now)
    if action == "WAIT_RESET":
        log(f"last-mile: slot start waits for the limit reset, {detail}")
        return stall["reset"] + RESET_GRACE
    d = budget_eval(fresh_usage(now, force=True), now)
    log(f"last-mile: {d['reason']}")
    if not d["go"]:
        return max(d.get("recheck_at") or now + LAST_MILE_RECHECK, now + timedelta(seconds=POLL_SECONDS)) \
            if d.get("postpone") else now + LAST_MILE_RECHECK
    handle_fire(sid, {"uuid": key, "timestamp": now, "prompt": "last_mile", "budget": d["text"]},
                d["reason"], st, args)
    return None


# ---------------------------------------------------------------- window start

def postpone_deadline(now):
    """A start postponed at the session-window start of `now` must begin before this (the next
    session-window start of the night, which checks itself); `now` at the last start or outside
    the window: no postponement (D-202, pacing.postpone_deadline)."""
    import pacing
    s = latest_session_start(now)
    return pacing.postpone_deadline(s, _win()) if s is not None else now


def window_start_pass(sid, now, args):
    """The session-window-start continue (no stall needed), one decision per session-window start
    of the night (D-202: 23:00 and 04:00): FIRE, a final HOLD, or a POSTPONE (e.g. the user was
    active) that the watcher re-decides at its recheck time (deferred_window_start_pass), but only
    while that is before the night's next session-window start: a later recheck (also any at the
    last start, the run would end after the window end) is skipped; the next session-window start
    decides again. -> the decision dict."""
    u = fresh_usage(now, force=True)
    d = budget_eval(u, now)
    log(f"window-start: {d['reason']}")
    key = f"manual-now-{now.isoformat()}" if args.now else window_start_key(now)
    deadline = postpone_deadline(now)
    defer = bool(d.get("postpone") and d.get("recheck_at") and not args.now and d["recheck_at"] < deadline)
    if not args.now:
        note_window_usage(key, u is not None, now, final=not defer)
    if not args.now and getattr(args, "arm", False):
        try:                                  # score the forecast later (pacing.forecast_errors)
            import pacing
            pacing.record_forecast(d, ((u or {}).get("weekly") or {}).get("resets_at"))
        except Exception as e:   # noqa: BLE001 - the log is advisory
            log(f"forecast log failed: {type(e).__name__}: {e}")
    if d["go"]:
        stall = {"uuid": key, "timestamp": now, "budget": d["text"]}
        handle_fire(sid, stall, "window start, " + d["reason"], load_state(), args)
    elif defer:
        defer_window_start(key, sid, d["recheck_at"], d["reason"], deadline)
        progress_note(f"window-start {berlin(now)}: postponed to {berlin(d['recheck_at'])}, {d['reason']}")
    elif d.get("postpone") and d.get("recheck_at") and not args.now:
        progress_note(f"window-start {berlin(now)}: not resumed, postponed past the night's next session-window "
                      f"start ({berlin(d['recheck_at'])} >= {berlin(deadline)}), skipped to the next "
                      f"session-window start {berlin(next_session_start(now))} (D-202); {d['reason']}")
    else:
        progress_note(f"window-start {berlin(now)}: not resumed, {d['reason']}")
    return d


# ---------------------------------------------------------------- postponed window start

DEFER_FILE = os.path.join(STATE_DIR, "keepalive_deferred.json")


def load_deferred():
    try:
        with open(DEFER_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_deferred(d):
    tmp = DEFER_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1, default=str)
    os.replace(tmp, DEFER_FILE)


def defer_window_start(key, sid, recheck_at, reason, deadline=None):
    """The session-window-start decision was a POSTPONE (the user was active, ...): the watcher
    re-decides at recheck_at, still once per session-window start (same dedup key), and only
    before `deadline` (the night's next session-window start, D-202)."""
    d = {k: v for k, v in load_deferred().items() if k == key}   # older starts are over
    d[key] = {"session": sid, "recheck_at": recheck_at.astimezone(UTC).isoformat(), "reason": reason,
              "deferred_at": datetime.now(UTC).isoformat()}
    if deadline is not None:
        d[key]["deadline"] = deadline.astimezone(UTC).isoformat()
    save_deferred(d)


def pending_deferral(sid, now):
    """The recheck time of the postponed session-window start the watcher will still re-decide
    for this session (deferred_window_start_pass), or None. Read-only (the quickview's next run)."""
    if not in_window(now):
        return None
    ent = load_deferred().get(window_start_key(now))
    if not ent or ent.get("session") != sid or window_start_key(now) in load_state().get("handled", {}):
        return None
    return parse_ts(ent.get("recheck_at"))


def deferred_window_start_pass(sid, now, st, args):
    """Watcher side of a postponed session-window start: at its recheck time, decide again and
    fire under the same window-start key, postpone again, or drop it (a final HOLD, or the
    recheck would reach the night's next session-window start or the window end: that start
    decides itself, D-202). -> the next recheck time or None."""
    defs = load_deferred()
    if not defs:
        return None
    # the key changes at each session-window start, so an entry is stale from the next start on
    key = window_start_key(now) if in_window(now) else None
    ent = defs.get(key) if key else None
    stale = [k for k in defs if k != key or (ent and ent.get("session") != sid)]
    if ent and ent.get("session") != sid:
        ent = None
    if ent and (key in st["handled"] or key in load_state().get("handled", {})):
        stale.append(key)
        ent = None
    if stale:
        for k in stale:
            defs.pop(k, None)
        save_deferred(defs)
    if not ent:
        return None
    due = parse_ts(ent.get("recheck_at")) or now
    if now < due:
        return due
    u = fresh_usage(now, force=True)
    d = budget_eval(u, now)
    log(f"window-start (postponed): {d['reason']}")
    deadline = postpone_deadline(now)
    again = bool(d.get("postpone") and d.get("recheck_at") and d["recheck_at"] < deadline)
    note_window_usage(key, u is not None, now, final=not again)
    if d["go"]:
        defs.pop(key, None)
        save_deferred(defs)
        handle_fire(sid, {"uuid": key, "timestamp": now, "budget": d["text"]},
                    "window start (postponed), " + d["reason"], st, args)
        return None
    if again:
        ent["recheck_at"] = d["recheck_at"].astimezone(UTC).isoformat()
        ent["reason"] = d["reason"]
        save_deferred(defs)
        return d["recheck_at"]
    defs.pop(key, None)
    save_deferred(defs)
    if d.get("postpone") and d.get("recheck_at"):
        progress_note(f"window-start (postponed) {berlin(now)}: not resumed, postponed past the night's next "
                      f"session-window start ({berlin(d['recheck_at'])} >= {berlin(deadline)}), skipped to the "
                      f"next session-window start {berlin(next_session_start(now))} (D-202); {d['reason']}")
    else:
        progress_note(f"window-start (postponed) {berlin(now)}: not resumed, {d['reason']}")
    return None


def _fire_usage():
    """The usage cache at a fire (session/weekly %, reset times, fetch time; the start decision
    just refreshed it), JSON-safe, or None. Never fails a fire."""
    try:
        import run_metrics
        return run_metrics.fire_usage(read_usage_cache())
    except Exception:   # noqa: BLE001 - advisory: run_metrics falls back to the sampler rows
        return None


def watch_run_usage(sid, now):
    """While a run is going: one extra usage reading per run_metrics.RUN_SAMPLE_EVERY into
    data/run_usage.jsonl (D-207, the fill-time measurement). Never fails the watcher."""
    try:
        import run_metrics
        run_metrics.watch_sample(sid, now)
    except Exception as e:   # noqa: BLE001 - advisory
        log(f"run usage reading failed: {type(e).__name__}: {e}")


def handle_fire(sid, stall, reason, st, args):
    key = stall["uuid"] or str(stall["timestamp"])
    if key in st["handled"]:
        return
    night = current_window_end(datetime.now(UTC)).date().isoformat()
    if st["fires"].get(night, 0) >= MAX_FIRES_PER_WINDOW:
        log(f"fire cap reached for window ending {night}; not firing")
        return
    ok, problems, plan = preflight(sid)
    log(f"preflight: {'ok, plan=' + plan if ok else 'PROBLEMS: ' + '; '.join(problems)}")
    if not ok:
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "preflight-failed", "problems": problems}
        save_state(st)
        progress_note(f"did NOT fire for stall {key}: preflight failed: {problems}")
        alert(f"keep-alive did NOT continue {sid[:8]}: preflight failed", "; ".join(problems))
        return
    budget = stall.get("budget") or budget_headroom(read_usage_cache(), datetime.now(UTC))[1]
    msg = session_message(stall.get("prompt", "continue"), reason=f"{reason}; {budget}", progress=PROGRESS_FILE)
    path = transcript_path(sid)
    # the last recorded cwd can be a directory that has since been renamed/deleted
    cwd = next((c for c in ((last_message(path) or {}).get("cwd"), HERE) if c and os.path.isdir(c)), HERE)
    if not args.arm:
        log(f"WOULD FIRE (dry-run): plan={plan} cwd={cwd} ka_resume.sh --session {sid} {LAUNCH} {msg!r}")
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "dry-run", "plan": plan, "reason": reason}
        save_state(st)
        progress_note(f"DRY-RUN would have continued {sid[:8]} via {plan} ({reason})")
        return
    sent_at = datetime.now(UTC)
    at_fire = _fire_usage()          # the run's start values (run_metrics.py, D-207)
    rc, out, err = fire(sid, cwd, msg, plan)
    st["fires"][night] = st["fires"].get(night, 0) + 1
    log(f"FIRED plan={plan} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    result = {"at": sent_at.isoformat(), "plan": plan, "rc": rc, "stdout": out, "stderr": err, "reason": reason,
              "session": sid, "usage": at_fire}
    reply = verify_reply(path, sent_at) if rc == 0 else None
    live = [a for a in agent_entries(sid) if pid_alive(a.get("pid"))]
    result["env"] = [process_env_flags(a["pid"]) for a in live]
    if reply:
        result["result"] = "continued-in-place"
        result["reply_model"] = (reply.get("message") or {}).get("model")
        log(f"VERIFIED: new assistant message in {sid[:8]} at {reply.get('timestamp')} "
            f"model={result['reply_model']} env={result['env']}")
    else:
        result["result"] = "no-reply-within-timeout" if rc == 0 else "launcher-failed"
        log(f"VERIFY FAILED: {result['result']}")
        alert(f"keep-alive continue of {sid[:8]} failed: {result['result']}",
              f"plan={plan} rc={rc} stdout={out.strip()[:300]} stderr={err.strip()[:500]}")
    st["handled"][key] = result
    save_state(st)
    progress_note(f"fired continue for {sid[:8]} via {plan}: {result.get('result')} ({reason})")


def load_backlog():
    """BACKLOG.md numbered items: '1. **Title** - description' (+ indented continuation lines)."""
    items, cur = [], None
    with open(BACKLOG_FILE) as fh:
        for line in fh:
            m = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(?:[—-]+\s*)?(.*)$", line.rstrip("\n"))
            if m:
                cur = {"n": int(m.group(1)), "title": m.group(2), "desc": m.group(3).strip()}
                items.append(cur)
            elif cur and line.startswith("   ") and line.strip():
                cur["desc"] += " " + line.strip()
            else:
                cur = None
    return items


def work_on_project(sel, args):
    """Start a backlog project immediately in a NEW tmux session, ignoring the nightly
    window. The budget rule still applies. `sel` = list number or title substring."""
    items = load_backlog()
    hits = [i for i in items if (sel.isdigit() and i["n"] == int(sel)) or (not sel.isdigit() and sel.lower() in i["title"].lower())]
    if len(hits) != 1:
        sys.exit(f"--work-on {sel!r}: {'no match' if not hits else 'ambiguous'}; backlog: "
                 + "; ".join(f"{i['n']}={i['title']}" for i in items))
    p = hits[0]
    now = datetime.now(UTC)
    go, reason = budget_decision(fresh_usage(now, force=True), now)
    log(f"work-on {p['title']!r} (window ignored): {reason}")
    if not go:
        progress_note(f"work-on {p['title']!r} not started: {reason}")
        return
    sid = str(uuid.uuid4())
    slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")
    msg = session_message("project_start", title=p["title"], desc=p["desc"], slug=slug)
    if not args.arm:
        log(f"WOULD START (dry-run): new session {sid} for {p['title']!r} cwd={HERE}")
        return
    sent_at = datetime.now(UTC)
    rc, out, err = fire(sid, HERE, msg, "resume", new=True, name=p["title"])
    log(f"STARTED project {p['title']!r} session={sid} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    st = load_state()
    st.setdefault("projects", {})[sid] = {"title": p["title"], "at": sent_at.isoformat(), "rc": rc, "reason": reason}
    save_state(st)
    reply = None
    if rc == 0:
        deadline = time.time() + 120
        while not transcript_path(sid) and time.time() < deadline:
            time.sleep(5)
        path = transcript_path(sid)
        reply = verify_reply(path, sent_at) if path else None
    log(f"work-on {p['title']!r}: {'VERIFIED new assistant message' if reply else 'VERIFY FAILED'}")
    progress_note(f"started backlog project {p['title']!r} as session {sid[:8]} "
                  f"({'verified' if reply else 'NOT verified'}; {reason})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.environ.get("KEEPALIVE_SESSION"),
                    help="FULL session UUID to keep alive")
    ap.add_argument("--arm", action="store_true", help="actually fire resumes (default: dry-run)")
    ap.add_argument("--once", action="store_true", help="single evaluation pass, then exit")
    ap.add_argument("--decide", action="store_true",
                    help="print the window + budget decision for right now and exit")
    ap.add_argument("--last-mile", action="store_true",
                    help="one-shot: continue the session if the last-mile period before the weekly reset is open")
    ap.add_argument("--window-start", action="store_true",
                    help="one-shot: at a session-window start (23:00, 04:00; D-202), continue the session if the budget rule allows")
    ap.add_argument("--now", action="store_true",
                    help="with --window-start: ignore the start-hour gate (a manual start now); budget rule still applies")
    ap.add_argument("--work-on", metavar="PROJECT",
                    help="start a BACKLOG.md project right now in a new tmux session (list number or title substring); "
                         "needs --arm to actually start")
    ap.add_argument("--take-over-idle", action="store_true",
                    help="if an idle interactive process (e.g. an open terminal) holds the session, SIGTERM it and resume in tmux")
    ap.add_argument("--model", default=LAUNCH["model"], help="model to relaunch with (tmux resume)")
    ap.add_argument("--effort", default=LAUNCH["effort"], help="effort to relaunch with")
    ap.add_argument("--name", default=LAUNCH["name"], help="Remote Control display name")
    args = ap.parse_args()
    LAUNCH.update(model=args.model, effort=args.effort, name=args.name)
    global TAKE_OVER_IDLE, IGNORE_WINDOW
    TAKE_OVER_IDLE = args.take_over_idle
    IGNORE_WINDOW = args.now
    if args.work_on:
        return work_on_project(args.work_on, args)
    if args.last_mile:
        if not args.session:
            sys.exit("--last-mile needs --session")
        st = load_state()
        nxt = last_mile_pass(args.session, datetime.now(UTC), st, args)
        print("not in the last mile" if not in_last_mile(datetime.now(UTC)) else
              (f"held, re-check after {berlin(nxt)}" if nxt else "done (fired or already handled)"))
        return
    if args.window_start and not args.session:
        sys.exit("--window-start needs --session")
    if args.window_start:
        # Session-window-start continue (no stall needed, D-202): budget rule + window, then fire.
        now = datetime.now(UTC)
        if not args.now and not is_window_start_hour(now):
            return
        window_start_pass(args.session, now, args)
        return
    if args.decide:
        now = datetime.now(UTC)
        u = fresh_usage(now, force=True)
        print(f"now {berlin(now)} in_window={in_window(now)} next_window={berlin(next_window_start(now))}")
        print(f"usage: {json.dumps(u, default=str)}")
        d = budget_eval(u, now)
        print((d["go"], d["reason"]))
        print(d["text"])
        if d.get("postpone") and d.get("recheck_at"):
            print(f"postponed: recheck {berlin(d['recheck_at'])}")
        w = (u or {}).get("weekly") or {}
        if w.get("resets_at"):
            setting = afclaude_config.last_mile_setting()
            lm = timedelta(hours=last_mile_hours(w.get("percent"), w["resets_at"], now))
            how = ("auto: min(ceil(session windows of quota left), 2) x session length"
                   if setting == "auto" else "last_mile_hours")
            print(f"last mile ({how}): {lm} before the weekly reset → opens {berlin(w['resets_at'] - lm)}"
                  if lm > timedelta(0) else "last mile: off")
        for k, v in load_deferred().items():
            print(f"postponed window start {k}: recheck {v.get('recheck_at')}")
        if d.get("forecast_source"):
            print(f"forecast: {d['forecast_source']}")
        try:
            import pacing
            for line in pacing.threshold_lines(pacing.threshold_info(now=now, decision=d)):
                print(line)
        except Exception:   # noqa: BLE001 - advisory
            pass
        if args.session:
            print(stall_status(args.session, now)[:2])
        return
    if not args.session or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args.session):
        sys.exit("--session must be the FULL session UUID (a short id or a prefix makes resume fork)")
    run(args)


def alert(subject, body=""):
    """Failure alert (ALERTS.md + PROGRESS.md + best-effort push). Never raises."""
    log(f"ALERT: {subject}")
    try:
        from notify import notify
        log(f"alert push: {notify(subject, body)}")
    except Exception as e:  # noqa: BLE001 - alerts must not take the watcher down
        log(f"alert itself failed: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # e.g. the 2026-09-29 00:00 fire died on "Permission denied: ka_resume.sh" and nobody noticed
        import traceback
        traceback.print_exc()
        alert(f"keepalive.py crashed: {type(e).__name__}: {e}"[:200], traceback.format_exc()[-1500:])
        sys.exit(1)
