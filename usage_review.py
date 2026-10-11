#!/usr/bin/env python3
"""Periodic usage-model review (user rules, 2026-09-29).

ONE run that tests hypotheses on data/ (samples, Haiku judgements, weekly cycles,
keep-alive decisions), then sorts every finding into universal (goes into the code,
public) or user-specific (the user model in the DB, local only). It starts a NEW
Opus 5.5 session at MEDIUM effort (tmux, Remote-Control visible, via ka_resume.sh)
with prompts/usage_review.md + stage_eta_review.md (the rare stage-ETA review, D-213:
it tunes the stages' session estimates only) + guard_respect.md + manager.md, and runs
without user input. The results are surfaced via PROGRESS.md, a push, and an "unread review"
memory entry that the user's next conversation picks up.

Schedule: at `next_run_at` in its state (the DB, runner_state "usage_review"). The first run is Thu 2026-10-01 17:00
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
"stage_eta_review.md"), with its own extra brief.

  usage_review.py --only stage-eta --now           # fire it right away
  usage_review.py --only stage-eta --at "2026-10-15 16:00"
                                                     # spool it for that Europe/Berlin time via
                                                     # the at-shim (docker/at_shim.py); precise to
                                                     # the minute (the at-shim's cron runs every
                                                     # minute, unlike this script's own hourly cron)
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402  (phase 2d: the state in the DB only, D-161)

UTC = timezone.utc
STATE = os.path.join(HERE, "data", "usage_review_state.json")
FIRST_RUN = datetime(2026, 10, 1, 17, 0, tzinfo=ka.BERLIN)
MIN_GAP_DAYS = 28                    # "once a month, or every other month"; tune here
BEFORE_RESET = timedelta(hours=2)
MODEL, EFFORT = "claude-opus-5-5", "medium"
# --only names -> their one-off prompt (prompts/<name>.md), distinct from the regular review's
# own prompt so the one-off brief (the owner's specific ask) can differ from the periodic one.
ONLY_PROMPTS = {"stage-eta": "stage_eta_review_once"}
AT_TIME_FMT = "%Y-%m-%d %H:%M"


def load():
    st = runner_state.load("usage_review", STATE, {"next_run_at": FIRST_RUN.isoformat(), "runs": []})   # the DB
    st.setdefault("next_run_at", FIRST_RUN.isoformat())
    st.setdefault("runs", [])
    return st


def stored():
    """The schedule has been saved before (the DB holds the component's rows)."""
    try:
        return bool(runner_state.read_json(STATE))
    except (OSError, ValueError):
        return False


def save(st):
    runner_state.save("usage_review", st, STATE)


def db_problem():
    """store.db_gate (alerts once per episode): a broken DB pauses the review's start. -> problem or None."""
    import store
    problem = store.db_gate("usage_review")
    if problem is not None:
        ka.log(f"usage review: PAUSED, database problem ({problem.kind}: {problem}); {problem.escalation}")
    return problem


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


def run_once(st, now, which):
    """A one-off review of just `which` (ONLY_PROMPTS), fired right away: its own prompt (not
    the regular periodic one) + guard_respect + manager, no usage_review.md. Logged under
    st["one_off_runs"] -- separate from st["runs"]/next_run_at, so it never touches or delays
    the monthly schedule. -> ok."""
    local = now.astimezone(ka.BERLIN)
    d = local.date().isoformat()
    parts = [ka.load_prompt(ONLY_PROMPTS[which]).format(date=d), ka.load_prompt("guard_respect"),
             ka.load_prompt("manager")]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--only", choices=sorted(ONLY_PROMPTS),
                    help="run (or, with --at, schedule) only this review, once, outside the monthly schedule")
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
    if getattr(st, "unavailable", False) or db_problem():
        return   # a broken DB (or its state unreadable): start nothing (design §7.8); the next cron run retries
    if args.only:
        run_once(st, now, args.only)
        save(st)
        return
    if args.now or now >= datetime.fromisoformat(st["next_run_at"]):
        run(st, now)
        save(st)
    elif not stored():
        save(st)


if __name__ == "__main__":
    main()
