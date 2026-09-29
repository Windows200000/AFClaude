#!/usr/bin/env python3
"""
AFClaude dispatcher (goal 5): keepalive.py keeps ONE session alive; the
dispatcher runs everything else AFClaude should run, in the same nightly
window (00:00-08:00 Europe/Berlin) and under the same budget rule
(keepalive.budget_decision). One pass per invocation (cron-friendly):

  1. Approved stalled sessions: store.stalled_decisions() rows whose effective
     decision is 'continue', plus the manager sessions of managed projects
     (unless explicitly ignored). Each is continued after its reset via
     ka_resume.sh, with keepalive's evaluate() (reset + grace, window, budget)
     and preflight() (send-keys / resume / take-over of archived or idle
     holders / refuse on busy). Sessions keepalive.py's own watcher targets are
     skipped (it continues them itself). Forks (sessions sharing entry uuids,
     detected by the uuid of their first message) are grouped into families,
     and at most one per family is continued: the explicitly decided one
     (one-off decision or session rule), else the one with the most recent
     activity of its own (entries the other copies don't have).
  2. Task queue: pending tasks (kind 'task') in store.execution_order (high ->
     medium -> low, by project rank, then stage), skipping the --skip-task list
     and the stages of managed projects (projects.manager_session: that session
     works through them itself). Each task starts in a NEW session (ka_resume.sh
     --new; cwd = the project's path, else the creating session's cwd; Opus 5.5,
     effort high) with prompts/task_start.md, and is marked in_progress for it
     (store.start_task). A task that was blocked and answered resumes its old
     session instead. Every task session gets a session-scoped standing rule
     'continue' and goes into data/own_sessions.txt, so when it stalls later it
     is an approved stalled session by itself.
  3. Concurrency + budget: at most max_concurrent (default 2) dispatcher
     sessions alive at once; before EACH start: window, budget rule, and session
     usage < session_usage_stop (default 85%). Also a per-night start cap.
  4. Cleanup (any time of day): tmux sessions the dispatcher started (tracked in
     its state) that finished (their task is done/blocked/cancelled and the
     session is idle, or idle for > 2 h after an end_turn), and are not stalled,
     get their tmux session killed (logged). Nothing else is ever killed, and
     never the manager's tmux session.

Default is DRY-RUN: it decides and logs what it WOULD do and changes nothing
(no stalled.py scan, no DB writes besides store.connect()'s schema check, no
state, no own_sessions.txt, no ka_resume, no tmux kill). It reads the DB as the
last scan left it (export_quickview.py scans every 3 min).
--arm acts. State: data/dispatcher_state.json, log: data/dispatcher.log,
config (optional JSON, keys as in DEFAULTS): data/dispatcher.json. Failures go
through keepalive.alert().

    dispatcher.py [--arm] [--once] [--now] [--skip-task ID|TITLE ...] [--max-concurrent N]
"""
import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (detection, window, budget, preflight, fire, alert)
import stalled  # noqa: E402  (scan, own markers, own list)
import store  # noqa: E402

UTC = timezone.utc
DATA_DIR = os.environ.get("DISPATCHER_DATA_DIR", os.path.join(HERE, "data"))
STATE_FILE = os.path.join(DATA_DIR, "dispatcher_state.json")
LOG_FILE = os.path.join(DATA_DIR, "dispatcher.log")
LOCK_FILE = os.path.join(DATA_DIR, ".dispatcher.lock")
CONFIG_FILE = os.path.join(DATA_DIR, "dispatcher.json")
# The AFClaude manager session (kept alive by keepalive.py's watcher + window-start cron);
# same default as export_quickview.py's SELF_SESSION.
MANAGER_SESSION = os.environ.get("DISPATCHER_MANAGER_SESSION", "f2897285-dd97-49d9-b29a-2334b4753dee")
TRUST_ROOT = os.environ.get("KA_TRUST_ROOT", "/mnt/BlockVolume/Claude")   # as in ka_resume.sh
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TMUX_RE = re.compile(r"ka-[0-9a-f]{8}")

