#!/usr/bin/env python3
"""Fail-stop (D-234, D-235): any failure or timeout of an AFClaude runner raises an error the
owner sees and stops automation until the owner has looked at it.

    "Just abort if something fails, so we catch all the possible ways to fail and fix them
     before going public" (D-235)

fail(component, subject, detail) is the one door for that, used by the runners (keepalive.py,
dispatcher.py, usage_sampler.py, usage_review.py, docker/at_shim.py, inbox_worker.py, inbox_api.py).
It never raises (it IS the error path) and does, in this order:
  1. an ERROR line in the component's log (= app_log level "error": runner_state.line_level),
  2. sets the pause with the reason (audited, actor runner:<component>): no runner starts or
     continues anything until the owner clears it with `python3 tasks.py automation resume` (the
     quickview shows the pause and its reason),
  3. an alert (notify.py: the alerts doc + a progress line + a best-effort push).
The caller aborts the operation itself (no fallback, no silent retry).

Two scopes (D-244). The install-wide pause (settings automation_paused, actions.automation.set)
holds every runner; it is what a failure that is not about one project sets (the sampler, the usage
reading, the DB, a runner itself). A project pause (table project_pauses, actions.automation.
set_project) holds only the checks of that project; fail(..., project= or session=) sets it: today
the inbox errors, for the project the message was for (its session's project). A session that
manages no project, or a project that can't be looked up, gets the install-wide pause instead (the
safe side; the log says so). The owner clears a project's pause with `python3 tasks.py automation
resume --project NAME` (resume_cmd(); plain `automation resume` clears only the install-wide one).
A runner check asks paused(project= or session=): the install-wide pause OR that project's.

While automation is already paused, a failure whose subject is already in the pause reason is
only logged (a cron job failing every 15 min alerts once per pause); a new subject is appended
to the reason and alerted. Keep `subject` stable (no times or numbers; those go in `detail`).

A process run for the resident runner whose failure the runner would otherwise report (an at-job's
shell command, phase 3a S4: runner_jobs.py can't see a fail() inside it) gets REPORTS_ENV, a file
path: fail() appends "<component>: <subject>" to it, and the runner records the failure as reported
by the handler instead of alerting a second time.

A DB that can't be written can't record the pause: the DB-error path (D-171) then holds every
start anyway (store.db_gate) and the alert goes to data/ALERTS.fallback.md; fail() logs that the
pause was not stored.

paused(): the strict read of the switch (afclaude_config.automation_pause: unreadable = paused);
pauses(): every pause (the status listing, the quickview).
"""
from __future__ import annotations

import os
import shlex
import sys
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BERLIN = ZoneInfo("Europe/Berlin")
RESUME_CMD = "python3 tasks.py automation resume"
REASON_MAX = 1500        # the stored reason (actions.REASON_MAX is the hard limit)
REPORTS_ENV = "AFCLAUDE_FAILSTOP_REPORTS"   # a file each fail() appends its subject to (module doc)
LogFn = Callable[[str], Any]
AlertFn = Callable[[str, str], Any]


def _stderr_log(line: str) -> None:
    sys.stderr.write(f"[{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z}] {line}\n")


def _notify(subject: str, body: str) -> Any:
    from notify import notify
    return notify(subject, body)


def resume_cmd(project: Optional[str] = None) -> str:
    """The owner's command that clears the install-wide pause, or with `project` (a name) that
    project's pause."""
    return f"{RESUME_CMD} --project {shlex.quote(project)}" if project else RESUME_CMD


def paused(project: object = None, session: Optional[str] = None) -> dict[str, Any]:
    """{paused, reason, since, by, scope, project} for a runner check: the install-wide pause OR
    the pause of `project` (id or name) / of the project `session` manages; neither given: the
    install-wide one only (afclaude_config.automation_pause). A DB that can't be read counts as
    paused."""
    import afclaude_config
    return afclaude_config.automation_pause(project, session)


def pauses() -> dict[str, Any]:
    """{"install": {paused, reason, since, by}, "projects": [{project, name, reason, since, by}]}
    (afclaude_config.automation_pauses; unreadable = paused install-wide)."""
    import afclaude_config
    return afclaude_config.automation_pauses()


def _target(project: object, session: Optional[str]) -> tuple[Optional[int], Optional[str], str]:
    """The project a failure pauses: (id, name, "") or (None, None, why the pause is install-wide
    instead: "" when neither was given)."""
    if project is None and not session:
        return None, None, ""
    import store
    try:
        conn = store.connect()
        try:
            pr = store.get_project(conn, project) if project is not None else store.session_project(conn, str(session))
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the error path must not fail: the install-wide pause then
        return None, None, f"its project could not be looked up ({type(e).__name__}: {e})"[:300]
    if pr is None:
        return None, None, (f"no project {project!r}" if project is not None
                            else f"the session {str(session)[:8]} is the task-manager of no project")
    return int(pr["id"]), str(pr["name"]), ""


