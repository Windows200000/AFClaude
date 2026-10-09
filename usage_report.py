#!/usr/bin/env python3
"""
Usage report for one Claude Code session: how much each subagent (and the
main session itself) has consumed, so the manager can watch the session cap.

    usage_report.py [--session UUID] [--since ISO|--last] [--json] [--fresh] [--record]

--session      defaults to $CLAUDE_CODE_SESSION_ID, else the AFClaude manager
               session (manager_session in data/afclaude.json).
--since ISO    only count assistant turns whose timestamp is after ISO.
--last         only count usage since the previous --record'ed report for
               this session (a delta). Ignored if --since is also given.
--json         machine-readable output instead of the table.
--fresh        run the local /usage command first (keepalive.fresh_usage;
               no model call) instead of trusting the possibly-stale
               ~/.claude.json cache.
--record       append a JSON row to data/usage_reports.jsonl (gitignored) so
               --last has something to diff against next time.

Data sources (read-only, never written to except --record's own file):
  - main transcript(s): ~/.claude/projects/<proj>/<session>.jsonl. A session
    can have more than one if its cwd changed mid-session; all are combined.
  - subagent transcripts: ~/.claude/projects/<proj>/<session>/subagents/
    agent-<agentId>.jsonl, with a sibling agent-<agentId>.meta.json holding
    its description/agentType (not its model or usage -- those only live in
    the transcript itself).

Token accounting: assistant entries carry message.id (repeated across
streamed chunks -- the last one seen wins) and message.usage. `model ==
"<synthetic>"` entries (rate-limit notices etc.) are skipped. output_tokens
already includes any thinking tokens (output_tokens_details.thinking_tokens
is a breakdown, not an addition), so the "out(think)" column shows the total
with the thinking share in parens, and input+cache-write is the sum of
input_tokens and cache_creation_input_tokens (a genuine sum, unrelated
counters). "turns" = number of distinct message ids.

A subagent is "running" if its transcript's last timestamped entry is younger
than RUNNING_MAX_AGE; otherwise "done". There is no explicit completion
marker in the transcript itself to fall back on.
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (usage cache, PROJECTS_DIR, berlin/parse_ts helpers)
import afclaude_config  # noqa: E402  (local machine-specific values: the manager session)
import telemetry  # noqa: E402  (phase 2c: --record also writes the DB table usage_reports)

UTC = timezone.utc
BERLIN = ka.BERLIN
DATA = os.path.join(HERE, "data")
REPORTS_FILE = os.path.join(DATA, "usage_reports.jsonl")
DEFAULT_SESSION = afclaude_config.manager_session()   # data/afclaude.json manager_session
RUNNING_MAX_AGE = 300  # seconds since last transcript entry -> still "running"


def now_utc():
    return datetime.now(UTC)


def berlin(dt):
    return ka.berlin(dt) if dt else "-"


def parse_ts(s):
    return ka.parse_ts(s) if s else None


def parse_since(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------- discovery

def main_transcripts(session_id):
    return sorted(glob.glob(os.path.join(ka.PROJECTS_DIR, "*", f"{session_id}.jsonl")))


def subagent_files(session_id):
    """agentId -> list of agent-<id>.jsonl paths (usually one, but a session's
    cwd can change mid-session, same as the main transcript)."""
    by_agent = {}
    for f in glob.glob(os.path.join(ka.PROJECTS_DIR, "*", session_id, "subagents", "agent-*.jsonl")):
        base = os.path.basename(f)
        agent_id = base[len("agent-"):-len(".jsonl")]
        by_agent.setdefault(agent_id, []).append(f)
    return by_agent


def load_meta(session_id, agent_id):
    for f in glob.glob(os.path.join(ka.PROJECTS_DIR, "*", session_id, "subagents",
                                     f"agent-{agent_id}.meta.json")):
        try:
            with open(f) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


# ---------------------------------------------------------------- scanning

def scan(paths, since=None):
    """One source (main transcript(s), or one subagent's transcript(s)).
    Returns turns / token totals / model / first+last activity."""
    msgs = {}  # message.id -> (ts, model, usage) ; last (by timestamp) wins
    first_ts = last_ts = None
    for path in paths:
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if b'"type":"assistant"' not in raw and b'"type": "assistant"' not in raw:
                    # still worth a cheap timestamp pass for first/last activity
                    if b'"timestamp"' in raw:
                        try:
                            e = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        ts = parse_ts(e.get("timestamp"))
                        if ts and (since is None or ts > since):
                            if first_ts is None or ts < first_ts:
                                first_ts = ts
                            if last_ts is None or ts > last_ts:
                                last_ts = ts
                    continue
                try:
                    e = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = parse_ts(e.get("timestamp"))
                if since is not None and ts is not None and ts <= since:
                    continue
                if ts is not None:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                m = e.get("message") or {}
                if m.get("model") == "<synthetic>":
                    continue
                mid = m.get("id")
                if mid is None:
                    continue
                prev = msgs.get(mid)
                if prev is not None and prev[0] is not None and ts is not None and ts < prev[0]:
                    continue  # keep whichever occurrence is temporally last
                msgs[mid] = (ts, m.get("model"), m.get("usage") or {})

    tok = {"in": 0, "cache_w": 0, "cache_r": 0, "out": 0, "think": 0}
    model_counts = {}
    for _mid, (_ts, model, u) in msgs.items():
        tok["in"] += u.get("input_tokens") or 0
        tok["cache_w"] += u.get("cache_creation_input_tokens") or 0
        tok["cache_r"] += u.get("cache_read_input_tokens") or 0
        tok["out"] += u.get("output_tokens") or 0
        tok["think"] += (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
        model_counts[model or "?"] = model_counts.get(model or "?", 0) + 1
    model = "+".join(sorted(model_counts, key=lambda k: -model_counts[k])) if model_counts else "-"

    return {
        "turns": len(msgs),
        "tokens": tok,
        "model": model,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def status_of(last_ts, now):
    if last_ts is None:
        return "-"
    return "running" if (now - last_ts).total_seconds() < RUNNING_MAX_AGE else "done"


def short(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- rows

def build_rows(session_id, since):
    now = now_utc()
    rows = []
    for agent_id, paths in sorted(subagent_files(session_id).items(),
                                   key=lambda kv: kv[0]):
        meta = load_meta(session_id, agent_id)
        r = scan(paths, since)
        rows.append({
            "id": agent_id,
            "short_id": agent_id[:8],
            "description": meta.get("description") or meta.get("agentType") or "",
            "agent_type": meta.get("agentType") or "",
            **r,
            "status": status_of(r["last_ts"], now),
        })
    rows.sort(key=lambda r: r["last_ts"] or datetime.min.replace(tzinfo=UTC))

    main = scan(main_transcripts(session_id), since)
    main_row = {"id": "main", "short_id": "main", "description": "(main session)",
                "agent_type": "", **main, "status": status_of(main["last_ts"], now)}

    return rows, main_row


def totals(rows, main_row):
    tot = {"in": 0, "cache_w": 0, "cache_r": 0, "out": 0, "think": 0}
    turns = 0
    first_ts = main_row["first_ts"]
    last_ts = main_row["last_ts"]
    for r in rows + [main_row]:
        for k in tot:
            tot[k] += r["tokens"][k]
        turns += r["turns"]
        if r["first_ts"] and (first_ts is None or r["first_ts"] < first_ts):
            first_ts = r["first_ts"]
        if r["last_ts"] and (last_ts is None or r["last_ts"] > last_ts):
            last_ts = r["last_ts"]
    return {"turns": turns, "tokens": tot, "first_ts": first_ts, "last_ts": last_ts}


# ---------------------------------------------------------------- record / --last

def serialize_usage(usage):
    if not usage:
        return None
    out = {}
    for key in ("session", "weekly"):
        if usage.get(key):
            out[key] = {"percent": usage[key]["percent"],
                        "resets_at": usage[key]["resets_at"].isoformat() if usage[key].get("resets_at") else None}
    out["fetched_at"] = usage["fetched_at"].isoformat() if usage.get("fetched_at") else None
    return out


def record(session_id, rows, main_row, tot, usage):
    os.makedirs(DATA, exist_ok=True)
    row = {
        "at": now_utc().isoformat(),
        "session": session_id,
        "main": {"turns": main_row["turns"], "tokens": main_row["tokens"]},
        "subagents": {r["id"]: {"turns": r["turns"], "tokens": r["tokens"],
                                 "description": r["description"]} for r in rows},
        "total": {"turns": tot["turns"], "tokens": tot["tokens"]},
        "usage": serialize_usage(usage),
    }
    line = json.dumps(row)
    with open(REPORTS_FILE, "a") as fh:
        fh.write(line + "\n")
    telemetry.record("usage.report", line, REPORTS_FILE, actor="cli", via="cli")   # phase 2c dual-write
    return row


def last_recorded_ts(session_id):
    """The time of the session's last recorded report (the DB once imported, else the file)."""
    try:
        src = telemetry.lines(REPORTS_FILE)
    except OSError:
        return None
    last = None
    for line in src:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("session") == session_id and row.get("at"):
            last = row["at"]
    return parse_ts(last) if last else None


# ---------------------------------------------------------------- printing

def fmt_tok(t):
    return f"{t['out']}({t['think']})", f"{t['in'] + t['cache_w']}", f"{t['cache_r']}"


def print_table(session_id, rows, main_row, tot, usage, since, cache_age):
    cols = ["id", "description", "model", "status", "turns", "out(think)", "in+cw", "cache_r", "first", "last"]
    widths = {"id": 8, "description": 40, "model": 26, "status": 7, "turns": 5,
              "out(think)": 14, "in+cw": 10, "cache_r": 10, "first": 19, "last": 19}

    def line(vals):
        return "  ".join(str(v).ljust(widths[c]) for c, v in zip(cols, vals))

    print(f"Usage report -- session {session_id}" + (f"  (since {berlin(since)})" if since else ""))
    print(line(cols))
    print(line(["-" * widths[c] for c in cols]))
    for r in rows:
        o, ic, cr = fmt_tok(r["tokens"])
        print(line([r["short_id"], short(r["description"], 40), r["model"], r["status"],
                     r["turns"], o, ic, cr,
                     berlin(r["first_ts"]) if r["first_ts"] else "-",
                     berlin(r["last_ts"]) if r["last_ts"] else "-"]))
    print(line(["-" * widths[c] for c in cols]))
    o, ic, cr = fmt_tok(main_row["tokens"])
    print(line([main_row["short_id"], main_row["description"], main_row["model"], main_row["status"],
                main_row["turns"], o, ic, cr,
                berlin(main_row["first_ts"]) if main_row["first_ts"] else "-",
                berlin(main_row["last_ts"]) if main_row["last_ts"] else "-"]))
    o, ic, cr = fmt_tok(tot["tokens"])
    print(line(["TOTAL", "", "", "", tot["turns"], o, ic, cr,
                berlin(tot["first_ts"]) if tot["first_ts"] else "-",
                berlin(tot["last_ts"]) if tot["last_ts"] else "-"]))

    session_out = tot["tokens"]["out"] or 1
    print("\nShare of session output tokens:")
    for r in rows:
        pct = 100.0 * r["tokens"]["out"] / session_out
        print(f"  {r['short_id']:8}  {pct:5.1f}%")
    main_pct = 100.0 * main_row["tokens"]["out"] / session_out
    print(f"  {'main':8}  {main_pct:5.1f}%")

    print()
    if usage:
        for key, label in (("session", "session"), ("weekly", "weekly")):
            u = usage.get(key)
            if u:
                reset = berlin(u["resets_at"]) if u.get("resets_at") else "?"
                print(f"{label}: {u['percent']:.0f}% used, resets {reset}")
        if usage.get("fetched_at"):
            print(f"usage cache age: {cache_age}")
    else:
        print("usage: unavailable (no cache; try --fresh)")


def print_json(session_id, rows, main_row, tot, usage, since):
    def ser(r):
        d = dict(r)
        d["first_ts"] = d["first_ts"].isoformat() if d["first_ts"] else None
        d["last_ts"] = d["last_ts"].isoformat() if d["last_ts"] else None
        return d
    out = {
        "session": session_id,
        "since": since.isoformat() if since else None,
        "subagents": [ser(r) for r in rows],
        "main": ser(main_row),
        "total": {**tot, "first_ts": tot["first_ts"].isoformat() if tot["first_ts"] else None,
                  "last_ts": tot["last_ts"].isoformat() if tot["last_ts"] else None},
        "usage": serialize_usage(usage),
    }
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session UUID (default $CLAUDE_CODE_SESSION_ID, "
                                       f"else {DEFAULT_SESSION})")
    ap.add_argument("--since", help="only count usage after this ISO timestamp")
    ap.add_argument("--last", action="store_true", help="only count usage since the "
                                                          "previous --record'ed report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="run /usage locally first (no model call)")
    ap.add_argument("--record", action="store_true", help="append a row to data/usage_reports.jsonl")
    args = ap.parse_args(argv)

    session_id = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or DEFAULT_SESSION

    since = None
    if args.since:
        since = parse_since(args.since)
    elif args.last:
        since = last_recorded_ts(session_id)

    rows, main_row = build_rows(session_id, since)
    tot = totals(rows, main_row)

    if args.fresh:
        usage = ka.fresh_usage(now_utc(), force=True)
    else:
        usage = ka.read_usage_cache()
    cache_age = "-"
    if usage and usage.get("fetched_at"):
        secs = (now_utc() - usage["fetched_at"]).total_seconds()
        cache_age = f"{int(secs // 60)}m{int(secs % 60):02d}s"

    if args.record:
        record(session_id, rows, main_row, tot, usage)

    if args.json:
        print_json(session_id, rows, main_row, tot, usage, since)
    else:
        print_table(session_id, rows, main_row, tot, usage, since, cache_age)


if __name__ == "__main__":
    main()
