#!/usr/bin/env python3
"""
AFClaude dispatcher: the 10-minute pass (cron). AFClaude runs begin only at STARTS (D-205):
the session-window starts of the night window and the last-stretch slot starts, which
keepalive.py handles (the gate decides; if it passes, the start continues the task-manager,
which works its own tasks). The dispatcher starts NO task sessions and no runs of its own;
one pass per invocation does:

  1. Approved stalled sessions (D-206): store.stalled_decisions() rows whose effective
     decision is 'continue' (the owner's own sessions) are continued right at their limit
     reset (+ grace), ANY time of day: no night window and no AFClaude budget/pacing gate
     (the windows are only for AFClaude). keepalive.evaluate() checks the reset (and that live
     usage no longer shows the limit at 100%); keepalive.preflight() still applies (send-keys
     / resume / take-over of archived or idle holders; never an RC-server thread, an sdk-url
     child or a busy session). Never continued (D-204: a limit hit ends an AFClaude run): the
     sessions keepalive.py targets (config keepalive_sessions = the AFClaude task-manager, any
     running watcher's --session), the task-manager sessions of managed projects
     (projects.manager_session) and AFClaude task sessions (assigned to a task or tracked as
     one). Forks (sessions sharing entry uuids, detected by the uuid of their first message)
     are grouped into families, and at most one per family is continued: the explicitly
     decided one (one-off decision or session rule), else the one with the most recent
     activity of its own (entries the other copies don't have).
  2. Verification: a continued session that shows no real reply within verify_minutes alerts.
  3. Cleanup (unchanged): tmux sessions the dispatcher started (tracked in its state) that
     finished (their task is done/blocked/cancelled and the session is idle, or idle for > 2 h
     after an end_turn), and are not stalled, get their tmux session killed (logged). Nothing
     else is ever killed, and never the task-manager's tmux session.

Phase 3b (not built): with several task-managers, the session-window start picks the project by
rank/priority and continues ITS task-manager; that selection belongs to the start
(keepalive.window_start_pass), not to this pass. See start_project_seam() below.

Default is DRY-RUN: it decides and logs what it WOULD do and changes nothing
(no stalled.py scan, no DB writes besides store.connect()'s schema check, no
state, no ka_resume, no tmux kill). It reads the DB as the last scan left it
(export_quickview.py scans every 3 min).
--arm acts. State: data/dispatcher_state.json, log: data/dispatcher.log. Tunables are DB
settings (D-146; afclaude_config.setting): stall_take_over_idle, stall_verify_minutes,
cleanup_finished_grace_minutes, cleanup_idle_hours. data/dispatcher.json (optional) keeps only
session ids (keepalive_sessions, exclude_sessions); its old tunables were imported into the DB
once. Failures go through keepalive.alert().

    dispatcher.py [--arm] [--once] [--no-scan] [--json]
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
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (detection, window, budget, preflight, fire, alert)
import host  # noqa: E402  (host calls: local on the host, the SSH bridge inside the container)
import stalled  # noqa: E402  (scan, own markers, own list)
import store  # noqa: E402
import afclaude_config  # noqa: E402  (settings from the DB; local identity: the task-manager session)

UTC = timezone.utc
DATA_DIR = os.environ.get("DISPATCHER_DATA_DIR", os.path.join(HERE, "data"))
STATE_FILE = os.path.join(DATA_DIR, "dispatcher_state.json")
LOG_FILE = os.path.join(DATA_DIR, "dispatcher.log")
LOCK_FILE = os.path.join(DATA_DIR, ".dispatcher.lock")
CONFIG_FILE = os.path.join(DATA_DIR, "dispatcher.json")
# The AFClaude task-manager session (continued by keepalive.py at each session-window start);
# same source as export_quickview.py's SELF_SESSION (data/afclaude.json manager_session).
MANAGER_SESSION = os.environ.get("DISPATCHER_MANAGER_SESSION") or afclaude_config.manager_session()
TMUX_RE = re.compile(r"ka-[0-9a-f]{8}")

# data/dispatcher.json: session ids only (machine identity, D-146)
DEFAULTS = {
    "keepalive_sessions": [MANAGER_SESSION],   # the task-manager: started by keepalive.py, never by the dispatcher
    "exclude_sessions": [],         # never continued, never cleaned up
}
# cfg key -> DB setting (actions.SETTINGS), read on every pass
SETTING_KEYS = {
    "idle_cleanup_hours": "cleanup_idle_hours",               # idle after an end_turn this long -> kill its tmux
    "finished_grace_minutes": "cleanup_finished_grace_minutes",   # task done/blocked: idle this long -> kill
    "verify_minutes": "stall_verify_minutes",                 # no real reply this long after a continue -> alert
    "take_over_idle": "stall_take_over_idle",                 # preflight: SIGTERM an idle interactive holder
}
FINISHED_TASK = ("done", "blocked", "cancelled")


# ---------------------------------------------------------------- log, state, config

def is_log_file(stream):
    """True if `stream` already writes into LOG_FILE (cron's `>> data/dispatcher.log 2>&1`)."""
    try:
        a, b = os.fstat(stream.fileno()), os.stat(LOG_FILE)
    except (AttributeError, OSError, ValueError):   # no fd (StringIO), closed, no log yet
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def log(msg):
    """Append one line to LOG_FILE and echo it to stdout, unless stdout IS the log file
    (cron redirects it there; every line used to land twice). -> True if written."""
    line = f"[{datetime.now(ka.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    written = False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
        written = True
    except OSError:
        pass
    if not (written and is_log_file(sys.stdout)):
        print(line, flush=True)
    return written


def load_config(path=None, overrides=None):
    """The pass's config: the session ids of data/dispatcher.json (or `path`), the tunables from
    the DB settings (afclaude_config.settings: DB value, else the code default), then `overrides`."""
    s = afclaude_config.settings(*SETTING_KEYS.values())   # first: it imports the file's old tunables once
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path or CONFIG_FILE) as fh:
            f = json.load(fh)
        cfg.update({k: v for k, v in f.items() if k in DEFAULTS})
        left = sorted(k for k in f if k not in DEFAULTS and not k.startswith("_"))
        if left:
            log(f"config {path or CONFIG_FILE}: {left} ignored (tunables are DB settings now, D-146)")
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        log(f"config {path or CONFIG_FILE} unreadable ({e}); using defaults")
    cfg.update({k: s[name] for k, name in SETTING_KEYS.items()})
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
    for k in ("sessions", "handled", "starts", "alerted", "rc_held"):
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
    """Session ids that a running keepalive.py watcher targets (its --session): local
    ones, plus, inside the container, the host's (a watcher left running there)."""
    out = set()
    argvs = []
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as fh:
                argvs.append([a.decode(errors="replace") for a in fh.read().split(b"\0") if a])
        except OSError:
            continue
    if host.in_container():
        try:
            argvs += [p["argv"] for p in host.proc_snapshot().values() if p.get("argv")]
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            log(f"host process list unavailable ({e}); only local keepalive watchers are known")
    for argv in argvs:
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
    return host.run_on_host(["tmux", "has-session", "-t", "=" + name], timeout=30).returncode == 0


