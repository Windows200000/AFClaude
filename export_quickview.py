#!/usr/bin/env python3
"""Export a read-only snapshot of AFClaude's state for the quickview page.

Writes into data/quickview/ (gitignored), which the key-gated nginx in quickview/
serves at /afclaude/:

  status.json   keepalive+usage (incl. when the next run takes place, pacing.next_run,
                the session fill time of AFClaude's runs, run_metrics.summary,
                the next planned fill-up run, D-212, from keepalive_fillup.json,
                and the reserve threshold and the model's accuracy,
                pacing.threshold_info), progress (from GOALS.md, plus the
                project's stages from the task store as a phase strip, and
                preview_from marking the step from which a non-blocking owner
                preview of the dashboard UI is possible, D-211; each open stage and
                phase with its cumulative ETA from stage_eta.py, D-213, which also
                logs one prediction per day (the DB, stage_eta_log) for the
                rare review), latest keep-alive
                decision, the newest progress log entries (the progress doc's appends
                in the DB, progress_log), needs-your-input (OPEN_QUESTIONS.md, each item
                one markdown string, + task-store inbox), page_version (the published
                page's stamp: a page that doesn't match is an old copy and says so),
                the owner's message box to the task-manager (counts only:
                the page reads the messages from the inbox API, D-241), what is scheduled (the AFClaude cron lines, plus the resident
                runner's jobs from runner_jobs while it runs), the task-manager's context (D-237:
                size, cache expiry, the next re-read's cost, a daily split; the compaction
                attempts of D-238; compact_timing.context_block), each project's
                task-manager model / effort (D-260, manager_launch.py; data only, the page
                has no settings area yet), the Remote Control setting of each session
                category (D-269, remote_control.py; data only too)
  docs/*.md     PROGRESS, GOALS, OPEN_QUESTIONS, ALERTS, EXCEPTIONS, BACKLOG,
                README, design doc, prompts/*.md; PROGRESS, GOALS, ALERTS and EXCEPTIONS
                (and the goal list) come from the project docs in the DB (design §7.7:
                the runners' notes and alerts are only there), the file if the DB has none

Never exports the usage samples or the Haiku judgements (raw usage/message
details); only small aggregates. Never calls /usage: usage comes from the
~/.claude.json cache that the sampler keeps fresh. All times Europe/Berlin.
Also publishes the page itself (quickview/AFClaude.html -> AFClaude.html, D-253).
Run every 3 min (the runner's quickview job, or cron in the legacy mode; via flock); each
file is replaced atomically.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (pure helpers only: cache read, budget evaluation, window)
import store  # noqa: E402  (read-only: pending_user_input)
import limit_ratio  # noqa: E402  (reads the per-sample ratio snapshot; never recomputes here)
import afclaude_config  # noqa: E402  (local machine-specific values)
import stage_eta  # noqa: E402  (stage ETAs, D-213: read-only, plus its once-a-day prediction log)

OUT = os.environ.get("QUICKVIEW_DIR", os.path.join(HERE, "data", "quickview"))
# D-253: the page itself, published next to status.json on every export (served at /afclaude/AFClaude.html)
PAGE = os.environ.get("QUICKVIEW_PAGE", os.path.join(HERE, "quickview", "AFClaude.html"))
USAGE_REVIEW_STATE_FILE = os.path.join(HERE, "data", "usage_review_state.json")
AT_SPOOL_DIR = os.path.join(HERE, "data", "at_spool")   # legacy (frozen): jobs the at-shim adopts
DESIGN_DOC = os.environ.get("QUICKVIEW_DESIGN_DOC", os.path.expanduser(
    "~/.claude/projects/-mnt-BlockVolume-Claude/memory/task_manager_mcp_plan.md"))
# The task-store project whose stages the Progress section shows as a phase strip.
STAGES_PROJECT = os.environ.get("QUICKVIEW_STAGES_PROJECT", "AFClaude")
# This manager's own session/tmux: the task-manager of the project STAGES_PROJECT in the registry
# (projects.manager_session, phase 3b S3; the machine setting is not read). A display path (D-239): a
# project without a task-manager session, or an unreadable registry, must not crash the export (no
# page update, no alert, every 3 min); it is shown on the page instead (status.json self_session_error),
# and every SELF_SESSION-dependent field below degrades to unknown rather than raising.


def registry_self_session(project):
    """The task-manager session of `project` (name or id) from the registry. Raises ValueError."""
    conn = store.connect()
    try:
        p = store.get_project(conn, project)
    finally:
        conn.close()
    if p is None:
        raise ValueError(f"no project {project!r} in the registry")
    if not p.get("manager_session"):
        raise ValueError(f"project {p['name']!r} has no task-manager session yet")
    return p["manager_session"]


try:
    SELF_SESSION = os.environ.get("QUICKVIEW_SELF_SESSION") or registry_self_session(STAGES_PROJECT)
    SELF_SESSION_ERROR = None
except Exception as e:   # noqa: BLE001 - a display path: shown, never raised (D-239)
    SELF_SESSION = None
    SELF_SESSION_ERROR = f"{type(e).__name__}: {e}"[:300]
# D-211: the step key (as build_stages() keys steps, e.g. "6a") from which a non-blocking owner
# preview build of the dashboard UI first becomes possible (docs/dashboard_design/10_deployment.md §10.9). A
# constant for now; swap for a task-store marker if that step ever moves without a doc edit.
PREVIEW_FROM = os.environ.get("QUICKVIEW_PREVIEW_FROM", "6a")
UTC = timezone.utc
RUNS_FILE = os.path.join(HERE, "data", "afclaude_runs.jsonl")   # names run_metrics.py's rows: the DB (D-161)

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
# published name -> the project doc it shows (the DB's text; the file only if the DB has none)
DB_DOCS = {"PROGRESS.md": "progress", "GOALS.md": "goals", "ALERTS.md": "alerts", "EXCEPTIONS.md": "exceptions"}
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


def db_doc(name):
    """(text, updated_at as datetime) of this repo's project doc `name` in the DB; (None, None) if
    there is none or the DB can't be read (the caller falls back to the file)."""
    try:
        conn = store.connect()
        try:
            p = store.get_project(conn, HERE)
            d = store.get_doc(conn, p["id"], name) if p is not None else None
            if d is None:
                return None, None
            return store.doc_render(conn, d["id"]), datetime.fromisoformat(d["updated_at"].replace("Z", "+00:00"))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a status page: show the file instead
        return None, None


