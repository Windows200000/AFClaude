#!/usr/bin/env python3
"""run_now.py: the run-now requests (phase 3b S5, docs/phase3b_plan.md §2 "Run-now", §5 S5).

A request is an action_requests row (request.add: `tasks.py run-now work|continue|review`, MCP
afclaude_request; owner-only until phase 5a's roles, D-184/D-190), which then wakes the runner.
The runner runs each open one as the keyed run `request <id>` (runner.JOBS "request": wake-driven,
its 60-s sleep as the backstop; on_interrupt "fail": a restart during a request fails it, D-257)
in its child runner_jobs.py, which calls run(key) here. The handler decides it and records the
outcome through request.start / request.finish (actor runner:request, audited: what the
requester sees, D-154/D-256):
  work_on_now   the project's task-manager continued now with the continue prompt (+ the held inbox
                messages and notes, like a start): no window, budget, session stop or D-274 timing
                (D-153); the preflight, the session lock and the owner check still apply
                (keepalive._start_session), and its limit (D-204: refused "at its limit until ...").
                A run already going: refused "running". A project without a task-manager yet: its
                first start (keepalive._first_start -> create_task_manager, D-281), as at a start.
  continue_now  one stalled session through the dispatcher's continue path (dispatcher.continue_now:
                the D-206 preflight, its reset), no decision row needed.
  review_now    a one-off full usage review without the quota gate (usage_review.review_now).
Refused (it can't run now) is not a failure. A failure alerts AND pauses automation (D-282, for now:
a pre-release step makes it alert-only, design §13): the handler's own paths report it (fail_stop),
and one that did not is reported here. A crash records the request failed, then raises (the runner
reports it). Run-now runs while automation is paused (D-282), install-wide or the project's.
A started run-now is an ordinary run: its fire record in the keepalive state (`handled`, key
`request:<id>`) is what run end (D-224), fill-up (D-212) and compaction (D-238) read; it is not
counted against the night's start cap (MAX_FIRES_PER_WINDOW) and touches no start's dedup key.
"""
import contextlib
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import failstop  # noqa: E402
import keepalive as ka  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
JOB = "request"                 # runner.JOBS / runner_jobs: the job's name; its run key is the request id
ACTOR = "runner:request"
VIA = "runner"


class _Args:
    """keepalive's start arguments for a run-now: armed (the runner's starts are)."""
    arm = True
    now = False


def state_key(request_id):
    """The request's record key in the keepalive state's `handled` (a run's fire record)."""
    return f"request:{request_id}"


def work_on_now(conn, req, now):
    """-> (outcome, detail, session id). See the module doc."""
    pid = int(req["target"])
    tm = next((t for t in store.task_managers(conn) if int(t["project_id"]) == pid), None)
    if tm is None:
        return "refused", f"unknown target: project #{pid} does not exist", None
    key = state_key(req["id"])
    why = f"run-now request #{req['id']}"
    reason = f"{why} by {req['actor']} (D-153: no window, budget or timing gate)"
    stall = {"uuid": key, "timestamp": now, "run_now": True, "prompt": "continue",
             "budget": "run-now: no budget gate (D-153)",
             "project": {"project_id": pid, "name": tm["name"], "why": why}}
    st = ka.load_state()
    if getattr(st, "unavailable", False):
        raise RuntimeError("the keepalive state could not be read from the database")
    if key in st["handled"]:
        raise RuntimeError(f"{key} is in the keepalive state already: decided before, not started twice")
    night = schedule.night(now)
    sid = tm["session"]
    if sid is None:          # D-281: the project's first start creates its task-manager
        ka._first_start(dict(tm, why=why), stall, reason, st, _Args(), key, night)
    else:
        since = ka.run_active(sid, now, st)
        if since:
            return "refused", f"running: a run of {tm['name']} is going (since {ka.berlin(since)})", sid
        action, detail, s = ka.stall_status(sid, now)
        if action == "WAIT_RESET":
            return "refused", f"at its limit until {ka.berlin(s['reset'])} (D-204): {detail}", sid
        if action == "NO_TRANSCRIPT":
            raise RuntimeError(f"the task-manager {sid[:8]} of {tm['name']} has no transcript")
        ka._start_session(sid, None, stall, reason, st, _Args(), key, night)
    h = st["handled"].get(key) or {}
    res = h.get("result")
    sid = h.get("session") if h.get("session") not in (None, "-") else sid
    if res == "continued-in-place":
        return "started", ka.tmux_name(sid), sid
    if res == "preflight-failed" and h.get("held"):
        return "refused", f"the owner holds the session ({(h.get('problems') or ['?'])[0]})", sid
    detail = h.get("error") or "; ".join(h.get("problems") or []) or {
        "no-reply-within-timeout": f"no reply within {ka.VERIFY_TIMEOUT}"}.get(res, "")
    return "failed", f"{res or 'not decided'}: {detail}".rstrip(": "), sid