DEFAULTS = {
    "max_concurrent": 2,            # dispatcher sessions alive at once
    "session_usage_stop": 85.0,     # no new starts at/above this session %
    "max_starts_per_window": 6,     # safety cap per night
    "idle_cleanup_hours": 2.0,      # idle after an end_turn this long -> kill its tmux
    "finished_grace_minutes": 10,   # task done/blocked: idle this long -> kill its tmux
    "verify_minutes": 15,           # no real reply this long after a start -> alert
    "launch_failure_limit": 2,      # a task whose launch failed this often is skipped
    "take_over_idle": True,         # preflight: SIGTERM an idle interactive holder of an approved session
    "skip_tasks": [],               # task ids or exact titles (case-insensitive) never started
    "keepalive_sessions": [MANAGER_SESSION],   # continued by keepalive.py, never by the dispatcher
    "exclude_sessions": [],         # never continued, never cleaned up
}
FINISHED_TASK = ("done", "blocked", "cancelled")


# ---------------------------------------------------------------- log, state, config

def log(msg):
    line = f"[{datetime.now(ka.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config(path=None, overrides=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path or CONFIG_FILE) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        log(f"config {path or CONFIG_FILE} unreadable ({e}); using defaults")
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    return cfg


def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    except (OSError, json.JSONDecodeError):
        st = {}
    for k in ("sessions", "handled", "starts", "launch_failures", "alerted"):
        st.setdefault(k, {})
    return st


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1, default=str)
    os.replace(tmp, STATE_FILE)


def alert_once(st, key, subject, body=""):
    if key in st["alerted"]:
        return
    st["alerted"][key] = store.iso(datetime.now(UTC))
    ka.alert(subject, body)


# ---------------------------------------------------------------- who not to touch

def keepalive_targets():
    """Session ids that a running keepalive.py watcher targets (its --session)."""
    out = set()
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if not any(os.path.basename(a) == "keepalive.py" for a in argv):
            continue
        for i, a in enumerate(argv):
            if a == "--session" and i + 1 < len(argv):
                out.add(argv[i + 1])
            elif a.startswith("--session="):
                out.add(a.split("=", 1)[1])
    return out


def excluded_sessions(cfg):
    """{session id: why} for sessions the dispatcher never continues or cleans up."""
    ex = {s: "continued by keepalive.py (config keepalive_sessions)" for s in cfg["keepalive_sessions"]}
    for s in keepalive_targets():
        ex.setdefault(s, "target of a running keepalive.py watcher")
    for s in cfg["exclude_sessions"]:
        ex.setdefault(s, "config exclude_sessions")
    return ex


def managed_projects(conn):
    """{manager session id: project} for projects with a manager_session."""
    return {p["manager_session"]: p for p in store.list_projects(conn) if p.get("manager_session")}


# ---------------------------------------------------------------- fork families