def doc_source(name, src):
    """(text, modified datetime) of a published doc: the DB's for DB_DOCS, else (or if the DB has
    none) the file's; (None, None) if neither exists."""
    if name in DB_DOCS:
        text, at = db_doc(DB_DOCS[name])
        if text is not None:
            return text, at
    text = read(src)
    return (text, datetime.fromtimestamp(os.path.getmtime(src), UTC)) if text is not None else (None, None)


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


# Stamped into the published page (its version: the source's sha256, 12 hex): status.json carries the
# same version, so a copy that isn't the current page (e.g. served from elsewhere instead of proxied,
# D-253) shows a banner instead of rendering new data with old code.
PAGE_VERSION_MARK = "__AFC_PAGE_VERSION__"


def read_page():
    """D-253: (page text with its version stamped in, version). A page that can't be read or has no
    stamp raises (the export fails, never publishes without it)."""
    with open(PAGE, encoding="utf-8") as fh:
        page = fh.read()
    if "status.json" not in page or PAGE_VERSION_MARK not in page:
        raise ValueError(f"{PAGE} does not look like the quickview page (no status.json or version mark in it)")
    version = hashlib.sha256(page.encode("utf-8")).hexdigest()[:12]
    return page.replace(PAGE_VERSION_MARK, version), version


def publish_page(page=None):
    """D-253: write the (stamped) page to OUT/AFClaude.html, so ovm3 proxies the current page instead
    of a copy made by hand."""
    if page is None:
        page = read_page()[0]
    write_atomic("AFClaude.html", page)


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


def _stage(t, eta=None):
    out = {"id": t.get("id"), "seq": t.get("stage_seq"), "status": t.get("status"),
           "priority": t.get("priority"), "title": (t.get("title") or "")[:140]}
    e = stage_eta.group_eta(eta, [t.get("id")]) if eta else None
    if e is not None:
        out.update(eta=e["eta"], eta_days=e["eta_days"], eta_sessions=e["sessions"])
    return out


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


def build_stages(tasks, eta=None):
    """Split a project's stages (every status, in execution order) into the
    'Dashboard N[x]: ...' phases and the other stages.

    phases: one entry per phase number N, ordered by N, with its steps (8a..8d
    are steps of phase 8). Cancelled steps are dropped from phases (they were
    superseded); a phase with only cancelled steps disappears. Exactly one
    phase (the first one not done) is marked current. others: everything else,
    in stage order, cancelled ones included (the page greys them).

    eta = a stage_eta.predict() result: every open stage gets its cumulative ETA (eta "~3 d" |
    "<1 d" | "?", eta_days, eta_sessions), a phase the ETA of its last open step (D-213)."""
    groups, others = {}, []
    for t in tasks:
        m = PHASE_RE.match(t.get("title") or "")
        if not m:
            others.append(_stage(t, eta))
            continue
        if t.get("status") == "cancelled":
            continue
        n = int(m.group(1))
        st = _stage(t, eta)
        st["key"] = f"{n}{m.group(2).lower()}"
        st["title"] = (m.group(3) or t.get("title") or "")[:140]
        groups.setdefault(n, []).append(st)
    phases = []
    for n in sorted(groups):
        steps = sorted(groups[n], key=lambda s: (s["key"], s["seq"] or 0))
        ph = {"n": n, "status": _phase_status(steps), "current": False,
              "done": sum(s["status"] == "done" for s in steps), "total": len(steps), "steps": steps}
        e = stage_eta.group_eta(eta, [s["id"] for s in steps]) if eta else None
        if e is not None:
            ph.update(eta=e["eta"], eta_days=e["eta_days"], eta_sessions=e["sessions"])
        phases.append(ph)
    cur = next((p for p in phases if p["status"] != "done"), None)
    if cur:
        cur["current"] = True
    return {"phases": phases, "others": others}


def stages(usage=None, now=None):
    conn = None
    eta = None
    try:
        conn = store.connect()
        eta = eta_result(conn, usage, now or datetime.now(UTC))
        out = build_stages(store.list_tasks(conn, project=STAGES_PROJECT), eta if eta.get("stages") else None)
        out["error"] = None
    except Exception as e:  # noqa: BLE001 - never let a DB hiccup break the export
        out = {"phases": [], "others": [], "error": str(e)[:300]}
    finally:
        if conn is not None:
            conn.close()
    out["project"] = STAGES_PROJECT
    out["eta"] = eta_summary(eta)
    return out


