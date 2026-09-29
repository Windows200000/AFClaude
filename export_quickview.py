#!/usr/bin/env python3
"""Export a read-only snapshot of AFClaude's state for the quickview page.

Writes into data/quickview/ (gitignored), which the key-gated nginx in quickview/
serves at /afclaude/:

  status.json   keepalive+usage, progress (from GOALS.md), latest keep-alive
                decision, needs-your-input (OPEN_QUESTIONS.md + task-store
                inbox), AFClaude cron entries
  docs/*.md     PROGRESS, GOALS, OPEN_QUESTIONS, ALERTS, EXCEPTIONS, BACKLOG,
                README, design doc, prompts/*.md

Never exports data/samples.jsonl or data/haiku.jsonl (raw usage/message
details); only small aggregates. Never calls /usage: usage comes from the
~/.claude.json cache that the sampler keeps fresh. All times Europe/Berlin.
Run from cron every few minutes (via flock); each file is replaced atomically.
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (pure helpers only: cache read, budget rule, window)
import store  # noqa: E402  (read-only: pending_user_input)

OUT = os.environ.get("QUICKVIEW_DIR", os.path.join(HERE, "data", "quickview"))
DESIGN_DOC = os.environ.get("QUICKVIEW_DESIGN_DOC", os.path.expanduser(
    "~/.claude/projects/-mnt-BlockVolume-Claude/memory/task_manager_mcp_plan.md"))
# This manager's own session/tmux (matches usage_report.py's DEFAULT_SESSION).
SELF_SESSION = os.environ.get("QUICKVIEW_SELF_SESSION", "f2897285-dd97-49d9-b29a-2334b4753dee")
UTC = timezone.utc

DOCS = [  # (published name, source, title)
    ("PROGRESS.md", os.path.join(HERE, "PROGRESS.md"), "Progress log"),
    ("GOALS.md", os.path.join(HERE, "GOALS.md"), "Goals"),
    ("OPEN_QUESTIONS.md", os.path.join(HERE, "OPEN_QUESTIONS.md"), "Open questions"),
    ("ALERTS.md", os.path.join(HERE, "ALERTS.md"), "Alerts"),
    ("EXCEPTIONS.md", os.path.join(HERE, "EXCEPTIONS.md"), "Rule exceptions"),
    ("BACKLOG.md", os.path.join(HERE, "BACKLOG.md"), "Future projects (backlog)"),
    ("README.md", os.path.join(HERE, "README.md"), "README"),
    ("design-task-manager.md", DESIGN_DOC, "Design doc: task manager / MCP plan"),
]


def bstr(dt):
    return dt.astimezone(ka.BERLIN).strftime("%a %d.%m. %H:%M %Z") if dt else None


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def strip_frontmatter(text):
    if text and text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[text.find("\n", end + 1) + 1:]
    return text


def write_atomic(name, data):
    path = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)  # the container's nginx user must be able to read it
    os.replace(tmp, path)


# -------------------------------------------------------------------- GOALS.md

GOAL_RE = re.compile(r"^\d+\.\s*\[( |x|X|~|!)\]\s*(.+)$")
SIDE_RE = re.compile(r"^-\s*\[( |x|X|~|!)\]\s*(.+)$")


def parse_goals(md):
    """One entry per top-level goal line and per 'Side work' bullet, in
    GOALS.md order. marker is one of ' ', 'x', '~', '!'. Long entries are
    truncated to a single short line (first sentence / up to ~140 chars)."""
    out, in_side = [], False
    for line in (md or "").splitlines():
        if line.strip().lower().startswith("side work"):
            in_side = True
            continue
        m = SIDE_RE.match(line) if in_side else GOAL_RE.match(line)
        if not m:
            continue
        marker, text = m.group(1).lower(), m.group(2).strip()
        text = re.sub(r"\s+", " ", text)
        short = text.split(". Follow-up:")[0].split(": ", 1)
        short = short[0] if len(short) == 1 else text  # keep short "label: detail" lines intact
        if len(short) > 140:
            short = short[:137].rstrip() + "..."
        out.append({"marker": marker if marker in "x~!" else " ", "text": short, "side": in_side})
    return out


# ---------------------------------------------------------------- other sources

def needs_input():
    oq_md = read(os.path.join(HERE, "OPEN_QUESTIONS.md")) or ""
    open_questions = [re.sub(r"^\s*-\s*", "", l).strip()
                      for l in oq_md.splitlines() if re.match(r"^\s*-\s", l)]
    try:
        conn = store.connect()
        import stalled  # incremental transcript scan (~0.06 s) so stalled sessions + own flags are current
        stalled.scan(conn)
        inbox = store.pending_user_input(conn)
        blocked = [{"id": t.get("id"), "title": t.get("title"), "project": t.get("project_name"),
                    "question": t.get("blocked_question")} for t in inbox["blocked_tasks"]]
        undecided = [{"session_id": s.get("session_id"), "title": s.get("title"),
                      "cwd": s.get("cwd"), "kind": s.get("kind"),
                      "reset_at_berlin": bstr(ka.parse_ts(s.get("reset_at"))) if s.get("reset_at") else None}
                     for s in inbox["undecided_sessions"]]
        err = None
    except Exception as e:  # noqa: BLE001 - never let a DB hiccup break the export
        blocked, undecided, err = [], [], str(e)[:300]
    return {"open_questions": open_questions, "blocked_tasks": blocked,
            "error": err,
            # user rule 29.09.: only real questions + blocked tasks; stalled sessions are not shown here
            "count": len(open_questions) + len(blocked)}


def latest_decision():
    log = read(os.path.join(HERE, "keepalive.log")) or ""
    lines = [l for l in log.splitlines() if l.startswith("[")]
    if not lines:
        return None
    line = lines[-1]
    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
    ts, rest = (m.group(1), m.group(2)) if m else (None, line)
    full = f"{ts} {rest}" if ts else rest
    return full[:120]


def watcher_running():
    try:
        r = subprocess.run(["pgrep", "-f", "keepalive.py --session"], capture_output=True, text=True, timeout=10)
        pids = [p for p in r.stdout.split() if p.strip()]
        return {"running": bool(pids), "pids": pids}
    except OSError:
        return {"running": None, "pids": []}


def keepalive_and_usage(now):
    u = ka.read_usage_cache()
    out = {"watcher": watcher_running(), "tmux_ka_exists": ka.tmux_alive(SELF_SESSION),
           "next_window_berlin": bstr(ka.next_window_start(now))}
    rev = usage_review()
    out["next_usage_review_berlin"] = rev["next_run_berlin"] if rev else None
    if not u:
        out["usage_error"] = "no usage cache in ~/.claude.json"
        return out
    out["fetched_at_berlin"] = bstr(u["fetched_at"])
    out["age_minutes"] = round((now - u["fetched_at"]).total_seconds() / 60, 1)
    for k in ("session", "weekly"):
        if u.get(k):
            out[k] = {"percent": u[k]["percent"], "resets_at_berlin": bstr(u[k]["resets_at"])}
    go, reason = ka.budget_decision(u, now)
    out["budget_rule_now"] = {"continue": go, "reason": reason}
    return out


def usage_review():
    try:
        with open(os.path.join(HERE, "data", "usage_review_state.json")) as fh:
            st = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    nxt = ka.parse_ts(st.get("next_run_at"))
    runs = st.get("runs") or []
    return {"next_run_berlin": bstr(nxt), "runs": len(runs), "last_run": runs[-1] if runs else None}


def cron_entries():
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except OSError:
        return []
    out = []
    for line in r.stdout.splitlines():
        if "AFClaude" not in line or line.lstrip().startswith("#"):
            continue
        comment = line.split(" # ", 1)[1].strip() if " # " in line else ""
        if line.startswith("@"):
            sched = line.split()[0]
        else:
            sched = " ".join(line.split()[:5])
        out.append({"schedule_utc": sched, "schedule_berlin": cron_berlin(sched),
                    "what": comment or line[len(sched):].strip()[:120]})
    return out


def cron_berlin(sched):
    """Cron runs on the host clock (UTC); show the hours in Berlin time (current offset)."""
    f = sched.split()
    if len(f) != 5 or not re.fullmatch(r"[\d,]+", f[1]):
        return sched  # @reboot or an hour-independent schedule: same in any time zone
    off = int(datetime.now(ka.BERLIN).utcoffset().total_seconds() // 3600)
    hours = ",".join(str((int(h) + off) % 24) for h in f[1].split(","))
    return " ".join([f[0], hours] + f[2:]) + f" (Berlin, UTC+{off})"


def main():
    now = datetime.now(UTC)
    os.makedirs(OUT, exist_ok=True)
    os.chmod(OUT, 0o755)

    docs = []
    for name, src, title in DOCS:
        text = strip_frontmatter(read(src))
        if text is None:
            continue
        write_atomic(f"docs/{name}", text)
        docs.append({"name": name, "title": title, "path": f"docs/{name}",
                     "modified_berlin": bstr(datetime.fromtimestamp(os.path.getmtime(src), UTC))})
    prompts = []
    for src in sorted(glob.glob(os.path.join(HERE, "prompts", "*.md"))):
        name = os.path.basename(src)
        write_atomic(f"docs/prompts/{name}", read(src) or "")
        prompts.append({"name": name, "path": f"docs/prompts/{name}"})

    status = {
        "generated_at": now.isoformat(),
        "generated_berlin": bstr(now),
        "keepalive": keepalive_and_usage(now),
        "goals": parse_goals(read(os.path.join(HERE, "GOALS.md"))),
        "latest_decision": latest_decision(),
        "needs_input": needs_input(),
        "cron": cron_entries(),
        "docs": docs,
        "prompts": prompts,
    }
    write_atomic("status.json", json.dumps(status, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