def first_message_uuid(path, max_lines=2000):
    """uuid of the first user/assistant entry. A fork (a resume while another
    process held the session) copies the whole history, so copies share it."""
    try:
        with open(path, errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and stalled.is_message_entry(e) and e.get("uuid"):
                    return e["uuid"]
    except OSError:
        pass
    return None


def fork_families(conn):
    """{session id: [every session id of its family]} for families of 2 or more
    (top-level transcripts known to the store that share their first message uuid)."""
    groups = {}
    for r in conn.execute("SELECT session_id, path FROM sessions WHERE path IS NOT NULL"):
        key = first_message_uuid(r["path"])
        if key:
            groups.setdefault(key, []).append(r["session_id"])
    return {sid: sorted(g) for g in groups.values() if len(g) > 1 for sid in g}


def own_activity(members):
    """{sid: latest timestamp of a message entry only this copy has (None if none)}."""
    seen = {}
    for sid in members:
        path = ka.transcript_path(sid)
        seen[sid] = {}
        if not path:
            continue
        for e in ka.iter_entries(path):
            if stalled.is_message_entry(e) and e.get("uuid"):
                seen[sid][e["uuid"]] = e.get("timestamp") or ""
    out = {}
    for sid, entries in seen.items():
        others = set().union(*(set(v) for k, v in seen.items() if k != sid))
        own = [ts for u, ts in entries.items() if u not in others]
        out[sid] = max(own) if own else None
    return out


def family_winner(conn, members):
    """(winner, why): the explicitly decided member (one-off decision or session
    rule for 'continue'), else the one with the most recent own activity."""
    act = own_activity(members)
    explicit = [m for m in members
                if (lambda d: d[0] == "continue" and d[1] and not d[1].startswith("project_rule"))(
                    store.effective_decision(conn, m))]
    pool, why = (explicit, "explicitly decided") if explicit else (members, "most recent own activity")
    return max(pool, key=lambda m: (act.get(m) or "", m)), why


# ---------------------------------------------------------------- helpers

def tmux_alive(name):
    return subprocess.run(["tmux", "has-session", "-t", "=" + name], capture_output=True).returncode == 0


def kill_tmux(name):
    r = subprocess.run(["tmux", "kill-session", "-t", "=" + name], capture_output=True, text=True)
    return r.returncode, (r.stderr or "").strip()


def running(st):
    """Tracked dispatcher sessions whose tmux session is alive."""
    return {sid: s for sid, s in st["sessions"].items() if s.get("status") == "running" and tmux_alive(s["tmux"])}


def is_afclaude_cwd(cwd):
    return any(mk in (cwd or "") for mk in stalled.OWN_CWD_MARKERS)


def inside_trust_root(path):
    p, root = os.path.realpath(path), os.path.realpath(TRUST_ROOT)
    return p == root or p.startswith(root + os.sep)


def session_cwd(conn, sid):
    path = ka.transcript_path(sid)
    cands = [(ka.last_message(path) or {}).get("cwd") if path else None,
             (store.get_session(conn, sid) or {}).get("cwd")]
    return next((c for c in cands if c and os.path.isdir(c)), None)


def session_model(sid):
    """Model of the session's last real (non-synthetic) assistant message, or None."""
    path = ka.transcript_path(sid)
    model = None
    for e in ka.iter_entries(path) if path else ():
        m = (e.get("message") or {}).get("model")
        if e.get("type") == "assistant" and not e.get("isSidechain") and m and m != "<synthetic>":
            model = m
    return model


def rc_server_pids(pids):
    """The pids among `pids` that are a `claude rc` / remote-control SERVER (the
    parent of the per-session `--print --sdk-url` children). Taking over the idle
    child is fine; the server itself must never be signalled."""
    bad = []
    for pid in pids:
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except (OSError, ValueError):
            continue
        if is_rc_server(argv):
            bad.append(int(pid))
    return bad


def is_rc_server(argv):
    args = argv[1:]
    return bool(args) and args[0] in ("rc", "remote-control") and "--print" not in args


def reply_after(sid, since):
    """First real (non-synthetic) assistant entry newer than `since`, or None."""
    path = ka.transcript_path(sid)
    if not path:
        return None
    for e in ka.iter_entries(path):
        ts = ka.parse_ts(e.get("timestamp"))
        m = e.get("message") or {}
        if e.get("type") == "assistant" and ts and ts > since and m.get("model") not in (None, "<synthetic>"):
            return e
    return None


def append_own(sid):
    try:
        with open(stalled.OWN_LIST, "a") as fh:
            fh.write(sid + "\n")
    except OSError as e:
        log(f"could not add {sid[:8]} to {stalled.OWN_LIST}: {e}")


def skip_match(task, skips):
    s = {str(x).strip().lower() for x in skips}
    return str(task["id"]) in s or (task["title"] or "").strip().lower() in s


def budget_gate(usage, now, cfg):
    """-> (go, reason). Budget rule plus the session-usage headroom stop."""
    s = (usage or {}).get("session") or {}
    stop = float(cfg["session_usage_stop"])
    if s.get("percent") is not None and s["percent"] >= stop and (not s.get("resets_at") or s["resets_at"] > now):
        return False, f"STOP: session usage {s['percent']:.0f}% >= {stop:.0f}% (headroom for the user)"
    return ka.budget_decision(usage, now)


def task_context(conn, sid):
    for t in store.list_tasks(conn, status=["in_progress", "blocked"]):
        if t["assigned_session"] == sid:
            return (f"This session works on AFClaude task #{t['id']} ({t['title']}): when it is finished, or "
                    f"when you need the user, update it with afclaude_update_task (id {t['id']}, status done "
                    f"with a result summary, or blocked with the question, as note).")
    return ""


def task_message(t):
    qa = ""
    if t.get("blocked_question"):
        qa = f"Earlier question: {t['blocked_question']}"
        qa += f" Answer from the user: {t['answer']}" if t.get("answer") else " (not answered yet)"
    return ka.session_message(
        "task_start", afclaude=is_afclaude_cwd(t.get("_cwd")), id=t["id"], title=t["title"],
        project=t.get("project") or "(none)", description=t.get("description") or "(none)", qa=qa)


# ---------------------------------------------------------------- the pass

class Pass:
    def __init__(self, conn, cfg, st, now, arm, usage_getter=None, ignore_window=False):
        self.conn, self.cfg, self.st, self.now, self.arm = conn, cfg, st, now, arm
        self.usage_getter = usage_getter or ka.fresh_usage
        self.ignore_window = ignore_window
        self.report = {"continue": [], "start": [], "skip": [], "cleanup": [], "verify": [], "stop": None}
        self.night = ka.current_window_end(now).date().isoformat()
        self.planned = 0      # starts this pass (dry-run: would-starts), for the caps
        self.alive = None

    # -- bookkeeping
    def skip(self, what, why):
        self.report["skip"].append(f"{what}: {why}")

    def slots_free(self, sid):
        """Concurrency + nightly cap for starting `sid` (a tracked alive session costs nothing)."""
        if self.alive is None:
            self.alive = running(self.st)
        if sid in self.alive:
            return True, ""
        n = len(self.alive) + (0 if self.arm else self.planned)   # armed starts are in self.alive
        if n >= int(self.cfg["max_concurrent"]):
            return False, f"concurrency cap: {n} dispatcher session(s) running or starting, max {self.cfg['max_concurrent']}"
        started = self.st["starts"].get(self.night, 0) + (0 if self.arm else self.planned)
        if started >= int(self.cfg["max_starts_per_window"]):
            return False, f"nightly start cap {self.cfg['max_starts_per_window']} reached"
        return True, ""

    def gate(self):
        """Window + budget + session headroom, checked before EACH start."""
        if self.report["stop"]:
            return False, self.report["stop"]
        if not self.ignore_window and not ka.in_window(self.now):
            return False, f"outside the window, next {ka.berlin(ka.next_window_start(self.now))}"
        go, reason = budget_gate(self.usage_getter(datetime.now(UTC) if self.arm else self.now), self.now, self.cfg)
        if not go:
            self.report["stop"] = reason
        return go, reason

    def plan_start(self, sid):
        if sid not in (self.alive or {}):
            self.planned += 1

    def track(self, sid, **info):
        prev = self.st["sessions"].get(sid, {})
        own_tmux = info.pop("own_tmux")
        self.st["sessions"][sid] = dict(prev, **info, tmux=ka.tmux_name(sid), status="running",
                                        own_tmux=own_tmux or (prev.get("own_tmux") and prev.get("status") == "running"),
                                        sent_at=store.iso(self.now), verified=None)
        if self.alive is not None:
            self.alive[sid] = self.st["sessions"][sid]

    # -- 1. approved stalled sessions
    def stalled_candidates(self, excluded):
        managed = managed_projects(self.conn)
        fams = fork_families(self.conn)
        out = []
        for r in store.stalled_decisions(self.conn):
            sid = r["session_id"]
            label = f"stalled {sid[:8]}"
            approved = r["decision"] == "continue" or (sid in managed and r["decision"] != "ignore")
            if sid in excluded:
                if approved or r["decision"] is None:
                    self.skip(label, f"skipped: {excluded[sid]}")
                continue
            if not approved:
                self.skip(label, "ignored" if r["decision"] == "ignore" else "undecided (waiting for the user)")
                continue
            fam = fams.get(sid)
            if fam:
                if set(fam) & set(excluded):
                    self.skip(label, f"fork family {[m[:8] for m in fam]} contains an excluded session")
                    continue
                winner, why = family_winner(self.conn, fam)
                if winner != sid:
                    self.skip(label, f"fork of {winner[:8]} (family {[m[:8] for m in fam]}; {why} wins)")
                    continue
            key = r.get("stall_uuid") or r.get("stalled_since")
            h = self.st["handled"].get(key)
            if h:
                self.skip(label, f"this stall was already continued at {h.get('at')} ({h.get('result')})")
                continue
            out.append((r, key, managed.get(sid)))
        return out

    def continue_stalled(self, r, key, managed_project):
        sid = r["session_id"]
        label = f"stalled {sid[:8]}"
        # keepalive's per-session decision: reset + grace, window, budget
        usage_cache = {}

        def getter(n):
            if "u" not in usage_cache:
                usage_cache["u"] = self.usage_getter(n)
            return usage_cache["u"]
        action, detail, _ = ka.evaluate(sid, self.now, getter)
        if action != "FIRE":
            self.skip(label, f"{action}: {detail}")
            if action == "HOLD":
                self.report["stop"] = self.report["stop"] or detail
            return
        ok, why = self.slots_free(sid)
        if not ok:
            return self.skip(label, why)
        go, reason = self.gate()
        if not go:
            return self.skip(label, reason)
        cwd = session_cwd(self.conn, sid)
        if not cwd:
            return self.skip(label, "no existing working directory recorded; not resumable")
        ok, problems, plan = ka.preflight(sid)
        if not ok:
            self.skip(label, "preflight refused: " + "; ".join(problems))
            if self.arm:
                self.st["handled"][key] = {"at": store.iso(self.now), "result": "preflight-failed",
                                           "problems": problems}
                alert_once(self.st, f"preflight:{key}", f"dispatcher did NOT continue {sid[:8]}: preflight failed",
                           "; ".join(problems))
            return
        ctx = task_context(self.conn, sid)
        if not ctx and managed_project:
            ctx = (f"This session manages the AFClaude project {managed_project['name']!r}: keep working through "
                   f"its open stages (afclaude_list_tasks) and keep their status current (afclaude_update_task).")
        if plan.startswith("take-over:"):
            bad = rc_server_pids(plan.split(":", 1)[1].split(","))
            if bad:
                return self.skip(label, f"preflight wants to take over pid(s) {bad}, which are a `claude rc` "
                                        "server itself (only its per-session child may be taken over)")
        reason_txt = "dispatcher, " + detail
        af = is_afclaude_cwd(cwd)
        model = None                                  # AFClaude sessions: LAUNCH (Opus 5.5, high)
        if af and not ctx:
            prog = os.path.join(cwd, "PROGRESS.md")
            msg = ka.session_message("continue", reason=reason_txt,
                                     progress=prog if os.path.exists(prog) else ka.PROGRESS_FILE)
        elif ctx:                                     # AFClaude-run: a task session or a project manager
            msg = ka.session_message("continue_foreign", afclaude=af, reason=reason_txt, context=ctx)
        else:                                         # the user's own session: neutral, on its own model
            msg = ka.session_message("continue_foreign", manager=False, reason=reason_txt, context="")
            model = session_model(sid)
        name = r.get("title") or ka.tmux_name(sid)
        entry = {"sid": sid, "plan": plan, "cwd": cwd, "reason": detail, "model": model or ka.LAUNCH["model"]}
        self.plan_start(sid)
        if not self.arm:
            self.report["continue"].append(entry)
            log(f"WOULD CONTINUE (dry-run) {sid[:8]} plan={plan} cwd={cwd} name={name!r} model={entry['model']}: "
                f"{msg[:160]!r}...")
            return
        rc, out, err = ka.fire(sid, cwd, msg, plan, name=name, model=model)
        entry.update(rc=rc, stdout=out.strip(), stderr=err.strip()[-500:])
        self.report["continue"].append(entry)
        log(f"CONTINUED {sid[:8]} plan={plan} rc={rc} stdout={out.strip()!r} stderr={err.strip()[-300:]!r}")
        self.st["handled"][key] = {"at": store.iso(self.now), "plan": plan, "rc": rc,
                                   "result": "launched" if rc == 0 else "launcher-failed"}
        self.st["starts"][self.night] = self.st["starts"].get(self.night, 0) + 1
        if rc != 0:
            alert_once(self.st, f"launch:{key}", f"dispatcher continue of {sid[:8]} failed (rc={rc})",
                       f"plan={plan} stdout={out.strip()[:300]} stderr={err.strip()[:500]}")
            return
        tracked = self.st["sessions"].get(sid, {})
        self.track(sid, kind=tracked.get("kind", "continue"), task_id=tracked.get("task_id"), cwd=cwd,
                   own_tmux=plan != "send-keys" or tracked.get("own_tmux", False))

    # -- 2. task queue
    def task_candidates(self):
        managed = {p["id"]: sid for sid, p in managed_projects(self.conn).items()}
        out = []
        for t in store.execution_order(self.conn, kind="task"):
            label = f"task #{t['id']} {t['title']!r}"
            if skip_match(t, self.cfg["skip_tasks"]):
                self.skip(label, "on the skip list")
                continue
            if t["project_id"] in managed:
                self.skip(label, f"managed project {t['project']!r}: its manager session "
                                 f"{managed[t['project_id']][:8]} works the stages itself")
                continue
            fails = self.st["launch_failures"].get(str(t["id"]), 0)
            if fails >= int(self.cfg["launch_failure_limit"]):
                self.skip(label, f"launch failed {fails} times; reopen it by hand after fixing the cause")
                continue
            out.append(t)
        return out

    def task_cwd(self, t):
        p = store.get_project(self.conn, t["project_id"]) if t["project_id"] else None
        if p and p.get("path"):
            return p["path"], "project path"
        s = store.get_session(self.conn, t["created_by_session"]) if t.get("created_by_session") else None
        if s and s.get("cwd"):
            return s["cwd"], "cwd of the session that created it"
        return None, "no project path and no creating session cwd"

    def start_task(self, t):
        label = f"task #{t['id']} {t['title']!r}"
        cwd, src = self.task_cwd(t)
        if not cwd or not os.path.isdir(cwd):
            return self.skip(label, f"no usable working directory ({cwd or src})")
        if not inside_trust_root(cwd):
            return self.skip(label, f"cwd {cwd} is outside {TRUST_ROOT}; ka_resume.sh won't trust it")
        old = t.get("assigned_session")
        resume = bool(old and UUID_RE.fullmatch(old) and ka.transcript_path(old))
        sid = old if resume else str(uuid.uuid4())
        ok, why = self.slots_free(sid)
        if not ok:
            return self.skip(label, why)
        go, reason = self.gate()
        if not go:
            return self.skip(label, reason)
        plan = "new"
        if resume:
            ok, problems, plan = ka.preflight(sid)
            if not ok:
                return self.skip(label, f"its session {sid[:8]} can't be resumed: " + "; ".join(problems))
        t = dict(t, _cwd=cwd)
        msg = task_message(t)
        name = f"ka-task{t['id']} {t['title']}"[:60]
        entry = {"task": t["id"], "sid": sid, "new": not resume, "plan": plan, "cwd": cwd, "reason": reason}
        self.plan_start(sid)
        if not self.arm:
            self.report["start"].append(entry)
            log(f"WOULD START (dry-run) {label} in {'its session ' + sid[:8] if resume else 'a new session'} "
                f"plan={plan} cwd={cwd} ({src}): {msg[:160]!r}...")
            return
        with store.transaction(self.conn):
            store.start_task(self.conn, t["id"], sid)
            store.add_rule(self.conn, "session", sid, "continue", note=f"dispatcher task #{t['id']}")
        if not resume:
            append_own(sid)
        rc, out, err = ka.fire(sid, cwd, msg, plan, new=not resume, name=name)
        entry.update(rc=rc, stdout=out.strip(), stderr=err.strip()[-500:])
        self.report["start"].append(entry)
        log(f"STARTED {label} session={sid} plan={plan} rc={rc} stdout={out.strip()!r} stderr={err.strip()[-300:]!r}")
        self.st["starts"][self.night] = self.st["starts"].get(self.night, 0) + 1
        if rc != 0:
            store.reopen_task(self.conn, t["id"], f"dispatcher launch failed (rc={rc})")
            k = str(t["id"])
            self.st["launch_failures"][k] = self.st["launch_failures"].get(k, 0) + 1
            ka.alert(f"dispatcher could not start task #{t['id']} (rc={rc})",
                     f"stdout={out.strip()[:300]} stderr={err.strip()[:500]}")
            return
        self.track(sid, kind="task", task_id=t["id"], cwd=cwd, own_tmux=plan != "send-keys")

    # -- 3. verification of earlier starts, 4. cleanup
    def verify(self):
        for sid, s in self.st["sessions"].items():
            if s.get("status") != "running" or s.get("verified") is not None or not s.get("sent_at"):
                continue
            sent = store.parse_iso(s["sent_at"])
            if reply_after(sid, sent):
                s["verified"] = True
                self.report["verify"].append(f"{sid[:8]} verified")
                log(f"VERIFIED {sid[:8]}: real assistant reply after {s['sent_at']}")
            elif self.now - sent > timedelta(minutes=float(self.cfg["verify_minutes"])):
                s["verified"] = False
                self.report["verify"].append(f"{sid[:8]} NOT verified")
                alert_once(self.st, f"verify:{sid}:{s['sent_at']}",
                           f"dispatcher session {sid[:8]} did not reply within {self.cfg['verify_minutes']} min",
                           f"kind={s.get('kind')} task={s.get('task_id')} tmux={s['tmux']}")

    def cleanup(self, excluded):
        never = {ka.tmux_name(MANAGER_SESSION)} | {ka.tmux_name(x) for x in excluded}
        for sid, s in self.st["sessions"].items():
            if s.get("status") != "running":
                continue
            name = s.get("tmux") or ""
            if not TMUX_RE.fullmatch(name) or name != ka.tmux_name(sid):
                continue
            if not tmux_alive(name):
                s["status"] = "ended"
                s["ended_at"] = store.iso(self.now)
                t = store.get_task(self.conn, s["task_id"]) if s.get("task_id") else None
                log(f"session {sid[:8]} ({name}) is gone" + (f"; task #{t['id']} is {t['status']}" if t else ""))
                if t and t["status"] == "in_progress":
                    alert_once(self.st, f"ended:{sid}:{s.get('sent_at')}",
                               f"task #{t['id']} session {sid[:8]} ended while the task is still in_progress",
                               "Reopen or finish the task by hand (tasks.py reopen/done).")
                continue
            if name in never or sid in excluded:
                continue
            path = ka.transcript_path(sid)
            last = ka.last_message(path) if path else None
            if not last or ka.stall_info(last):
                continue                     # no transcript yet, or stalled: waits for a continue
            m = last.get("message") or {}
            if not (last.get("type") == "assistant" and m.get("stop_reason") == "end_turn"):
                continue                     # mid-turn (tool use) or waiting on a user entry
            idle = self.now - ka.parse_ts(last.get("timestamp"))
            t = store.get_task(self.conn, s["task_id"]) if s.get("task_id") else None
            if t and t["status"] in FINISHED_TASK and idle >= timedelta(minutes=float(self.cfg["finished_grace_minutes"])):
                why = f"task #{t['id']} is {t['status']}, idle {idle.total_seconds() / 60:.0f} min"
            elif idle >= timedelta(hours=float(self.cfg["idle_cleanup_hours"])):
                why = f"idle {idle.total_seconds() / 3600:.1f} h after an end_turn"
            else:
                continue
            if not s.get("own_tmux"):
                # we only typed into a tmux session someone else started: stop counting it, never kill it
                log(f"released {name} ({why}): not started by the dispatcher, left running")
                if self.arm:
                    s["status"] = "released"
                    s["released_at"] = store.iso(self.now)
                continue
            rows = [a for a in ka.agent_entries(sid) if ka.pid_alive(a.get("pid"))]
            if any(a.get("state") == "working" or a.get("status") == "busy" for a in rows):
                self.skip(f"cleanup {name}", "process reports busy")
                continue
            self.report["cleanup"].append({"sid": sid, "tmux": name, "why": why})
            if not self.arm:
                log(f"WOULD KILL (dry-run) tmux {name}: {why}")
                continue
            rc, err = kill_tmux(name)
            log(f"CLEANUP killed tmux {name} ({why}) rc={rc}{' ' + err if err else ''}")
            if rc == 0:
                s["status"] = "cleaned"
                s["cleaned_at"] = store.iso(self.now)
                s["cleaned_why"] = why

    def run(self):
        excluded = excluded_sessions(self.cfg)
        self.verify()
        self.cleanup(excluded)
        for r, key, mp in self.stalled_candidates(excluded):
            self.continue_stalled(r, key, mp)
        for t in self.task_candidates():
            self.start_task(t)
        return self.report


def prune_state(st, now, days=7):
    cut = store.iso(now - timedelta(days=days))
    st["sessions"] = {k: v for k, v in st["sessions"].items()
                      if v.get("status") == "running"
                      or (v.get("cleaned_at") or v.get("ended_at") or v.get("released_at") or "9") > cut}
    st["handled"] = {k: v for k, v in st["handled"].items() if (v.get("at") or "9") > cut}
    st["starts"] = dict(sorted(st["starts"].items())[-14:])


def run_pass(conn, cfg, st, now, arm, usage_getter=None, ignore_window=False):
    """One dispatcher pass; returns the report. Armed: the caller saves `st`."""
    ka.TAKE_OVER_IDLE = bool(cfg["take_over_idle"])
    ka.IGNORE_WINDOW = ignore_window
    p = Pass(conn, cfg, st, now, arm, usage_getter, ignore_window)
    rep = p.run()
    if arm:
        prune_state(st, now)
    return rep


def summary(rep, arm):
    verb = "" if arm else "would "
    parts = [f"{verb}continue {len(rep['continue'])}", f"{verb}start {len(rep['start'])}",
             f"{verb}clean up {len(rep['cleanup'])}", f"{len(rep['skip'])} skipped"]
    return ", ".join(parts) + (f" | {rep['stop']}" if rep["stop"] else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true", help="act (default: dry-run, changes nothing)")
    ap.add_argument("--once", action="store_true", help="one pass (always the case; for cron symmetry)")
    ap.add_argument("--now", action="store_true", help="ignore the 00:00-08:00 window (budget rule still applies)")
    ap.add_argument("--skip-task", action="append", default=[], metavar="ID|TITLE",
                    help="never start this task (id or exact title, case-insensitive); repeatable, "
                         "adds to the config's skip_tasks")
    ap.add_argument("--max-concurrent", type=int, help=f"default {DEFAULTS['max_concurrent']}")
    ap.add_argument("--config", help=f"JSON config (default {CONFIG_FILE})")
    ap.add_argument("--no-scan", action="store_true", help="armed: skip the stalled.py scan first")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another dispatcher.py pass holds the lock; exiting")
        return 0
    ka.log = log            # keepalive's own log lines (usage refresh, alerts) go to dispatcher.log too
    cfg = load_config(args.config, {"max_concurrent": args.max_concurrent})
    cfg["skip_tasks"] = list(cfg["skip_tasks"]) + args.skip_task
    now = datetime.now(UTC)
    conn = store.connect()
    try:
        if args.arm and not args.no_scan:
            stalled.scan(conn)
        st = load_state()
        rep = run_pass(conn, cfg, st, now, args.arm, ignore_window=args.now)
        seen = set(st.get("last_skips") or []) if args.arm else set()
        for line in rep["skip"]:
            if line not in seen:        # armed (cron): each skip reason is logged when it first shows up
                log("  skip " + line)
        st["last_skips"] = rep["skip"]
        log(f"pass ({'ARMED' if args.arm else 'DRY-RUN'}{', window ignored' if args.now else ''}): "
            + summary(rep, args.arm))
        if args.arm:
            save_state(st)
        if args.json:
            print(json.dumps(rep, indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        try:
            log(f"CRASH: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        finally:
            ka.alert(f"dispatcher.py crashed: {type(e).__name__}: {e}"[:200], traceback.format_exc()[-1500:])
        sys.exit(1)