def eta_result(conn, usage, now):
    """stage_eta.gather() over every open stage (all projects: the execution order interleaves
    them), logged once a day (the DB, stage_eta_log) for the stage-ETA review. Never raises."""
    try:
        res = stage_eta.gather(conn, usage, now, runs_path=RUNS_FILE)
    except Exception as e:   # noqa: BLE001 - the page shows "?" and the error instead
        return {"status": "unknown", "reason": f"{type(e).__name__}: {e}"[:300], "stages": [], "assumptions": {}}
    try:
        stage_eta.record(res, now=now)
    except OSError:
        pass
    return res


def eta_summary(eta):
    """The ETA assumptions for the page (the tooltip), small aggregates only."""
    if not eta:
        return None
    a = eta.get("assumptions") or {}
    return {"status": eta.get("status"), "reason": eta.get("reason"), "text": stage_eta.assumptions_text(eta),
            "sessions_per_day": a.get("sessions_per_day"), "gate_sessions_per_week": a.get("gate_sessions_per_week"),
            "formula_sessions_per_week": a.get("formula_sessions_per_week"),
            "night_sessions_per_week": a.get("night_sessions_per_week"),
            "design_sessions_per_week": a.get("design_sessions_per_week"),
            "user_forecast_week": a.get("user_forecast_week"), "design_forecast_week": a.get("design_forecast_week"),
            "estimate_sources": a.get("estimate_sources"), "stage_share": a.get("stage_share"),
            "stage_share_info": a.get("stage_share_info")}


# ---------------------------------------------------------------- other sources

_MD_FILE_RE = re.compile(r"\b[\w./-]*\.md\b")
_MD_POINTER_WORDS_RE = re.compile(r"\b(see|cf\.?|details?( in)?|refer to|in)\b", re.IGNORECASE)


def _split_open_questions(oq_md):
    """Top-level '- ' items of OPEN_QUESTIONS.md, each as its list of raw lines (the first line
    with the leading '- ' stripped). A line is part of the current item unless it starts a new
    top-level item (a '-' with no leading indentation); indented continuation lines and nested
    numbered/bulleted sub-items (D-279: a multi-line item must not collapse to its first line)
    stay with it."""
    items, cur = [], None
    for line in oq_md.splitlines():
        if re.match(r"^-\s+\S", line):
            if cur is not None:
                items.append(cur)
            cur = [re.sub(r"^-\s+", "", line).rstrip()]
        elif cur is not None:
            cur.append(line.rstrip())
        # lines before the first top-level item (title, blank, comments) aren't an item
    if cur is not None:
        items.append(cur)
    return items


def _open_question_text(lines):
    """lines (as from _split_open_questions) joined into markdown: the header line, then - if
    there are continuation lines - a blank line and the continuation dedented to its own
    shallowest indent, so a nested list under it still parses as a list rather than being eaten
    as lazy paragraph continuation."""
    head, rest = lines[0], lines[1:]
    while rest and rest[-1].strip() == "":
        rest.pop()
    if not rest:
        return head
    indents = [len(l) - len(l.lstrip()) for l in rest if l.strip()]
    cut = min(indents) if indents else 0
    rest = ["" if not l.strip() else l[cut:] for l in rest]
    return head + "\n\n" + "\n".join(rest)


def _open_question_warning(lines):
    """D-279 safety net: flag an item whose only real content is a pointer to a .md file (the
    original bug - a header naming a doc with the actual questions left out of OPEN_QUESTIONS.md
    entirely) so it's caught instead of silently showing an empty-looking item."""
    full = " ".join(l.strip() for l in lines if l.strip())
    if not _MD_FILE_RE.search(full):
        return None
    rest = _MD_FILE_RE.sub("", full)
    rest = _MD_POINTER_WORDS_RE.sub("", rest)
    rest = re.sub(r"[\s:.,()*_\-]+", "", rest)
    if len(rest) < 20:
        return "points mainly to a file instead of stating the question here"
    return None


def _open_question_md(lines):
    """One open question as one markdown string (D-279: in full); a warning goes into the text."""
    text = _open_question_text(lines)
    warning = _open_question_warning(lines)
    return text + "\n\n**⚠ " + warning + "**" if warning else text


def needs_input():
    oq_md = read(os.path.join(HERE, "OPEN_QUESTIONS.md")) or ""
    open_questions = [_open_question_md(lines) for lines in _split_open_questions(oq_md)]
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


PROGRESS_ENTRIES = 8   # the newest progress log entries the page shows
PROGRESS_ENTRY_CHARS = 300   # D-082: short lines; the full log is the PROGRESS.md doc


def progress_log():
    """The newest appends to this project's progress doc in the DB (where `tasks.py doc append
    progress` and the runners write), newest first: at_berlin, by, text (the first line, cut short).
    A display path: a DB error is shown on the page, never raised."""
    try:
        conn = store.connect()
        try:
            p = store.get_project(conn, HERE)
            d = store.get_doc(conn, p["id"], "progress") if p is not None else None
            rows = store.doc_entries(conn, d["id"], 4 * PROGRESS_ENTRIES) if d is not None else []
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - shown on the page, never breaks the export
        return {"entries": [], "error": f"{type(e).__name__}: {str(e)[:300]}"}
    out = []
    for r in reversed(rows):
        if r["op"] != "append":
            continue   # a replace rewrites the whole doc: not a log entry
        lines = [l.strip() for l in r["body"].splitlines() if l.strip()]
        if not lines:
            continue
        text = re.sub(r"^-\s+", "", lines[0])
        if len(lines) > 1 or len(text) > PROGRESS_ENTRY_CHARS:
            text = text[:PROGRESS_ENTRY_CHARS].rstrip() + " …"
        out.append({"at_berlin": bstr(ka.parse_ts(r["ts"])), "by": r["author"], "text": text})
        if len(out) == PROGRESS_ENTRIES:
            break
    return {"entries": out, "error": None}


