#!/usr/bin/env python3
"""Periodic usage-model review (user rules, 2026-09-29).

ONE run that tests hypotheses on data/ (samples, Haiku judgements, weekly cycles,
keep-alive decisions), then sorts every finding into universal (goes into the code,
public) or user-specific (data/user_model.json, local only). It starts a NEW
Opus 5.5 session at MEDIUM effort (tmux, Remote-Control visible, via ka_resume.sh)
with prompts/usage_review.md + stage_eta_review.md (the rare stage-ETA review, D-213:
it tunes the stages' session estimates only) + guard_respect.md + manager.md, and runs
without user input. The results are surfaced via PROGRESS.md, a push, and an "unread review"
memory entry that the user's next conversation picks up.

Schedule: at `next_run_at` in the state file. The first run is Thu 2026-10-01 17:00
Berlin, 2 h before that week's reset, so it spends quota that would expire anyway.
It deliberately IGNORES the weekly budget rule and the night window. Each later run
is 2 h before the first weekly reset that is at least MIN_GAP_DAYS after the
previous run. Cron checks hourly.

  usage_review.py            # cron mode: run if next_run_at has passed
  usage_review.py --status
  usage_review.py --now      # run immediately

One-off review (--only, owner request, D-220/D-221): run just ONE of the reviews below, outside
the monthly schedule, without touching next_run_at or st["runs"] (st["one_off_runs"] instead) --
e.g. a single stage-ETA review ("prompts/stage_eta_review_once.md" rather than the regular
"stage_eta_review.md"), with its own extra brief. "usage" runs the full usage review
(prompts/usage_review.md, the same brief as the monthly run) as a one-off, same rule: it never
touches next_run_at/st["runs"].

  usage_review.py --only stage-eta --now           # fire it right away
  usage_review.py --only usage --now               # a one-off full usage review, same rule
  usage_review.py --only stage-eta --at "2026-10-15 16:00"
                                                     # spool it for that Europe/Berlin time via
                                                     # the at-shim (docker/at_shim.py); precise to
                                                     # the minute (the at-shim's cron runs every
                                                     # minute, unlike this script's own hourly cron)

Chained one-off (--only usage,stage-eta, D-228): the owner asked for an extra usage review AND
the already-scheduled one-off ETA review right after it, at the same time. The ETA review only
needs the usage review's RESULT (the forecast it fits), not its working context, so this does
NOT run them in one session: it fires the one-off usage review, waits (bounded, see
USAGE_DONE_TIMEOUT below) for it to reliably finish -- its report data/reviews/<date>.md written
AND its session gone idle (session_idle(); D-224's explicit stop-marker CLI does not exist yet,
so this is the strongest signal available) -- then fires a SEPARATE fresh session for the one-off
ETA review, whose brief (prompts/stage_eta_review_once_after_usage.md) points at the finished
review's report and data/user_model.json. If the usage review never finishes within the timeout,
it alerts and never launches the ETA review. The order is fixed (usage first) regardless of how
the two names are given to --only. Each leg is logged under its own st["one_off_runs"] entry, as
usual never touching the monthly schedule.

  usage_review.py --only usage,stage-eta --now
  usage_review.py --only usage,stage-eta --at "2026-10-15 16:00"
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402  (phase 2d: the state also in the DB)

UTC = timezone.utc
STATE = os.path.join(HERE, "data", "usage_review_state.json")
FIRST_RUN = datetime(2026, 10, 1, 17, 0, tzinfo=ka.BERLIN)
MIN_GAP_DAYS = 28                    # "once a month, or every other month"; tune here
BEFORE_RESET = timedelta(hours=2)
MODEL, EFFORT = "claude-opus-5-5", "medium"
# --only names -> their one-off prompt (prompts/<name>.md), distinct from the regular review's
# own prompt so the one-off brief (the owner's specific ask) can differ from the periodic one.
# "usage" reuses the regular monthly brief (it IS the same full review, just logged as a one-off).
ONLY_PROMPTS = {"stage-eta": "stage_eta_review_once", "usage": "usage_review"}
# Chaining order (D-228): when --only names more than one of the above, they always run in this
# order, whatever order the owner typed them in -- the ETA review needs the usage review's result.
ONLY_ORDER = ("usage", "stage-eta")
AT_TIME_FMT = "%Y-%m-%d %H:%M"
USAGE_DONE_TIMEOUT = timedelta(hours=6)    # D-228 chain: how long to wait for the usage review
USAGE_DONE_POLL = timedelta(minutes=5)     # before giving up (alert) and never firing the ETA leg


def load():
    got = runner_state.db_load("usage_review", STATE)
    if got is not None:
        return got
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"next_run_at": FIRST_RUN.isoformat(), "runs": []}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=1)
    runner_state.save("usage_review", st, STATE)


def next_run_after(now):
    """2 h before the first weekly reset >= MIN_GAP_DAYS from now."""
    u = ka.read_usage_cache() or {}
    reset = (u.get("weekly") or {}).get("resets_at")
    if not reset:
        return now + timedelta(days=MIN_GAP_DAYS)
    while reset < now + timedelta(days=MIN_GAP_DAYS):
        reset += ka.WEEK
    return reset - BEFORE_RESET


def run(st, now):
    local = now.astimezone(ka.BERLIN)
    since = st["runs"][-1]["at"][:10] if st["runs"] else "2026-09-26"
    d = local.date().isoformat()
    parts = [ka.load_prompt("usage_review").format(date=d, since=since),
             ka.load_prompt("stage_eta_review").format(date=d),
             ka.load_prompt("guard_respect"), ka.load_prompt("manager")]
    msg = " ".join(" ".join(parts).split())
    sid = str(uuid.uuid4())
    ka.LAUNCH.update(model=MODEL, effort=EFFORT)
    rc, out, err = ka.fire(sid, HERE, msg, "resume", new=True, name=f"AFClaude usage review {d}")
    ok = rc == 0 and out.strip().startswith("started")
    st["runs"].append({"at": now.isoformat(), "session": sid, "rc": rc, "out": out.strip(), "err": err.strip()[-300:]})
    if ok:
        st["next_run_at"] = next_run_after(now).isoformat()
        ka.log(f"usage review started: {sid}")
        ka.progress_note(f"usage review started: session {sid} (Opus medium, tmux ka-{sid[:8]}); "
                         f"next {ka.berlin(datetime.fromisoformat(st['next_run_at']))}")
    else:
        ka.alert("usage review launch failed", f"rc={rc} out={out.strip()} err={err.strip()[-500:]}")


def run_once(st, now, which, extra=None):
    """A one-off review of just `which` (one ONLY_PROMPTS key: "stage-eta" or "usage"), fired
    right away in its OWN fresh session: its own prompt (not the regular periodic one; "usage"
    is the exception -- it reuses prompts/usage_review.md verbatim, since the one-off usage
    review IS the full monthly review, just logged as a one-off) + guard_respect + manager.
    `extra`, if given, is extra context inserted right after `which`'s own brief (D-228 chain:
    the ETA review's pointer at the usage review's result) -- never used for a solo run. Logged
    under st["one_off_runs"] -- separate from st["runs"]/next_run_at, so it never touches or
    delays the monthly schedule. -> ok."""
    local = now.astimezone(ka.BERLIN)
    d = local.date().isoformat()
    since = st["runs"][-1]["at"][:10] if st.get("runs") else "2026-09-26"
    brief = ka.load_prompt(ONLY_PROMPTS[which]).format(date=d, since=since)
    parts = [brief] + ([extra] if extra else []) + [ka.load_prompt("guard_respect"), ka.load_prompt("manager")]
    msg = " ".join(" ".join(parts).split())
    sid = str(uuid.uuid4())
    ka.LAUNCH.update(model=MODEL, effort=EFFORT)
    rc, out, err = ka.fire(sid, HERE, msg, "resume", new=True, name=f"AFClaude {which} review (one-off) {d}")
    ok = rc == 0 and out.strip().startswith("started")
    st.setdefault("one_off_runs", []).append(
        {"at": now.isoformat(), "only": which, "session": sid, "rc": rc, "out": out.strip(), "err": err.strip()[-300:]})
    if ok:
        ka.log(f"one-off {which} review started: {sid}")
        ka.progress_note(f"one-off {which} review started: session {sid} (Opus medium, tmux ka-{sid[:8]})")
    else:
        ka.alert(f"one-off {which} review launch failed", f"rc={rc} out={out.strip()} err={err.strip()[-500:]}")
    return ok


def schedule_once(which, at_str, now=None):
    """Spool an at-shim job (docker/at_shim.py, the mechanism usage_sampler.py's weekly-reset
    samples already use) that runs `usage_review.py --only <which> --now` at the given
    Europe/Berlin wall-clock time `at_str` ("YYYY-MM-DD HH:MM"). Precise to the minute (the
    at-shim's own cron line runs every minute); never touches next_run_at or st["runs"]/the
    monthly schedule -- that state is only written by run_once(), when the spooled job actually
    fires. -> dict(ok, which, at_local, at_utc, rc, out) | dict(ok=False, error=...) for a bad time."""
    now = now or datetime.now(UTC)
    try:
        local = datetime.strptime(at_str, AT_TIME_FMT).replace(tzinfo=ka.BERLIN)
    except ValueError as e:
        return {"ok": False, "error": f'bad --at time {at_str!r} (want "{AT_TIME_FMT}"): {e}'}
    when = local.astimezone(UTC)
    at_time = when.strftime("%Y%m%d%H%M")
    log_file = os.path.join(os.environ.get("KEEPALIVE_STATE_DIR", HERE), "keepalive.log")
    cmd = f"cd {HERE} && python3 usage_review.py --only {which} --now >> {log_file} 2>&1\n"
    r = subprocess.run(["at", "-t", at_time], input=cmd, text=True, capture_output=True,
                       env=dict(ka.SCRUBBED_ENV, TZ="UTC"))
    ok = r.returncode == 0
    result = {"ok": ok, "which": which, "at_local": local.isoformat(), "at_utc": when.isoformat(),
              "rc": r.returncode, "out": (r.stderr or r.stdout).strip()[-300:]}
    if ok:
        ka.log(f"one-off {which} review scheduled for {ka.berlin(when)}: {result['out']}")
    else:
        ka.alert(f"one-off {which} review scheduling failed", json.dumps(result))
    return result


def parse_only(value):
    """argparse type for --only: one ONLY_PROMPTS key, or a comma-separated combination of them
    (D-228: "usage,stage-eta"). -> the canonical, chain-order string ("usage", "stage-eta" or
    "usage,stage-eta"), regardless of the order `value` named them in -- the chain's order is
    fixed (ONLY_ORDER), never the owner's typing order."""
    parts = {p.strip() for p in value.split(",") if p.strip()}
    bad = parts - set(ONLY_PROMPTS)
    if bad or not parts:
        raise argparse.ArgumentTypeError(
            f"invalid --only value {value!r}; choices: {', '.join(ONLY_ORDER)}, or the combination "
            f"'{','.join(ONLY_ORDER)}'")
    return ",".join(k for k in ONLY_ORDER if k in parts)


def session_idle(sid, now, idle=None):
    """True once session `sid` (and any of its subagents) has written nothing for `idle` (default
    keepalive's own RUN_IDLE, 15 min) -- the same "last written" signal keepalive.run_active()
    uses for a run IT dispatched, adapted for a session usage_review.py fired directly (ka.fire()):
    such a session never gets a st["handled"] entry of its own, so run_active() itself cannot see
    it. False (never idle) if the transcript cannot be found yet (not started, or still starting
    up). The task-manager's explicit stop (keepalive.py --run-stopped, D-224) ends only keep-alive
    runs; a review session doesn't run it."""
    idle = idle if idle is not None else ka.RUN_IDLE
    path = ka.transcript_path(sid)
    if not path:
        return False
    files = [path] + glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl"))
    try:
        last = max(os.path.getmtime(f) for f in files if os.path.exists(f))
    except ValueError:
        return False
    return now - datetime.fromtimestamp(last, UTC) >= idle


def usage_review_finished(sid, report, now):
    """The one-off usage review session `sid` has reliably finished (D-228): its report
    deliverable exists AND the session itself has gone idle (session_idle()) -- both together,
    since either alone is weaker (the report could be mid-write; the session could just be slow
    to start). -> bool."""
    return os.path.exists(report) and session_idle(sid, now)


def wait_for_usage_review(sid, report, start, timeout=USAGE_DONE_TIMEOUT, poll=USAGE_DONE_POLL,
                           now_fn=None, sleep_fn=None):
    """Bounded poll for usage_review_finished(), from `start` for up to `timeout`, checking every
    `poll`. now_fn/sleep_fn are injectable (tests pass a fake clock that jumps by `poll` each call
    and a no-op sleep, so this returns in-process instantly instead of really waiting hours).
    -> bool (True = seen finished within the timeout)."""
    now_fn = now_fn or (lambda: datetime.now(UTC))
    sleep_fn = sleep_fn or time.sleep
    while True:
        now = now_fn()
        if usage_review_finished(sid, report, now):
            return True
        if now - start >= timeout:
            return False
        sleep_fn(poll.total_seconds())


def run_usage_then_eta(st, now, wait_fn=None):
    """D-228 chain (--only usage,stage-eta): the one-off usage review, then -- once it has
    reliably finished, in its OWN session -- a SEPARATE fresh session for the one-off ETA review,
    whose brief points at the usage review's result (prompts/stage_eta_review_once_after_usage.md
    inserted ahead of the regular one-off ETA brief). Never runs them in one session: the ETA
    review only needs the usage review's result, not its working context. If the usage review's
    own launch fails, or it never finishes within USAGE_DONE_TIMEOUT, this alerts and never
    launches the ETA review. Each leg is its own st["one_off_runs"] entry; the monthly schedule
    (next_run_at/st["runs"]) is never touched, exactly like a solo run_once(). wait_fn defaults to
    wait_for_usage_review, looked up at call time (not bound as a default) so tests can swap in a
    fast fake without really waiting. -> bool (both legs launched)."""
    wait_fn = wait_fn or wait_for_usage_review
    if not run_once(st, now, "usage"):
        save(st)
        return False
    sid = st["one_off_runs"][-1]["session"]
    save(st)
    d = now.astimezone(ka.BERLIN).date().isoformat()
    report = os.path.join(HERE, "data", "reviews", f"{d}.md")
    if not wait_fn(sid, report, now):
        ka.alert("one-off usage review (D-228 chain) did not finish in time for the ETA review",
                 f"session {sid}; waited {USAGE_DONE_TIMEOUT}; report "
                 f"{'found' if os.path.exists(report) else 'missing'}; the ETA review was not launched")
        return False
    extra = ka.load_prompt("stage_eta_review_once_after_usage").format(report=report)
    ok = run_once(st, datetime.now(UTC), "stage-eta", extra=extra)
    save(st)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--only", type=parse_only,
                    help="run (or, with --at, schedule) only this review, once, outside the monthly schedule: "
                         "'stage-eta', 'usage', or the chained 'usage,stage-eta' (D-228)")
    ap.add_argument("--at", metavar="\"YYYY-MM-DD HH:MM\"",
                    help="with --only: schedule it for this Europe/Berlin time via the at-shim, instead of "
                         "running it now")
    args = ap.parse_args()
    if args.at and not args.only:
        ap.error("--at requires --only")
    st = load()
    now = datetime.now(UTC)
    if args.status:
        print(json.dumps(st, indent=1), "\nnext:", ka.berlin(datetime.fromisoformat(st["next_run_at"])))
        return
    if args.at:
        print(json.dumps(schedule_once(args.only, args.at, now), indent=1))
        return
    if args.only:
        if args.only == ",".join(ONLY_ORDER):   # "usage,stage-eta": the D-228 chain
            run_usage_then_eta(st, now)
        else:
            run_once(st, now, args.only)
            save(st)
        return
    if args.now or now >= datetime.fromisoformat(st["next_run_at"]):
        run(st, now)
        save(st)
    elif not os.path.exists(STATE):
        save(st)


if __name__ == "__main__":
    main()