def kill_tmux(name):
    r = host.run_on_host(["tmux", "kill-session", "-t", "=" + name], timeout=30)
    return r.returncode, (r.stderr or "").strip()


def is_afclaude_cwd(cwd):
    return any(mk in (cwd or "") for mk in stalled.OWN_CWD_MARKERS)


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


def task_of(conn, sid):
    """The open AFClaude task this session works on (in_progress / blocked), or None."""
    return next((t for t in store.list_tasks(conn, status=["in_progress", "blocked"])
                 if t["assigned_session"] == sid), None)


def start_project_seam(conn):
    """Design phase 3b seam (NOT built, D-205): with several task-managers, a session-window start
    (keepalive.window_start_pass, after its gate passed) would pick the project to run by rank /
    priority (store.execution_order over the managed projects) and continue THAT project's
    task-manager (projects.manager_session), which works its own tasks. Today only AFClaude has a
    task-manager, so the start always continues it. The 10-min pass never starts anything.
    -> the managed projects in rank order (read-only; nothing calls this for a decision yet)."""
    return sorted(managed_projects(conn).values(), key=lambda p: (p.get("rank") or 1 << 30, p.get("name") or ""))


# ---------------------------------------------------------------- the pass

class Pass:
    def __init__(self, conn, cfg, st, now, arm, usage_getter=None):
        self.conn, self.cfg, self.st, self.now, self.arm = conn, cfg, st, now, arm
        self.usage_getter = usage_getter or ka.fresh_usage
        self.report = {"continue": [], "skip": [], "cleanup": [], "verify": []}
        self.day = now.astimezone(ka.BERLIN).date().isoformat()

    # -- bookkeeping
    def skip(self, what, why):
        self.report["skip"].append(f"{what}: {why}")

    def track(self, sid, **info):
        prev = self.st["sessions"].get(sid, {})
        own_tmux = info.pop("own_tmux")
        self.st["sessions"][sid] = dict(prev, **info, tmux=ka.tmux_name(sid), status="running",
                                        own_tmux=own_tmux or (prev.get("own_tmux") and prev.get("status") == "running"),
                                        sent_at=store.iso(self.now), verified=None)

    # -- 1. approved stalled sessions (D-206)
    def afclaude_run(self, sid, managed):
        """Why `sid` is an AFClaude run that is never continued at a limit reset (D-204), or None."""
        if sid in managed:
            return (f"task-manager of the project {managed[sid]['name']!r}: no continue after a session limit "
                    f"(D-204), the next session-window start decides (several task-managers: phase 3b)")
        t = task_of(self.conn, sid)
        if t or self.st["sessions"].get(sid, {}).get("kind") == "task":
            what = f"task #{t['id']}" if t else "an AFClaude task"
            return f"AFClaude task session ({what}): no continue after a session limit (D-204)"
        return None

    def stalled_candidates(self, excluded):
        managed = managed_projects(self.conn)
        fams = fork_families(self.conn)
        out = []
        for r in store.stalled_decisions(self.conn):
            sid = r["session_id"]
            label = f"stalled {sid[:8]}"
            approved = r["decision"] == "continue"
            if sid in excluded:
                if approved or r["decision"] is None:
                    self.skip(label, f"skipped: {excluded[sid]}")
                continue
            run = self.afclaude_run(sid, managed)
            if run:
                if r["decision"] != "ignore":
                    self.skip(label, run)
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
            out.append((r, key))
        return out

    def continue_stalled(self, r, key):
        """D-206: right at the reset (+ grace), any time of day, no window / budget / pacing gate;
        the preflight still refuses RC-server threads, sdk-url children and busy sessions."""
        sid = r["session_id"]
        label = f"stalled {sid[:8]}"
        action, detail, _ = ka.evaluate(sid, self.now, self.usage_getter)
        if action != "FIRE":
            return self.skip(label, f"{action}: {detail}")
        cwd = session_cwd(self.conn, sid)
        if not cwd:
            return self.skip(label, "no existing working directory recorded; not resumable")
        ok, problems, plan = ka.preflight(sid)
        if not ok and problems and problems[0].startswith(ka.RC_HELD):
            # the user has it open in the Remote Control app: skip, retry next pass, log once per stall
            self.skip(label, "skipped, " + problems[0])
            if key not in self.st.setdefault("rc_held", {}):
                log(f"not continuing {sid[:8]}: {problems[0]}")
                if self.arm:
                    self.st["rc_held"][key] = store.iso(self.now)
            return
        if not ok:
            self.skip(label, "preflight refused: " + "; ".join(problems))
            if self.arm:
                self.st["handled"][key] = {"at": store.iso(self.now), "result": "preflight-failed",
                                           "problems": problems}
                alert_once(self.st, f"preflight:{key}", f"dispatcher did NOT continue {sid[:8]}: preflight failed",
                           "; ".join(problems))
            return
        reason_txt = "dispatcher, " + detail
        af = is_afclaude_cwd(cwd)
        model = None                                  # AFClaude-repo sessions: LAUNCH (Opus 5.5, high)
        if af:
            prog = os.path.join(cwd, "PROGRESS.md")
            msg = ka.session_message("continue", reason=reason_txt,
                                     progress=prog if os.path.exists(prog) else ka.PROGRESS_FILE)
        else:                                         # the user's own session: neutral, on its own model
            msg = ka.session_message("continue_foreign", manager=False, reason=reason_txt, context="")
            model = session_model(sid)
        name = r.get("title") or ka.tmux_name(sid)
        entry = {"sid": sid, "plan": plan, "cwd": cwd, "reason": detail, "model": model or ka.LAUNCH["model"]}
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
        self.st["starts"][self.day] = self.st["starts"].get(self.day, 0) + 1   # a count, no cap (D-206)
        if rc != 0:
            alert_once(self.st, f"launch:{key}", f"dispatcher continue of {sid[:8]} failed (rc={rc})",
                       f"plan={plan} stdout={out.strip()[:300]} stderr={err.strip()[:500]}")
            return
        tracked = self.st["sessions"].get(sid, {})
        self.track(sid, kind=tracked.get("kind", "continue"), task_id=tracked.get("task_id"), cwd=cwd,
                   own_tmux=plan != "send-keys" or tracked.get("own_tmux", False))

    # -- 2. verification of earlier continues, 3. cleanup
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
        for r, key in self.stalled_candidates(excluded):
            self.continue_stalled(r, key)
        # no task-session starts and no in-window starts of task-managers here (D-205): runs
        # begin only at the session-window / last-stretch slot starts (keepalive.py)
        return self.report