def inbox_block():
    """The owner's message box (D-178/D-241): only the counts. The page reads the messages live from
    the inbox API (/afclaude/api/inbox, inbox_api.py); their texts stay in the DB, the only copy."""
    try:
        import inbox
        conn = store.connect()
        try:
            return inbox.summary(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - shown on the page, never breaks the export
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def automation_block():
    """The automation pause (D-234/D-235) for the page's banner: a runner sets it on any failure;
    no run starts until the owner clears it. Strict read (a DB that can't be read = paused).
    paused: anything is paused; install: the install-wide pause (reason, by, since_berlin, resume
    are its own); projects: each paused project (D-244: an inbox error pauses only its project)
    with its name, reason, by, since_berlin and resume command."""
    import failstop
    ps = failstop.pauses()
    i = ps["install"]
    projects = [{"name": p["name"], "reason": p.get("reason"), "by": p.get("by"),
                 "since_berlin": bstr(ka.parse_ts(p["since"])) if p.get("since") else None,
                 "resume": failstop.resume_cmd(p["name"])} for p in ps["projects"]]
    return {"paused": bool(i["paused"]) or bool(projects), "install": bool(i["paused"]),
            "reason": i.get("reason"), "by": i.get("by"),
            "since_berlin": bstr(ka.parse_ts(i["since"])) if i.get("since") else None,
            "resume": failstop.RESUME_CMD, "projects": projects}


def manager_launch_block():
    """D-260: the code default and each project's task-manager launch (manager_launch.view: model,
    effort, source project|default per value). A display path: an error is shown, never raised."""
    import manager_launch
    try:
        import store
        conn = store.connect()
        try:
            projects = [dict(manager_launch.view(p), name=p["name"])
                        for p in store.list_projects(conn)]
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"default": {"model": manager_launch.DEFAULT_MODEL, "effort": manager_launch.DEFAULT_EFFORT},
            "projects": projects}


def remote_control_block():
    """D-269: whether each session category is launched with Remote Control (remote_control.view:
    enabled, source db|default|invalid: <why>, setting, covers). A display path: an error is shown,
    never raised."""
    import remote_control
    try:
        conn = store.connect()
        try:
            return remote_control.view(conn)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"[:300]}


COMPACTIONS_SHOWN = 5


def context_block(now):
    """D-237: the task-manager's context (compact_timing.context_block): its size, when its prompt
    cache expires and what the next message would re-read, a daily split (re-reads / warm reads /
    writes / output), the compaction threshold (D-307) and the cost model's break-even, and the
    latest compaction attempts (D-238, the keepalive state's "compactions"). The per-message list
    is dropped: the owner keeps only the daily split (owner, 25.10.). A display path: an error is
    shown, never paused (D-239)."""
    try:
        import compact_timing
        out = compact_timing.context_block(ka.transcript_path(SELF_SESSION), now,
                                           threshold=int(ka._compaction_settings()["compaction_threshold_tokens"]))
        out.pop("messages", None)
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    try:
        comps = [dict(v, key=k) for k, v in (ka.load_state().get("compactions") or {}).items()
                 if isinstance(v, dict) and v.get("session") in (None, SELF_SESSION)]
        comps.sort(key=lambda c: str(c.get("at")), reverse=True)
        out["compactions"] = [{"at_berlin": bstr(ka.parse_ts(c["at"])) if c.get("at") else None,
                               **{k: c.get(k) for k in ("status", "trigger", "reason", "context", "threshold",
                                                        "break_even", "est_cost_pct", "saves_per_reread_pct", "error",
                                                        "last_hold")}}
                              for c in comps[:COMPACTIONS_SHOWN]]
    except Exception as e:   # noqa: BLE001 - shown on the page
        out["compactions_error"] = f"{type(e).__name__}: {e}"[:300]
    return out


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


WATCH_STALE = timedelta(minutes=5)   # the runner's watch pass (S6) last ran ok this long ago: not running


def watcher_running(conn=None, now=None):
    """The keep-alive watcher: its process (pgrep), or while the resident runner runs its passes
    (AFCLAUDE_SCHEDULER on/runner, phase 3a S6) the runner's `watch` job: running = its last ok run
    is younger than WATCH_STALE ({"running", "pids": [], "by": "runner", "last_ok_berlin"}). A
    read that fails is running None (unknown), never raised."""
    if os.environ.get("AFCLAUDE_SCHEDULER", "off") in RUNNER_MODES:
        now = now or datetime.now(UTC)
        try:
            own = conn is None
            conn = conn or store.connect()
            try:
                row = store.get_runner_job(conn, "watch") or {}
            finally:
                if own:
                    conn.close()
        except Exception:   # noqa: BLE001 - a display path: unknown
            return {"running": None, "pids": [], "by": "runner"}
        ok = store.parse_iso(row.get("last_ok_at")) if row.get("last_ok_at") else None
        return {"running": bool(ok and now - ok <= WATCH_STALE), "pids": [], "by": "runner",
                "last_ok_berlin": bstr(ok) if ok else None}
    try:
        r = subprocess.run(["pgrep", "-f", "keepalive.py --session"], capture_output=True, text=True, timeout=10)
        pids = [p for p in r.stdout.split() if p.strip()]
        return {"running": bool(pids), "pids": pids}
    except OSError:
        return {"running": None, "pids": []}


