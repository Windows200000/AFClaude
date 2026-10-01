#!/usr/bin/env python3
"""Export a read-only snapshot of AFClaude's state for the quickview page.

Writes into data/quickview/ (gitignored), which the key-gated nginx in quickview/
serves at /afclaude/:

  status.json   keepalive+usage, progress (from GOALS.md, plus the project's
                stages from the task store as a phase strip), latest keep-alive
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
import limit_ratio  # noqa: E402  (reads the per-sample ratio snapshot; never recomputes here)
import afclaude_config  # noqa: E402  (local machine-specific values)

OUT = os.environ.get("QUICKVIEW_DIR", os.path.join(HERE, "data", "quickview"))
DESIGN_DOC = os.environ.get("QUICKVIEW_DESIGN_DOC", os.path.expanduser(
    "~/.claude/projects/-mnt-BlockVolume-Claude/memory/task_manager_mcp_plan.md"))
# This manager's own session/tmux (data/afclaude.json manager_session, as usage_report.py).
SELF_SESSION = os.environ.get("QUICKVIEW_SELF_SESSION") or afclaude_config.manager_session()
# The task-store project whose stages the Progress section shows as a phase strip.
STAGES_PROJECT = os.environ.get("QUICKVIEW_STAGES_PROJECT", "AFClaude")
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
EXCLUDE_DIRS = ("data", "run", "probe", ".git", ".venv", "prompts", "node_modules", "__pycache__")


def all_docs():
    """The fixed list plus EVERY other Markdown file in the repo (root, docs/, any
    subdir except runtime/vendored ones; prompts/ is published separately), so new
    documents surface automatically (owner's rule, 30.09.)."""
    out = list(DOCS)
    known = {os.path.realpath(src) for _, src, _ in DOCS}
    for src in sorted(glob.glob(os.path.join(HERE, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(src, HERE)
        if rel.split(os.sep)[0] in EXCLUDE_DIRS or os.path.realpath(src) in known:
            continue
        name = rel.replace(os.sep, "__")
        title = rel[:-3].replace("_", " ").replace("/", ": ")
        out.append((name, src, title))
    return out


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


# ------------------------------------------------------------ task-store stages

PHASE_RE = re.compile(r"^Dashboard\s+(\d+)([a-z]?)\s*:?\s*(.*)$", re.I)


def _stage(t):
    return {"id": t.get("id"), "seq": t.get("stage_seq"), "status": t.get("status"),
            "priority": t.get("priority"), "title": (t.get("title") or "")[:140]}


def _phase_status(steps):
    """Aggregate status of one phase's (non-cancelled) steps."""
    sts = [s["status"] for s in steps]
    if all(s == "done" for s in sts):
        return "done"
    if "blocked" in sts:
        return "blocked"
    if "in_progress" in sts or "done" in sts:
        return "in_progress"
    return "pending"


def build_stages(tasks):
    """Split a project's stages (every status, in execution order) into the
    'Dashboard N[x]: ...' phases and the other stages.

    phases: one entry per phase number N, ordered by N, with its steps (8a..8d
    are steps of phase 8). Cancelled steps are dropped from phases (they were
    superseded); a phase with only cancelled steps disappears. Exactly one
    phase (the first one not done) is marked current. others: everything else,
    in stage order, cancelled ones included (the page greys them)."""
    groups, others = {}, []
    for t in tasks:
        m = PHASE_RE.match(t.get("title") or "")
        if not m:
            others.append(_stage(t))
            continue
        if t.get("status") == "cancelled":
            continue
        n = int(m.group(1))
        st = _stage(t)
        st["key"] = f"{n}{m.group(2).lower()}"
        st["title"] = (m.group(3) or t.get("title") or "")[:140]
        groups.setdefault(n, []).append(st)
    phases = []
    for n in sorted(groups):
        steps = sorted(groups[n], key=lambda s: (s["key"], s["seq"] or 0))
        phases.append({"n": n, "status": _phase_status(steps), "current": False,
                       "done": sum(s["status"] == "done" for s in steps), "total": len(steps),
                       "steps": steps})
    cur = next((p for p in phases if p["status"] != "done"), None)
    if cur:
        cur["current"] = True
    return {"phases": phases, "others": others}


def stages():
    conn = None
    try:
        conn = store.connect()
        out = build_stages(store.list_tasks(conn, project=STAGES_PROJECT))
        out["error"] = None
    except Exception as e:  # noqa: BLE001 - never let a DB hiccup break the export
        out = {"phases": [], "others": [], "error": str(e)[:300]}
    finally:
        if conn is not None:
            conn.close()
    out["project"] = STAGES_PROJECT
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
    log = read(os.path.join(ka.STATE_DIR, "keepalive.log")) or ""
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


def tail_last_json(path, chunk=65536):
    """Last complete JSON object in a JSONL file, without reading the whole
    (potentially large, never-exported) file: grows the read window from the
    end until a full line is found or the file start is reached."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    with open(path, "rb") as fh:
        read = min(chunk, size)
        while True:
            fh.seek(size - read)
            data = fh.read(read)
            lines = data.split(b"\n")
            usable = lines[1:] if read < size else lines  # first entry may be a partial line
            for line in reversed(usable):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            if read >= size:
                return None
            read = min(read * 4, size)


def limits_block():
    """Compact version of the limit_ratio.py snapshot usage_sampler.py already
    stored on the latest data/samples.jsonl row (no recompute, no raw rows
    exported -- consistent with samples.jsonl never being exported directly)."""
    row = tail_last_json(os.path.join(HERE, "data", "samples.jsonl"))
    snap = (row or {}).get("limit_ratio")
    if not snap:
        return None
    r = snap.get("ratio") or {}
    share = (snap.get("attribution") or {}).get("week_share") or {}

    def r4(x):
        return round(x, 4) if x is not None else None

    def r1(x):
        return round(x, 1) if x is not None else None

    def r3(x):
        return round(x, 3) if x is not None else None

    return {
        "ratio_status": r.get("status"),
        "ratio_median": r4(r.get("median")),
        "ratio_trimmed_mean": r4(r.get("trimmed_mean")),
        "ratio_n": r.get("n"),
        "windows_per_week": r1(snap.get("windows_per_week")),
        "windows_left_this_week": r1(snap.get("windows_left_this_week")),
        "share_status": share.get("status"),
        "own_share_week": r3(share.get("own_share")),
        "user_share_week": r3(share.get("other_share")),
        "as_of": snap.get("generated_at"),
    }


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
    out["limits"] = limits_block()
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
    # In the manager container supercronic runs a crontab FILE (AFCLAUDE_CRONTAB);
    # there is no `crontab -l` there.
    if os.environ.get("AFCLAUDE_CRONTAB"):
        text = read(os.environ["AFCLAUDE_CRONTAB"]) or ""
    else:
        try:
            text = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
        except OSError:
            return []
    out = []
    for line in text.splitlines():
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
    for name, src, title in all_docs():
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
        "stages": stages(),
        "latest_decision": latest_decision(),
        "needs_input": needs_input(),
        "cron": cron_entries(),
        "docs": docs,
        "prompts": prompts,
    }
    write_atomic("status.json", json.dumps(status, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