def continue_now(conn, req, now):
    import dispatcher
    sid = req["target"]
    outcome, detail = dispatcher.continue_now(conn, sid, now, req["id"])
    return outcome, detail, sid


def review_now(conn, req, now):
    import usage_review
    ok, sid, tmux = usage_review.review_now(now, req["id"])
    return ("started", tmux, sid) if ok else ("failed", "the review's launch failed", sid)


HANDLERS = {"work_on_now": work_on_now, "continue_now": continue_now, "review_now": review_now}


@contextlib.contextmanager
def _counting_reports():
    """Counts failstop.fail calls made inside (a handler path that reported its failure itself)."""
    seen = []
    orig = failstop.fail

    def spy(*a, **kw):
        seen.append(a[1] if len(a) > 1 else kw.get("subject"))
        return orig(*a, **kw)
    failstop.fail = spy
    try:
        yield seen
    finally:
        failstop.fail = orig


def _run_id(conn, key):
    """The runner's run of this request (the running `request <key>` row), or None."""
    rows = [r for r in store.key_runs(conn, JOB, key) if r["status"] == "running"]
    return max(int(r["id"]) for r in rows) if rows else None


def describe(req):
    return f"run-now request #{req['id']} ({req['kind']}{' ' + str(req['target']) if req['target'] else ''})"


def run(key, now=None):
    """Decide the open request `key` (its id) and record the outcome. -> the result note. A crash
    is recorded as failed and raised (the runner reports it: alert + pause, D-282)."""
    conn = store.connect()
    try:
        req = store.retrying(lambda: store.get_request(conn, int(key)), conn)
        if req is None:
            raise RuntimeError(f"request #{key} does not exist")
        if req["status"] != "open":
            raise RuntimeError(f"request #{key} is {req['status']}, not open: not decided twice")
        run_id = _run_id(conn, str(key))
        what = describe(req)
        ka.log(f"{what} by {req['actor']}: deciding it (run {run_id})")
        with _counting_reports() as reported:
            try:
                outcome, detail, sid = HANDLERS[req["kind"]](conn, req, now or datetime.now(UTC))
            except Exception as e:
                actions.perform(conn, "request.finish", {"request_id": req["id"], "status": "failed",
                                                         "reason": f"{type(e).__name__}: {e}", "run_id": run_id},
                                actor=ACTOR, via=VIA)
                raise
        if outcome == "started":
            actions.perform(conn, "request.start", {"request_id": req["id"], "tmux": detail, "run_id": run_id,
                                                    "session_id": sid}, actor=ACTOR, via=VIA)
        else:
            actions.perform(conn, "request.finish", {"request_id": req["id"], "status": outcome,
                                                     "reason": detail or outcome, "run_id": run_id},
                            actor=ACTOR, via=VIA)
        note = f"{what}: {outcome} ({detail})"
        ka.log(note)
        if outcome == "failed" and not reported:      # D-282: every failed run-now alerts and pauses
            failstop.fail("run_now", f"{what} failed", detail or "", log=ka.log, alert=lambda s, b="": ka.alert(s, b))
        return note
    finally:
        conn.close()