def tail_last_json(path, chunk=65536):
    """Last complete JSON object in a JSONL file, without reading the whole
    (potentially large, never-exported) file: grows the read window from the
    end until a full line is found or the file start is reached. The live path's rows come
    from the DB (phase 2c, telemetry.py, D-161: the DB is the only copy; an unreadable DB
    gives None, never the frozen legacy file)."""
    import telemetry
    got = telemetry.db_lines(path, max_bytes=chunk * 16)
    if got is not None:
        for line in reversed(got):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None
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
    stored on the latest usage sample (the DB; no recompute, no raw rows
    exported -- consistent with the samples never being exported directly)."""
    row = tail_last_json(os.path.join(HERE, "data", "samples.jsonl"))
    return limits_from_snapshot((row or {}).get("limit_ratio"))


def limits_from_snapshot(snap):
    """The page's limits block from one stored limit_ratio snapshot (pure)."""
    if not snap:
        return None
    r = snap.get("ratio") or {}
    rw = snap.get("ratio_windows") or {}
    pref = snap.get("preferred_ratio") or {}
    share = (snap.get("attribution") or {}).get("week_share") or {}
    tok = (snap.get("attribution") or {}).get("week_share_tokens") or {}
    split = (snap.get("attribution") or {}).get("week_split")
    if split:   # pacing's three-way split (D-266: design chats are AFClaude's); older snapshots: the time split
        share = split

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
        # "time": weekly % risen during autonomous AFClaude runs vs everything else (the owner's
        # chats with the task-manager and other devices are user usage); absent = an older
        # snapshot whose share was the token split by session (it counted those chats as AFClaude)
        "share_method": share.get("method") or ("tokens" if share.get("status") else None),
        "own_pct_week": r1(share.get("own_pct")),
        "user_pct_week": r1(share.get("user_pct")),
        "user_unseen_pct_week": r1(share.get("user_unseen_pct")),
        "runs_week": share.get("runs_in_cycle"),
        "run_hours_week": r1(share.get("run_hours")),
        # share_method "pacing" (pacing.cycle_split): AFClaude = runs + design chats, user = other use
        "run_pct_week": r1(share.get("run_pct")),
        "design_pct_week": r1(share.get("design_pct")),
        "other_pct_week": r1(share.get("other_pct")),
        "other_unseen_pct_week": r1(share.get("other_unseen_pct")),
        "total_pct_week": r1(share.get("total_pct")),
        "uncovered_pct_week": r1(share.get("uncovered_pct")),
        "afc_session_token_share_week": r3(tok.get("own_share")) if tok.get("status") == "ok" else None,
        "as_of": snap.get("generated_at"),
        # per-session-window estimate (limit_ratio.estimate_window_ratio) and the
        # ratio consumers should use; absent in snapshots older than this field
        "ratio_pref": r4(pref.get("value")),
        "ratio_pref_source": pref.get("source"),
        "ratio_pref_flagged": pref.get("flagged"),
        "ratio_spread": r4(rw.get("weighted_stdev")),
        "ratio_iqr": [r4(rw.get("p25")), r4(rw.get("p75"))] if rw.get("p25") is not None else None,
        "ratio_se": r4(rw.get("se")),
        "ratio_windows_n": rw.get("n"),
        "ratio_windows_total": rw.get("n_total"),
        "ratio_15min_median": r4(((rw.get("vs_15min") or {}).get("median_15min"))),
        "windows_per_week_pref": r1(pref.get("windows_per_week")),
        "windows_left_pref": r1(pref.get("windows_left_this_week")),
    }


def window_info(now):
    """The automation window for the page (schedule.py): the current window's wall-clock span in
    window_tz (else the next one's), whether `now` is inside, the current window's end, and the
    NEXT window's start (after the current one if inside). No window on any day: "off"."""
    import schedule
    cfg = schedule.load()
    cur = schedule.current_window(now, cfg)
    show = cur or schedule.next_window(now, cfg)
    nxt = schedule.next_window(cur.start if cur else now, cfg)
    return {"window_berlin": show.span() if show else "off",
            "window_tz": cfg.tz,
            "in_window": cur is not None,
            "window_end_berlin": bstr(cur.end) if cur else None,
            "next_window_berlin": bstr(nxt.start) if nxt else None}


def threshold_block(now, decision=None):
    """pacing.threshold_info(): the reserve threshold (dynamic default vs override), the
    session/weekly ratio +- its spread, the full-session cost +- its uncertainty and the
    prediction model's back-calculated error (small aggregates only, never raw samples)."""
    try:
        import pacing
        d = decision if decision and decision.get("mode") in ("night", "last_mile") else None
        return pacing.threshold_info(now=now, decision=d)
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"}


def runner_state(now):
    """What the runner is doing for this manager session (D-202): a run going (the fire time), a
    pending postponed session-window start (its recheck) and, in the last stretch, whether the
    current slot already ran (keepalive.last_mile_next_slot(), D-204). Read-only; unknown -> None."""
    out = {"active": None, "deferred": None, "slot_next": None}
    try:
        out["active"] = ka.run_active(SELF_SESSION, now)
    except Exception:   # noqa: BLE001 - advisory: the walk still shows the next start
        pass
    try:
        p = ka.pending_deferral(SELF_SESSION, now)
        out["deferred"] = p if p is not None else False      # False = known: none pending
    except Exception:   # noqa: BLE001 - None = unknown: inferred from the decision
        pass
    try:
        out["slot_next"] = ka.last_mile_next_slot(now)
    except Exception:   # noqa: BLE001 - None = unknown: the decision decides
        pass
    return out