def prune_state(st, now, days=7):
    cut = store.iso(now - timedelta(days=days))
    st["sessions"] = {k: v for k, v in st["sessions"].items()
                      if v.get("status") == "running"
                      or (v.get("cleaned_at") or v.get("ended_at") or v.get("released_at") or "9") > cut}
    st["handled"] = {k: v for k, v in st["handled"].items() if (v.get("at") or "9") > cut}
    st["rc_held"] = {k: v for k, v in st["rc_held"].items() if v > cut}
    st["starts"] = dict(sorted(st["starts"].items())[-14:])
    st.pop("launch_failures", None)       # task starts are gone (D-205)


def run_pass(conn, cfg, st, now, arm, usage_getter=None):
    """One dispatcher pass; returns the report. Armed: the caller saves `st`."""
    ka.TAKE_OVER_IDLE = bool(cfg["take_over_idle"])
    p = Pass(conn, cfg, st, now, arm, usage_getter)
    rep = p.run()
    if arm:
        prune_state(st, now)
    return rep


def summary(rep, arm):
    verb = "" if arm else "would "
    return ", ".join([f"{verb}continue {len(rep['continue'])}", f"{verb}clean up {len(rep['cleanup'])}",
                      f"{len(rep['skip'])} skipped"])


def db_paused(arm):
    """A broken AFClaude database pauses the pass (no continues, no clean-ups, no scan; design
    §7.8, D-171). Armed: store.db_gate (the owner is alerted once per episode; the first pass
    that finds it healthy again says that automation resumes); a dry run only checks. Logged
    when the problem first shows up. -> True = skip this pass."""
    problem = store.db_gate("dispatcher") if arm else store.health()
    st = load_state() if arm else {}
    seen = st.get("db_problem")
    now_seen = f"{problem.kind}: {problem}" if problem is not None else None
    if problem is not None and (now_seen != seen or not arm):
        tail = f"; {problem.escalation}" if arm and problem.escalation else ""
        log(f"pass skipped (paused): database problem ({now_seen}){tail}")
    elif problem is None and seen:
        log("database healthy again: passes resume")
    if arm and now_seen != seen:
        st["db_problem"] = now_seen
        save_state(st)
    return problem is not None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true", help="act (default: dry-run, changes nothing)")
    ap.add_argument("--once", action="store_true", help="one pass (always the case; for cron symmetry)")
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
    afclaude_config.LOG = log   # settings problems (and the one-time import) too
    cfg = load_config(args.config)
    now = datetime.now(UTC)
    if db_paused(args.arm):
        return 0
    conn = store.connect()
    try:
        if args.arm and not args.no_scan:
            stalled.scan(conn)
        st = load_state()
        rep = run_pass(conn, cfg, st, now, args.arm)
        seen = set(st.get("last_skips") or []) if args.arm else set()
        for line in rep["skip"]:
            if line not in seen:        # armed (cron): each skip reason is logged when it first shows up
                log("  skip " + line)
        st["last_skips"] = rep["skip"]
        log(f"pass ({'ARMED' if args.arm else 'DRY-RUN'}): " + summary(rep, args.arm))
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
        tb = traceback.format_exc()
        written = False
        try:
            written = log(f"CRASH: {type(e).__name__}: {e}\n{tb.rstrip()}")
        finally:
            if not (written and is_log_file(sys.stderr)):   # cron: stderr is the log file too
                sys.stderr.write(tb)
            ka.alert(f"dispatcher.py crashed: {type(e).__name__}: {e}"[:200], traceback.format_exc()[-1500:])
        sys.exit(1)