def _set_pause(component: str, reason: str, project: Optional[int] = None) -> Optional[str]:
    """automation.set (or with `project` automation.set_project) paused=True with `reason` (actor
    runner:<component>). -> None, or why not."""
    import actions
    import store
    params: dict[str, Any] = {"paused": True, "reason": reason[:REASON_MAX]}
    try:
        conn = store.connect()
        try:
            if project is None:
                actions.perform(conn, "automation.set", params, actor=f"runner:{component}", via="runner")
            else:
                actions.perform(conn, "automation.set_project", dict(params, project=project),
                                actor=f"runner:{component}", via="runner")
        finally:
            conn.close()
        return None
    except Exception as e:   # noqa: BLE001 - the error path must not fail
        return f"{type(e).__name__}: {e}"[:300]


def _current(project: Optional[int]) -> dict[str, Any]:
    """The pause fail() adds to: the install-wide one, or that project's own (not the
    install-wide one: a project failure is recorded on the project even while all is paused)."""
    try:
        ps = pauses()
    except Exception as e:   # noqa: BLE001
        return {"paused": True, "reason": f"unreadable ({type(e).__name__})"}
    if project is None:
        return dict(ps["install"])
    mine = next((p for p in ps["projects"] if p["project"] == project), None)
    return {"paused": mine is not None, "reason": mine["reason"] if mine else None}


def fail(component: str, subject: str, detail: str = "", *, log: Optional[LogFn] = None,
         alert: Optional[AlertFn] = None, pause: bool = True, project: object = None,
         session: Optional[str] = None) -> dict[str, Any]:
    """Report a failure and stop automation (see the module doc). component: the runner (its
    actor runner:<component>); subject: what failed, one stable line; detail: the specifics.
    log / alert: the component's own (default: stderr / notify.notify). pause=False only for
    what is no failure of AFClaude itself (the caller says why). project (id or name) / session
    (the project it manages): pause only that project (D-244); without either, or if it has no
    project, the install-wide pause. Never raises.
    -> {"paused": the pause is set, "alerted": an alert went out, "already": it was already
    paused for this subject, "project": the paused project's name (None = install-wide)}."""
    log = log or _stderr_log
    alert = alert or _notify
    detail = (detail or "").strip()
    out: dict[str, Any] = {"paused": False, "alerted": False, "already": False, "project": None}
    try:
        log(f"ERROR: {component}: {subject}" + (f": {detail[:500]}" if detail else ""))
    except Exception:   # noqa: BLE001
        pass
    _note_report(component, subject)
    pid: Optional[int] = None
    name: Optional[str] = None
    if pause:
        pid, name, why_wide = _target(project, session)
        out["project"] = name
        if why_wide:
            try:
                log(f"{component}: the install-wide pause instead of a project's: {why_wide}")
            except Exception:   # noqa: BLE001
                pass
        cur = _current(pid)
        reason_now = cur.get("reason") or ""
        if cur.get("paused") and subject in reason_now:
            out.update(paused=True, already=True)
            try:
                log(f"automation already paused{f' for the project {name}' if name else ''} for this "
                    f"({component}: {subject}); not alerted again")
            except Exception:   # noqa: BLE001
                pass
            return out
        stamp = datetime.now(BERLIN).strftime("%d.%m. %H:%M")
        entry = f"{stamp} {component}: {subject}"
        reason = f"{reason_now} | also: {entry}" if cur.get("paused") and reason_now else entry
        if len(reason) > REASON_MAX:
            reason = reason[:REASON_MAX - 3] + "..."
        why_not = _set_pause(component, reason, pid)
        out["paused"] = why_not is None
        if why_not is not None:
            try:
                log(f"ERROR: {component}: the pause{f' of the project {name}' if name else ''} could not be set "
                    f"({why_not}); the database error path holds the starts (D-171)")
            except Exception:   # noqa: BLE001
                pass
    body = detail[:1200]
    if pause:
        if not out["paused"]:
            stop = ("AFClaude could NOT record the pause (database problem); the database error path holds "
                    "the starts until it is healthy.")
        elif name:
            stop = (f"AFClaude automation of the project {name} is PAUSED until you clear it (`{resume_cmd(name)}`): "
                    "none of its runs starts or continues; other projects go on.")
        else:
            stop = (f"AFClaude automation is PAUSED until you clear it (`{RESUME_CMD}`): no runner starts or "
                    "continues anything.")
        body += ("\n" if body else "") + stop
    try:
        alert((f"AFClaude STOPPED ({name}): " if pause and name else "AFClaude STOPPED: " if pause else "") + subject,
              body)
        out["alerted"] = True
    except Exception as e:   # noqa: BLE001
        try:
            log(f"ERROR: {component}: the alert itself failed ({type(e).__name__}: {e})")
        except Exception:   # noqa: BLE001
            pass
    return out


def _note_report(component: str, subject: str) -> None:
    """This failure into the REPORTS_ENV file, when the runner gave one (module doc). Never raises:
    a note that can't be written is logged to stderr (the runner then reports the failure too)."""
    path = os.environ.get(REPORTS_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{component}: {' '.join(subject.split())}\n")
    except OSError as e:
        _stderr_log(f"failstop: the report note for the runner could not be written ({type(e).__name__}: {e})")


class Failed(Exception):
    """An operation that must not go on (D-234/D-235). why: one stable line (fail()'s subject),
    detail: the specifics, record: what the caller keeps about it (e.g. a start's record)."""
    def __init__(self, why: str, detail: str = "", record: Optional[dict[str, Any]] = None) -> None:
        super().__init__(why + (f": {detail}" if detail else ""))
        self.why, self.detail, self.record = why, detail, dict(record or {})