def next_run_block(u, now, decision=None):
    """pacing.next_run(): when the next autonomous run takes place (D-166: the page shows this,
    not the budget rule): Berlin time, kind, a one-line reason. Only what the runner will
    actually do (D-202): "now" only while a run is going, in the last stretch, or when a
    session-window start's check is due and passes. The main result assumes no more usage
    (D-200); "expected" = the same with the user's forecast usage."""
    try:
        import pacing
        d = pacing.next_run(u, now, decision, **runner_state(now))
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"}
    r = lambda x: round(x, 1) if isinstance(x, (int, float)) else None   # noqa: E731
    e = d.get("expected") or {}
    return {"at_berlin": bstr(d.get("at")), "kind": d.get("kind"), "label": d.get("label"),
            "reason": d.get("reason"), "predicted_end": r(d.get("predicted_end")),
            "threshold": r(d.get("threshold")), "last_stretch_berlin": bstr(d.get("last_stretch_at")),
            "expected": {"at_berlin": bstr(e.get("at")), "kind": e.get("kind"), "label": e.get("label"),
                         "reason": e.get("reason"), "predicted_end": r(e.get("predicted_end"))}
            if e else None}


def tmux_block():
    """The task-manager's tmux session: its stored name (driven_sessions, 3b S2) and whether it
    runs. Advisory (D-239): no manager session, no stored name or a failed check is shown as
    unknown (tmux_ka_exists None) with tmux_error, never as a computed name and never raised."""
    if not SELF_SESSION:
        return {"tmux_name": None, "tmux_ka_exists": None, "tmux_error": "no manager session configured"}
    try:
        name = ka.tmux_name(SELF_SESSION)
    except ka.NoTmuxName:
        return {"tmux_name": None, "tmux_ka_exists": None, "tmux_error": "no stored tmux name (driven_sessions)"}
    except Exception as e:   # noqa: BLE001 - advisory: unknown, not raised (D-239)
        return {"tmux_name": None, "tmux_ka_exists": None, "tmux_error": f"tmux name unreadable: {e}"[:200]}
    try:
        return {"tmux_name": name, "tmux_ka_exists": ka.tmux_has(name)}
    except Exception as e:   # noqa: BLE001 - advisory: unknown, not raised (D-239)
        return {"tmux_name": name, "tmux_ka_exists": None, "tmux_error": f"tmux check failed: {e}"[:200]}


def keepalive_and_usage(now):
    u = ka.read_usage_cache()
    out = {"watcher": watcher_running(), **tmux_block(), **window_info(now)}
    nr = next_review_block()
    out["next_usage_review_berlin"] = nr["at_berlin"]
    out["next_usage_review_which"] = nr["which"]
    out["next_usage_review_label"] = nr["label"]
    out["next_usage_review_secondary"] = nr["secondary"]
    out["fill"] = fill_block()
    out["fillup"] = fillup_block(now)
    if not u:
        out["usage_error"] = "no usage cache in ~/.claude.json"
        out["next_run"] = next_run_block(None, now)
        out["threshold"] = threshold_block(now)
        return out
    out["fetched_at_berlin"] = bstr(u["fetched_at"])
    out["age_minutes"] = round((now - u["fetched_at"]).total_seconds() / 60, 1)
    for k in ("session", "weekly"):
        if u.get(k):
            out[k] = {"percent": u[k]["percent"], "resets_at_berlin": bstr(u[k]["resets_at"])}
    d = ka.budget_eval(u, now, display=True)      # a display path never pauses (D-234/D-235)
    if d.get("mode") == "failed":
        out["budget_error"] = d["reason"]             # shown prominently on the page
    out["next_run"] = next_run_block(u, now, d)
    out["limits"] = limits_block()
    out["threshold"] = threshold_block(now, d)
    return out


def fill_block():
    """How long AFClaude's runs take to fill the session limit (D-207, run_metrics.summary over
    the run rows in the DB, which the sampler keeps): the last run and the median."""
    try:
        import run_metrics
        return run_metrics.summary(run_metrics.read_rows(RUNS_FILE))
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"error": f"{type(e).__name__}: {e}"}


FILLUP_PLANNED = ("wait", "hold", "fire")   # fillup.decide statuses with a start ahead


def _r1(x):
    return round(float(x), 1) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _ts(x):
    try:
        return ka.parse_ts(x) if isinstance(x, str) else None
    except ValueError:
        return None


