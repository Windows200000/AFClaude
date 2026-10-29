#!/usr/bin/env python3
"""status_snapshot.py: the status snapshot the runner writes (phase 3a S9(b), design
dashboard_design/04_architecture.md: the web container reads the usage numbers and the next run from
the DB, it imports no model code and never reads ~/.claude.json).

The runner's `status_snapshot` job runs this once a minute (runner.JOBS, `every=MIN`: at most once a
minute, and also while automation is paused), as a child process, so the model code (pacing,
keepalive) never runs inside the runner's loop. One run builds the sections and replaces their
`status_snapshot` rows in one transaction (actions.snapshot_put: a runner write, no audit row).

Sections (docs/phase3a_plan.md "As built (S9b, part)"; the payloads are the quickview's blocks, so
phase 6c R1 can build status.json from the same values):
  usage      the usage cache (session / weekly %, resets) and the budget decision
  next_run   when the next autonomous run takes place (D-166, D-200, D-201: "holding until")
  threshold  the reserve threshold, the session/weekly ratio and the prediction error
  runner     the automation pause, the runs going now, the loop heartbeat

A section that cannot be built is an error row (state 'error' and why), never an old value passed
off as current; the rows carry computed_at, so the readers can show their age. A DB error is the
job's failure: the write path alerts and pauses (D-234/D-235).
"""
import json
import sys
from datetime import datetime, timezone

import actions
import store

BUILDER = 1                         # the payload format's version (status_snapshot.builder)
SECTIONS = ("usage", "next_run", "threshold", "runner")
ACTOR = "runner:status_snapshot"
SELF_JOB = "status_snapshot"        # runner.JOBS name: its own running row is not "a run going"


def _usage(qv, u, now, decision):
    if not u:
        return {"error": "no usage cache in ~/.claude.json"}
    out = {"fetched_at_berlin": qv.bstr(u["fetched_at"]),
           "age_minutes": round((now - u["fetched_at"]).total_seconds() / 60, 1)}
    for k in ("session", "weekly"):
        if u.get(k):
            out[k] = {"percent": u[k]["percent"], "resets_at_berlin": qv.bstr(u[k]["resets_at"])}
    out["budget"] = {k: decision.get(k) for k in ("mode", "go", "reason", "headroom", "text", "postpone")}
    return {"payload": out}


def _wrap(block):
    """The quickview's blocks report a failure as {"error": text}: that is an error row."""
    if isinstance(block, dict) and "error" in block and len(block) == 1:
        return {"error": str(block["error"])}
    return {"payload": block}


def _runner(qv, conn, now):
    beat = (store.get_runner_job(conn, actions.RUNNER_ROW) or {}).get("heartbeat_at")
    running = [{"job": r["job"], "key": r["key"], "started_at": r["started_at"]}
               for r in store.running_runs(conn) if r["job"] != SELF_JOB]
    return {"payload": {"automation": qv.automation_block(), "running": running, "heartbeat_at": beat}}


def build(conn, now):
    """-> {section: {"payload": dict} | {"error": text}} (actions.snapshot_put's `sections`). One
    section's failure (any exception in building it) is that section's error row; the others stay."""
    import export_quickview as qv        # the blocks, the pause and the Berlin formatting (host and container)
    import keepalive as ka
    out = {}

    def section(name, fn):
        try:
            out[name] = fn()
        except Exception as e:    # noqa: BLE001 - recorded as the section's error row, shown on the dashboard
            out[name] = {"error": f"{type(e).__name__}: {e}"}
        return out[name]

    u = ka.read_usage_cache()
    decision = {}
    if u:
        decision = ka.budget_eval(u, now, display=True)       # a display path never pauses (D-234/D-235)
    section("usage", lambda: _usage(qv, u, now, decision))
    d = decision if decision else None
    section("next_run", lambda: _wrap(qv.next_run_block(u, now, d)))
    section("threshold", lambda: _wrap(qv.threshold_block(now, d)))
    section("runner", lambda: _runner(qv, conn, now))
    for name, sec in list(out.items()):      # the cap: an oversized payload is an error row too
        if "payload" in sec:
            n = len(json.dumps(sec["payload"], ensure_ascii=False, default=str).encode("utf-8"))
            if n > actions.SNAPSHOT_MAX:
                out[name] = {"error": f"payload of {n} bytes is over the {actions.SNAPSHOT_MAX} byte cap"}
    return out


def run(conn=None, now=None, builder=build):
    """Build and write the snapshot once. -> a one-line note. A failing write raises (DB error)."""
    own = conn is None
    conn = store.connect() if own else conn
    try:
        now = now or datetime.now(timezone.utc)
        sections = builder(conn, now)
        actions.snapshot_put(conn, sections, builder=BUILDER, actor=ACTOR, via="runner",
                             computed_at=store.iso_ms(now))
    finally:
        if own:
            conn.close()
    bad = sorted(k for k, v in sections.items() if "error" in v)
    return f"status snapshot: {len(sections)} section(s)" + (f", error in {', '.join(bad)}" if bad else "")


def main(argv=None):
    print(run())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
