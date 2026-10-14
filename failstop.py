#!/usr/bin/env python3
"""Fail-stop (D-234, D-235): any failure or timeout of an AFClaude runner raises an error the
owner sees and stops automation until the owner has looked at it.

    "Just abort if something fails, so we catch all the possible ways to fail and fix them
     before going public" (D-235)

fail(component, subject, detail) is the one door for that, used by the runners (keepalive.py,
dispatcher.py, usage_sampler.py, usage_review.py, docker/at_shim.py). It never raises (it IS the
error path) and does, in this order:
  1. an ERROR line in the component's log (= app_log level "error": runner_state.line_level),
  2. sets automation_paused with the reason (actions.automation.set, audited, actor
     runner:<component>): no runner starts or continues anything until the owner clears it with
     `python3 tasks.py automation resume` (the quickview shows the pause and its reason),
  3. an alert (notify.py: the alerts doc + a progress line + a best-effort push).
The caller aborts the operation itself (no fallback, no silent retry).

While automation is already paused, a failure whose subject is already in the pause reason is
only logged (a cron job failing every 15 min alerts once per pause); a new subject is appended
to the reason and alerted. Keep `subject` stable (no times or numbers; those go in `detail`).

A DB that can't be written can't record the pause: the DB-error path (D-171) then holds every
start anyway (store.db_gate) and the alert goes to data/ALERTS.fallback.md; fail() logs that the
pause was not stored.

paused(): the strict read of the switch (afclaude_config.automation_pause: unreadable = paused).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BERLIN = ZoneInfo("Europe/Berlin")
RESUME_CMD = "python3 tasks.py automation resume"
REASON_MAX = 1500        # the stored reason (actions.REASON_MAX is the hard limit)
LogFn = Callable[[str], Any]
AlertFn = Callable[[str, str], Any]


def _stderr_log(line: str) -> None:
    sys.stderr.write(f"[{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z}] {line}\n")


def _notify(subject: str, body: str) -> Any:
    from notify import notify
    return notify(subject, body)


def paused() -> dict[str, Any]:
    """{paused, reason, since, by}; a DB that can't be read counts as paused."""
    import afclaude_config
    return afclaude_config.automation_pause()


def _set_pause(component: str, reason: str) -> Optional[str]:
    """automation.set paused=True with `reason` (actor runner:<component>). -> None, or why not."""
    import actions
    import store
    try:
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": reason[:REASON_MAX]},
                            actor=f"runner:{component}", via="runner")
        finally:
            conn.close()
        return None
    except Exception as e:   # noqa: BLE001 - the error path must not fail
        return f"{type(e).__name__}: {e}"[:300]


def fail(component: str, subject: str, detail: str = "", *, log: Optional[LogFn] = None,
         alert: Optional[AlertFn] = None, pause: bool = True) -> dict[str, Any]:
    """Report a failure and stop automation (see the module doc). component: the runner (its
    actor runner:<component>); subject: what failed, one stable line; detail: the specifics.
    log / alert: the component's own (default: stderr / notify.notify). pause=False only for
    what is no failure of AFClaude itself (the caller says why). Never raises.
    -> {"paused": the pause is set, "alerted": an alert went out, "already": it was already
    paused for this subject}."""
    log = log or _stderr_log
    alert = alert or _notify
    detail = (detail or "").strip()
    out: dict[str, Any] = {"paused": False, "alerted": False, "already": False}
    try:
        log(f"ERROR: {component}: {subject}" + (f": {detail[:500]}" if detail else ""))
    except Exception:   # noqa: BLE001
        pass
    cur: dict[str, Any] = {"paused": False, "reason": None}
    if pause:
        try:
            cur = paused()
        except Exception as e:   # noqa: BLE001
            cur = {"paused": True, "reason": f"unreadable ({type(e).__name__})"}
        reason_now = cur.get("reason") or ""
        if cur.get("paused") and subject in reason_now:
            out.update(paused=True, already=True)
            try:
                log(f"automation already paused for this ({component}: {subject}); not alerted again")
            except Exception:   # noqa: BLE001
                pass
            return out
        stamp = datetime.now(BERLIN).strftime("%d.%m. %H:%M")
        entry = f"{stamp} {component}: {subject}"
        reason = f"{reason_now} | also: {entry}" if cur.get("paused") and reason_now else entry
        if len(reason) > REASON_MAX:
            reason = reason[:REASON_MAX - 3] + "..."
        why_not = _set_pause(component, reason)
        out["paused"] = why_not is None
        if why_not is not None:
            try:
                log(f"ERROR: {component}: automation_paused could not be set ({why_not}); the database "
                    "error path holds the starts (D-171)")
            except Exception:   # noqa: BLE001
                pass
    body = detail[:1200]
    if pause:
        body += ("\n" if body else "") + (
            f"AFClaude automation is PAUSED until you clear it (`{RESUME_CMD}`): no runner starts or "
            "continues anything." if out["paused"] else
            "AFClaude could NOT record the pause (database problem); the database error path holds "
            "the starts until it is healthy.")
    try:
        alert(("AFClaude STOPPED: " if pause else "") + subject, body)
        out["alerted"] = True
    except Exception as e:   # noqa: BLE001
        try:
            log(f"ERROR: {component}: the alert itself failed ({type(e).__name__}: {e})")
        except Exception:   # noqa: BLE001
            pass
    return out


class Failed(Exception):
    """An operation that must not go on (D-234/D-235). why: one stable line (fail()'s subject),
    detail: the specifics, record: what the caller keeps about it (e.g. a start's record)."""
    def __init__(self, why: str, detail: str = "", record: Optional[dict[str, Any]] = None) -> None:
        super().__init__(why + (f": {detail}" if detail else ""))
        self.why, self.detail, self.record = why, detail, dict(record or {})