def fillup_block(now):
    """The next planned fill-up run (D-212), from the watcher's plan in keepalive_fillup.json
    (fillup.decide, rewritten by the watcher every minute) or a --plan-fillup-test entry of this
    session: {"planned": True, "at_berlin", "remaining" (session %), "cost" (weekly %),
    "reset_berlin", "status", "reason", "test"} or {"planned": False, "reason"}. Read-only."""
    try:
        f = ka.load_fillup()
        t = f.get("test")
        if isinstance(t, dict) and t.get("session") == SELF_SESSION:
            start, reset = _ts(t.get("start")), _ts(t.get("reset"))
            if start and reset and reset > now:
                return {"planned": True, "test": True, "at_berlin": bstr(start), "remaining": _r1(t.get("remaining")),
                        "cost": _r1(t.get("cost")), "reset_berlin": bstr(reset), "status": "wait",
                        "reason": "TEST fill-up (--plan-fillup-test)"}
        if not afclaude_config.setting("fillup_enabled"):
            return {"planned": False, "reason": "fillup_enabled is off"}
    except Exception as e:   # noqa: BLE001 - the page shows the error instead
        return {"planned": False, "reason": f"{type(e).__name__}: {e}"}
    p = f.get("plan")
    if not isinstance(p, dict):
        return {"planned": False, "reason": "no plan yet (the watcher writes it every minute)"}
    reset, start = _ts(p.get("reset")), _ts(p.get("start"))
    if reset is not None and reset <= now:
        return {"planned": False, "reason": "that session window has ended (no newer plan from the watcher)"}
    if p.get("status") in FILLUP_PLANNED and start is not None:
        return {"planned": True, "test": False, "at_berlin": bstr(start), "remaining": _r1(p.get("remaining")),
                "cost": _r1(p.get("cost")), "reset_berlin": bstr(reset), "status": p.get("status"),
                "reason": p.get("reason")}
    return {"planned": False, "reason": str(p.get("reason") or p.get("status") or "unknown")}


def usage_review():
    try:
        import runner_state
        st = runner_state.read_json(USAGE_REVIEW_STATE_FILE)
    except (OSError, ValueError):
        return None
    nxt = ka.parse_ts(st.get("next_run_at"))
    runs = st.get("runs") or []
    return {"next_run_at": nxt, "next_run_berlin": bstr(nxt), "runs": len(runs),
            "last_run": runs[-1] if runs else None}


ONE_OFF_LABELS = {"usage": "usage", "stage-eta": "ETA"}    # usage_review.py's ONLY_PROMPTS keys


def _review_label(which):
    """'usage' -> 'usage'; 'stage-eta' -> 'ETA'; 'usage,stage-eta' (D-228 chain) -> 'usage + ETA'."""
    return " + ".join(ONE_OFF_LABELS.get(w, w) for w in which.split(","))


def one_off_reviews():
    """Pending one-off usage/ETA reviews (owner's `usage_review.py --only ... --at ...`, D-220/
    D-221/D-228): the at-shim's pending jobs in scheduled_jobs (the DB is their only copy,
    D-161), plus a legacy spool file (data/at_spool, frozen) whose job the DB doesn't know yet:
    the at-shim adopts it as pending on its next run (docker/at_shim.py). A DB that can't be
    read: none. Earliest first. -> [{"at": datetime, "at_berlin", "which", "label"}, ...] (jobs
    whose cmd isn't a usage_review.py --only call are ignored)."""
    import runner_state
    try:
        every = runner_state.jobs()
    except Exception:   # noqa: BLE001 - no DB, or a hiccup: no one-off shown (never a file)
        return []
    known = {str(r.get("id")) for r in every}
    rows = [r for r in every if r.get("status") == "pending"]
    rows += [{"run_at": j.get("at"), "cmd": j.get("cmd")} for j in runner_state.legacy_jobs(AT_SPOOL_DIR)
             if str(j.get("id")) not in known and isinstance(j.get("cmd"), str)]
    out = []
    for r in rows:
        m = re.search(r"usage_review\.py\s+--only\s+(\S+)", r.get("cmd") or "")
        if not m:
            continue
        at = ka.parse_ts(r.get("run_at"))
        if at is None:
            continue
        which = m.group(1)
        out.append({"at": at, "at_berlin": bstr(at), "which": which, "label": _review_label(which)})
    out.sort(key=lambda j: j["at"])
    return out


def nightly_reviews():
    """The pending nightly tasks (nightly_tasks.py, D-254/D-261: the monthly review once due, a
    one-off like the ETA review's re-run), done in the first nightly run from their due time. A
    DB that can't be read: none (as one_off_reviews). Earliest first. -> [{"at", "at_berlin", "label"}]."""
    try:
        import nightly_tasks
        tasks = nightly_tasks.load()["tasks"].values()
    except Exception:   # noqa: BLE001 - no DB, or a hiccup: none shown (the runners report it)
        return []
    out = []
    for t in tasks:
        at = ka.parse_ts(t.get("due_from")) if isinstance(t, dict) and t.get("status") == "pending" else None
        if at is not None:
            out.append({"at": at, "at_berlin": bstr(at), "label": f"{t.get('title')} (nightly run)"})
    out.sort(key=lambda j: j["at"])
    return out


def next_review_block():
    """The next usage review for the quickview tile (owner bug report: the monthly schedule
    alone ignores a one-off review the owner asked for, which can land first). The EARLIEST of
    the monthly schedule (usage_review_state.json's next_run_at), any pending one-off job and any
    pending nightly task (D-254/D-261, its due time), tagged which one it is; the other one (if any), as a secondary line. ->
    {"at_berlin", "which": "monthly"|"one-off"|"nightly", "label": None|str, "secondary": None|
     {"at_berlin", "which", "label"}} (all-None fields if nothing is scheduled at all)."""
    cands = []
    rev = usage_review()
    if rev and rev["next_run_at"] is not None:
        cands.append({"kind": "monthly", "at": rev["next_run_at"], "at_berlin": rev["next_run_berlin"], "label": None})
    for o in one_off_reviews():
        cands.append({"kind": "one-off", "at": o["at"], "at_berlin": o["at_berlin"], "label": o["label"]})
    for o in nightly_reviews():
        cands.append({"kind": "nightly", "at": o["at"], "at_berlin": o["at_berlin"], "label": o["label"]})
    if not cands:
        return {"at_berlin": None, "which": None, "label": None, "secondary": None}
    cands.sort(key=lambda c: c["at"])
    win = cands[0]
    secondary = None
    if len(cands) > 1:
        nxt = cands[1]
        secondary = {"at_berlin": nxt["at_berlin"], "which": nxt["kind"], "label": nxt["label"]}
    return {"at_berlin": win["at_berlin"], "which": win["kind"], "label": win["label"], "secondary": secondary}


RUNNER_MODES = ("on", "runner")     # AFCLAUDE_SCHEDULER values with the resident runner (phase 3a §4)


def cron_entries():
    """What is scheduled: the crontab lines tagged AFClaude (owner "cron") and, while the resident
    runner runs (AFCLAUDE_SCHEDULER on/runner; not in legacy, where cron runs everything), its own
    jobs from runner_jobs (owner "runner", phase 3a S3). A display path: a runner read that fails
    is shown as an entry with its error, never raised (D-239.3)."""
    out = crontab_entries()
    if os.environ.get("AFCLAUDE_SCHEDULER", "off") in RUNNER_MODES:
        out += runner_entries()
    return out


def runner_schedule(job):
    """A runner job's cadence as (UTC, Berlin) text."""
    if job.every is not None:
        mins = job.every.total_seconds() / 60
        txt = f"every {mins:g} min" if mins < 60 else f"every {mins / 60:g} h"
        if job.offset:
            txt += f" (+{job.offset.total_seconds():g} s)"
        return txt, txt
    if job.daily is not None:
        t = datetime.now(UTC).replace(hour=job.daily[0], minute=job.daily[1], second=0, microsecond=0)
        return f"daily {t:%H:%M} UTC", f"daily {t.astimezone(ka.BERLIN):%H:%M %Z}"
    if job.at_jobs:
        return "each at-job's minute", "each at-job's minute"
    if job.planned:
        return "when its pass plans it", "when its pass plans it"
    return "each session-window start", "each session-window start"


def runner_entries(conn=None, now=None):
    """The runner's jobs (runner.RUNNER_OWNS) with their next due time and last run, from
    runner_jobs; the first entry is the runner itself (its loop heartbeat)."""
    now = now or datetime.now(UTC)
    try:
        import actions
        import runner
        own = conn is None
        conn = conn or store.connect()
        try:
            rows = store.runner_job_rows(conn)
        finally:
            if own:
                conn.close()
    except Exception as e:  # noqa: BLE001 - shown on the page, never breaks the export
        return [{"owner": "runner", "schedule_utc": "", "schedule_berlin": "", "what": "resident runner",
                 "error": f"{type(e).__name__}: {str(e)[:300]}"}]

    def when(v):
        return bstr(store.parse_iso(v)) if v else None
    beat = store.parse_iso((rows.get(actions.RUNNER_ROW) or {}).get("heartbeat_at"))
    out = [{"owner": "runner", "schedule_utc": "", "schedule_berlin": "", "what": "resident runner (runner.py serve)",
            "heartbeat_berlin": bstr(beat), "heartbeat_age_s": round((now - beat).total_seconds()) if beat else None}]
    for name in sorted(runner.RUNNER_OWNS):
        job, r = runner.JOBS[name], rows.get(name) or {}
        utc, berlin = runner_schedule(job)
        out.append({"owner": "runner", "job": name, "schedule_utc": utc, "schedule_berlin": berlin,
                    "what": job.what or job.cron_tag or name, "next_due_berlin": when(r.get("next_due")),
                    "last_status": r.get("last_status"), "last_ok_berlin": when(r.get("last_ok_at")),
                    "consecutive_failures": r.get("consecutive_failures") or 0})
    return out


def crontab_entries():
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
        out.append({"owner": "cron", "schedule_utc": sched, "schedule_berlin": cron_berlin(sched),
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
        text, modified = doc_source(name, src)
        text = strip_frontmatter(text)
        if text is None:
            continue
        write_atomic(f"docs/{name}", text)
        docs.append({"name": name, "title": title, "path": f"docs/{name}", "modified_berlin": bstr(modified)})
    prompts = []
    for src in sorted(glob.glob(os.path.join(HERE, "prompts", "*.md"))):
        name = os.path.basename(src)
        write_atomic(f"docs/prompts/{name}", read(src) or "")
        prompts.append({"name": name, "path": f"docs/prompts/{name}"})

    status = {
        "generated_at": now.isoformat(),
        "generated_berlin": bstr(now),
        "self_session_error": SELF_SESSION_ERROR,  # D-239: shown, never crashes the export
        "automation": automation_block(),
        "manager_launch": manager_launch_block(),
        "remote_control": remote_control_block(),
        "keepalive": keepalive_and_usage(now),
        "context": context_block(now),
        "progress_log": progress_log(),
        "goals": parse_goals(doc_source("GOALS.md", os.path.join(HERE, "GOALS.md"))[0]),
        "stages": stages(ka.read_usage_cache(), now),
        "preview_from": PREVIEW_FROM,  # D-211: owner-preview marker for the phase strip (§10.9)
        "latest_decision": latest_decision(),
        "needs_input": needs_input(),
        "inbox": inbox_block(),
        "cron": cron_entries(),
        "docs": docs,
        "prompts": prompts,
    }
    page, status["page_version"] = read_page()
    write_atomic("status.json", json.dumps(status, indent=1, ensure_ascii=False, default=str))
    publish_page(page)   # after the data, so a page that reads a new field finds it


if __name__ == "__main__":
    main()
