#!/usr/bin/env python3
"""Periodic usage-model review (user rules, 2026-09-29).

ONE run that tests hypotheses on data/ (samples, Haiku judgements, weekly cycles,
keep-alive decisions), then sorts every finding into universal (goes into the code,
public) or user-specific (the user model in the DB, local only). It starts a NEW
Opus 5.5 session at MEDIUM effort (tmux, Remote-Control visible, via ka_resume.sh)
with prompts/usage_review.md + stage_eta_review.md (the rare stage-ETA review, D-213:
it tunes the stages' session estimates only) + guard_respect.md + manager.md, and runs
without user input. The report is the review doc review:<date> in the DB (`tasks.py doc set`,
design §7.7; the old data/reviews/<date>.md files are legacy, still imported); the results are
surfaced via the progress doc, a push, and an "unread review" memory entry that the user's next
conversation picks up.

usage_reviews (the review docs' structured part, D-161, design §7.2): every launch is a record
in the DB (telemetry kind usage.review, keyed by the session id): which reviews, one-off or
monthly, when, the report doc, the attempt, and its status (started -> finished / no_report /
limit / died, or launch_failed / timed_out); the hourly cron run (track_reviews) marks a started
review finished once its report doc exists and its session went idle, limit or died once
review_death() finds it dead, or no_report after REVIEW_REPORT_DEADLINE.

Schedule: at `next_run_at` in its state (the DB, runner_state "usage_review"). The first run is Thu 2026-10-01 17:00
Berlin, 2 h before that week's reset, so it spends quota that would expire anyway.
It deliberately IGNORES the weekly budget rule and the night window (but not the quota a
review needs, see Quota below). Each later run is 2 h before the first weekly reset that is at
least MIN_GAP_DAYS after the previous run. Cron checks hourly.

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
NOT run them in one session: it fires the one-off usage review, then -- once it has reliably
finished, its report doc review:<date> written AND its session gone idle (session_idle()) --
a SEPARATE fresh session for the one-off ETA review, whose brief
(prompts/stage_eta_review_once_after_usage.md) points at the finished review's report and the
user model (in the DB, `telemetry.py user-model get`). The order is fixed (usage first)
regardless of how the two names are given to --only. Each leg is logged under its own
st["one_off_runs"] entry, as usual never touching the monthly schedule.

  usage_review.py --only usage,stage-eta --now
  usage_review.py --only usage,stage-eta --at "2026-10-15 16:00"

The chain never waits in a long-lived process (a container restart killed that waiter): its
pending step is st["chain"] in the DB, and chain_pass() advances it, from an at-shim job
`usage_review.py --chain-step` it spools for itself every CHAIN_POLL (the at jobs are in the DB
too) and from every hourly cron run (the backstop that re-spools it), under a file lock (one
pass at a time, so the ETA leg never fires twice).

Quota (fix of 15.10.: the one-off review started at 99 % weekly and its worker died at the
limit three minutes later): no review session starts (monthly, one-off, either chain leg)
unless the fresh usage reading (keepalive.fresh_usage, the keep-alive's own) has REVIEW_NEEDS
free; otherwise it is POSTPONED to the reset that frees it (+ RESET_GRACE): a one-off gets an
at job for that time, the monthly one a later next_run_at, the chain's ETA leg a later chain
step. Postponing is logged, noted in the progress doc and kept in st["postponed"]; it is not an
error. A reading that fails, or a meter without a reset time to postpone to, is (D-235).

Dead reviews (review_death()): a review session whose transcript ends on a usage-limit notice,
on another API error, or with a failed worker agent, or that ended its turn without the report
and with no agent running, or whose tmux session is gone, is dead. The chain checks its usage
leg every CHAIN_POLL, the hourly run every other started review. A usage-limit death is a known,
recoverable cause: the review is retried once after the limit's reset (MAX_ATTEMPTS, with an
alert, no pause); a second limit death, or any other death, is an error (fail-stop).

Failures (D-234/D-235, failstop.py): a launch that fails, a dead review, a chained usage review
that does not finish within USAGE_DONE_TIMEOUT, a review without its report after
REVIEW_REPORT_DEADLINE, an `at` job that can't be scheduled, a report doc that can't be read, a
crash: an ERROR line, an alert, and automation paused until the owner clears it (`tasks.py
automation resume`). While automation is paused no review starts: the monthly one waits (the
hourly cron run starts it once the pause is cleared), a pending chain step waits too (the hourly
run picks it up), a one-off one (its `at` job is used up) is reported and must be scheduled
again.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402  (phase 2d: the state in the DB only, D-161)
import project_docs  # noqa: E402  (the report doc review:<date>, design §7.7)
import telemetry  # noqa: E402  (the usage_reviews records, kind usage.review)
import failstop  # noqa: E402  (D-234/D-235: a failure is an error the owner sees, automation paused)

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
CHAIN = ",".join(ONLY_ORDER)
AT_TIME_FMT = "%Y-%m-%d %H:%M"
USAGE_DONE_TIMEOUT = timedelta(hours=6)    # D-228 chain: how long to wait for the usage review
CHAIN_POLL = timedelta(minutes=5)          # before giving up (error, no ETA leg); its next check this soon
REVIEW_REPORT_DEADLINE = timedelta(hours=24)   # a started review without its report doc by then: no_report (error)
REVIEWS_DIR = os.path.join(HERE, "data", "reviews")   # legacy report files (before the §7.7 switch)
ACTOR = "runner:usage_review"
# The free quota a review session needs to start, in % of each meter. Measured: the 01.10. review
# used 40 % of a session window (52 -> 92 %) and >= 6 % of the week before the weekly limit cut it
# off (94 -> 100 %; ~0.16 weekly % per session %, limit_ratio); the 15.10. one started at 99 %
# weekly and its worker died at the limit 3 min later. Both are lower bounds, hence the margin.
REVIEW_NEEDS = {"weekly": 10.0, "session": 50.0}
MAX_ATTEMPTS = 2                   # a review that hits a usage limit is retried once, after the reset
POSTPONED_KEPT = 20                # st["postponed"]: the last postponements
CHAIN_STEP_ARG = "--chain-step"
LOCK_NAME = "usagerev"             # keepalive.session_lock's file keepalive_session_usagerev.lock
LOCK_WAIT = timedelta(minutes=10)  # a pass waits this long for another one, then: error (D-234)
FAILED_AGENT_RE = re.compile(r"<status>failed</status>.*?<summary>(?P<summary>.*?)</summary>", re.S)


def fail_stop(subject, detail=""):
    """A failure (D-234/D-235): ERROR line, alert (keepalive.alert), automation paused."""
    return failstop.fail("usage_review", subject, detail, log=lambda m: ka.log(m),
                         alert=lambda subj, body="": ka.alert(subj, body))


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
    """-> stored (False: a DB failure, the save went to the fallback file, §7.8)."""
    return runner_state.save("usage_review", st, STATE)


def save_checked(st, what):
    """save() for a state a later step depends on (the chain's pending step, a retry, a
    postponement): a save that failed is an error (D-235), never a step silently lost."""
    if not save(st):
        raise failstop.Failed("the usage review state could not be saved", what)


def db_problem():
    """store.db_gate (alerts once per episode): a broken DB pauses the review's start. -> problem or None."""
    import store
    problem = store.db_gate("usage_review")
    if problem is not None:
        ka.log(f"usage review: PAUSED, database problem ({problem.kind}: {problem}); {problem.escalation}")
    return problem


def lock():
    """The review runner's lock (one pass at a time: the record tracking, a chain start or step,
    the monthly start; so a chain step never fires a leg twice), keepalive.session_lock's file
    lock. -> the open lock (release it with keepalive.release_lock). Still held by another
    process after LOCK_WAIT: an error (D-234)."""
    fh = ka.session_lock(LOCK_NAME, LOCK_WAIT)
    if fh is None:
        raise failstop.Failed("the usage review lock is held too long", f"still held after {LOCK_WAIT}")
    return fh


def report_doc(now):
    """The review doc of a review started at `now`: review:<Berlin date>."""
    return f"review:{now.astimezone(ka.BERLIN).date().isoformat()}"


def report_ready(report):
    """The report doc `report` (review:<date>) exists in the DB, or (a review that still wrote the
    legacy file) data/reviews/<date>.md does. A DB read error counts as not ready."""
    try:
        if project_docs.doc_text(report) is not None:
            return True
    except Exception as e:  # noqa: BLE001 - an error (D-234/D-235), not "not ready yet"
        raise failstop.Failed("a review's report doc could not be read",
                              f"{report}: {type(e).__name__}: {e}"[:300]) from None
    return os.path.exists(os.path.join(REVIEWS_DIR, report.split(":", 1)[-1] + ".md"))


def record_review(sid, reviews, now, ok, rc, one_off, **extra):
    """The usage_reviews record of a launch (telemetry kind usage.review, the DB; never raises).
    extra: attempt (1 = first, MAX_ATTEMPTS = the retry after a usage limit), chain ("usage" /
    "stage-eta": a D-228 chain leg), after_report (the ETA leg: the usage review's report)."""
    rec = {"review_id": sid, "session": sid, "reviews": list(reviews), "one_off": one_off,
           "date": now.astimezone(ka.BERLIN).date().isoformat(), "started_at": now.isoformat(),
           "report_doc": report_doc(now), "status": "started" if ok else "launch_failed", "rc": rc,
           "final": not ok, "attempt": 1}
    rec.update(extra)
    telemetry.write_records("usage.review", [rec], actor=ACTOR)
    return rec


def review_record(sid):
    return next((r for r in telemetry.read_records("usage.review") if r.get("review_id") == sid), None)


def finish_review(sid, status, now, **extra):
    """Mark the record of review session `sid` final with `status` (finished / no_report / limit /
    died / timed_out; extra: why). -> the record, None if there is none."""
    rec = review_record(sid)
    if rec is None:
        return None
    rec.update(status=status, final=True, finished_at=now.isoformat(), **extra)
    telemetry.write_records("usage.review", [rec], actor=ACTOR)
    return rec


def review_label(rec):
    """'monthly usage+stage-eta', 'one-off usage', 'one-off usage (D-228 chain)', ..."""
    label = f"{'one-off' if rec.get('one_off') else 'monthly'} {'+'.join(rec.get('reviews') or [])}"
    return label + (" (D-228 chain)" if rec.get("chain") else "")


# ---------------------------------------------------------------- quota before a start

def ceil_minute(t):
    """`t` on the minute, rounded up (the at-shim runs jobs on the minute)."""
    return t if not (t.second or t.microsecond) else t.replace(second=0, microsecond=0) + timedelta(minutes=1)


def quota_check(now, what):
    """Before a review session `what` starts (monthly, one-off, either chain leg): the fresh usage
    reading (keepalive.fresh_usage, forced; the one the keep-alive's starts use) against
    REVIEW_NEEDS. -> None: enough quota; {"until", "why"}: postpone to `until`, the latest reset
    of the short meters + RESET_GRACE, on the minute. Raises failstop.Failed when the reading
    failed (fresh_usage has alerted and paused already) or a short meter has no reset ahead to
    postpone to: then the review can't be scheduled at all (D-235)."""
    u = ka.fresh_usage(now, force=True)
    if u is None:
        raise failstop.Failed("a review was not started: the usage reading failed",
                              f"{what} review; keepalive.fresh_usage failed (alerted, automation paused)")
    short = []
    for meter, need in REVIEW_NEEDS.items():
        m = u.get(meter)
        if not m:
            raise failstop.Failed("a review was not started: the usage reading has no meter",
                                  f"{what} review: no {meter} meter in the reading ({sorted(u)})")
        pct = float(m.get("percent") or 0)
        if 100 - pct >= need:
            continue
        reset = m.get("resets_at")
        if reset is None or reset <= now:
            raise failstop.Failed("a review can't be scheduled: no reset ahead with quota",
                                  f"{what} review: {meter} {pct:g}% used (it needs {need:g}% free), "
                                  f"resets_at {reset}")
        short.append((reset, f"{meter} {pct:g}% used (a review needs {need:g}% free), resets {ka.berlin(reset)}"))
    if not short:
        return None
    return {"until": ceil_minute(max(r for r, _ in short) + ka.RESET_GRACE), "why": "; ".join(w for _, w in short)}


def note_postponed(st, now, what, until, why, attempt):
    """A postponed start (not an error): kept in st["postponed"], logged, a progress note."""
    st.setdefault("postponed", []).append({"at": now.isoformat(), "review": what, "until": until.isoformat(),
                                           "why": why, "attempt": attempt})
    st["postponed"] = st["postponed"][-POSTPONED_KEPT:]
    ka.log(f"usage review: {what} review postponed to {ka.berlin(until)}: {why}")
    ka.progress_note(f"{what} review postponed to {ka.berlin(until)}, not enough quota now: {why}")


# ---------------------------------------------------------------- at jobs

def spool(when, args):
    """An at-shim job (docker/at_shim.py, its row in the DB's scheduled_jobs) that runs
    `usage_review.py <args>` at `when` (on the minute). -> dict(ok, at_utc, rc, out)."""
    when = when.astimezone(UTC)
    log_file = os.path.join(os.environ.get("KEEPALIVE_STATE_DIR", HERE), "keepalive.log")
    cmd = f"cd {HERE} && python3 usage_review.py {args} >> {log_file} 2>&1\n"
    r = subprocess.run(["at", "-t", when.strftime("%Y%m%d%H%M")], input=cmd, text=True, capture_output=True,
                       env=dict(ka.SCRUBBED_ENV, TZ="UTC"))
    return {"ok": r.returncode == 0, "at_utc": when.isoformat(), "rc": r.returncode,
            "out": (r.stderr or r.stdout).strip()[-300:]}


def only_args(which, attempt=1):
    return f"--only {which} --now" + (f" --attempt {attempt}" if attempt > 1 else "")


def spool_review(which, when, attempt):
    """A one-off review (or the D-228 chain, which=CHAIN) to start at `when`: a postponement or a
    usage-limit retry. A job that can't be spooled is an error (D-235): it could not be scheduled."""
    r = spool(when, only_args(which, attempt))
    if not r["ok"]:
        raise failstop.Failed(f"one-off {which} review could not be scheduled", json.dumps(r))
    ka.log(f"one-off {which} review scheduled for {ka.berlin(when)} (attempt {attempt}): {r['out']}")
    return r


def spool_chain_step(when):
    """The chain's next pass at `when` (an at job `usage_review.py --chain-step`), unless one is
    already pending (the job itself and the hourly backstop run both ask). A job that can't be
    spooled is an error (D-235). -> the at result, None if one was pending."""
    if any(CHAIN_STEP_ARG in (j.get("cmd") or "") for j in runner_state.jobs("pending")):
        return None
    r = spool(when, CHAIN_STEP_ARG)
    if not r["ok"]:
        raise failstop.Failed("the review chain's next step could not be scheduled", json.dumps(r))
    return r


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
    result = dict(spool(when, only_args(which)), which=which, at_local=local.isoformat())
    if result["ok"]:
        ka.log(f"one-off {which} review scheduled for {ka.berlin(when)}: {result['out']}")
    else:
        fail_stop(f"one-off {which} review scheduling failed", json.dumps(result))
    return result


# ---------------------------------------------------------------- launches

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
    """Fire the monthly review (start_monthly() checks the quota first)."""
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
    attempt, st["retry_attempt"] = st.get("retry_attempt") or 1, None   # None, not deleted: the state never deletes
    record_review(sid, ("usage", "stage-eta"), now, ok, rc, False, attempt=attempt)
    if ok:
        st["next_run_at"] = next_run_after(now).isoformat()
        ka.log(f"usage review started: {sid}")
        ka.progress_note(f"usage review started: session {sid} (Opus medium, tmux ka-{sid[:8]}); "
                         f"next {ka.berlin(datetime.fromisoformat(st['next_run_at']))}")
    else:
        fail_stop("usage review launch failed", f"rc={rc} out={out.strip()} err={err.strip()[-500:]}")


def run_once(st, now, which, extra=None, record=None):
    """A one-off review of just `which` (one ONLY_PROMPTS key: "stage-eta" or "usage"), fired
    right away in its OWN fresh session: its own prompt (not the regular periodic one; "usage"
    is the exception -- it reuses prompts/usage_review.md verbatim, since the one-off usage
    review IS the full monthly review, just logged as a one-off) + guard_respect + manager.
    `extra`, if given, is extra context inserted right after `which`'s own brief (D-228 chain:
    the ETA review's pointer at the usage review's result) -- never used for a solo run. Logged
    under st["one_off_runs"] -- separate from st["runs"]/next_run_at, so it never touches or
    delays the monthly schedule. `record`: extra fields of its usage_reviews record (attempt,
    chain, after_report). The quota check is the caller's (start_one_off, the chain). -> ok."""
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
    record_review(sid, (which,), now, ok, rc, True, **(record or {}))
    if ok:
        ka.log(f"one-off {which} review started: {sid}")
        ka.progress_note(f"one-off {which} review started: session {sid} (Opus medium, tmux ka-{sid[:8]})")
    else:
        fail_stop(f"one-off {which} review launch failed", f"rc={rc} out={out.strip()} err={err.strip()[-500:]}")
    return ok


def start_monthly(st, now):
    """The due monthly review (or --now): run() if the quota allows, else postponed (next_run_at =
    the point with quota; the hourly cron run starts it then). -> started."""
    q = quota_check(now, "monthly usage")
    if q:
        st["next_run_at"] = q["until"].isoformat()
        note_postponed(st, now, "monthly usage", q["until"], q["why"], st.get("retry_attempt") or 1)
        save_checked(st, "the postponed monthly review's next_run_at")
        return False
    run(st, now)
    save(st)
    return True


def start_one_off(st, now, which, attempt=1):
    """A solo one-off review (--only usage / stage-eta --now): run_once() if the quota allows, else
    postponed (an at job for the point with quota). -> started."""
    q = quota_check(now, f"one-off {which}")
    if q:
        spool_review(which, q["until"], attempt)
        note_postponed(st, now, f"one-off {which}", q["until"], q["why"], attempt)
        save(st)    # the job is in the DB already: a lost st["postponed"] note loses no step
        return False
    ok = run_once(st, now, which, record={"attempt": attempt})
    save(st)
    return ok


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
            f"'{CHAIN}'")
    return ",".join(k for k in ONLY_ORDER if k in parts)


# ---------------------------------------------------------------- is the review done / dead?

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
    """The review session `sid` has reliably finished (D-228): its report doc (review:<date>,
    report_ready()) exists AND the session itself has gone idle (session_idle()) -- both
    together, since either alone is weaker (the report could be mid-write; the session could
    just be slow to start). -> bool."""
    return report_ready(report) and session_idle(sid, now)


def limit_notice(text, ts):
    """keepalive.stall_info() on a limit text that is not the session's own notice (a failed
    worker agent's summary): -> {kind, reset_from_text, ...} or None."""
    return ka.stall_info({"type": "assistant", "message": {"model": "<synthetic>", "content": text},
                          "timestamp": ts.isoformat() if ts else None})


def failed_agent(path):
    """The newest failed worker agent in the session's transcript: its task notification
    (<status>failed</status>) -> (summary, time), or None."""
    hit = None
    for e in ka.iter_entries(path):
        if e.get("type") != "user" or e.get("isSidechain"):
            continue
        text = ka.message_text(e)
        m = FAILED_AGENT_RE.search(text) if "<task-notification>" in text else None
        if m:
            hit = (m.group("summary").strip(), ka.parse_ts(e.get("timestamp")))
    return hit


def agent_running(path, now):
    """A worker agent of the session is still running: Claude Code ends each turn with a
    turn_duration entry that counts the background agents still pending
    (pendingBackgroundAgentCount); without one after the last turn, a subagent transcript written
    within keepalive.SUBAGENT_QUIET (keepalive.compaction_busy_reason's test)."""
    pending = None
    for e in ka.iter_entries(path):
        if e.get("isSidechain"):
            continue
        if e.get("type") in ("user", "assistant"):
            pending = None          # a newer turn: its own end entry decides
        elif e.get("type") == "system" and e.get("subtype") == "turn_duration":
            pending = int(e.get("pendingBackgroundAgentCount") or 0)
    if pending is not None:
        return pending > 0
    subs = glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl"))
    newest = max((os.path.getmtime(f) for f in subs if os.path.exists(f)), default=None)
    return newest is not None and now - datetime.fromtimestamp(newest, UTC) < ka.SUBAGENT_QUIET


def review_death(sid, now):
    """Why review session `sid` is dead (it will never write its report), or None while it may
    still. The caller checks the report first: a review with its report is never dead. With the
    keep-alive's transcript readers: the session's last message is a usage-limit notice
    (keepalive.stall_status, the reset from the notice) or another API error; a worker agent
    failed (its task notification; at a usage limit or not); the session ended its turn
    (keepalive.session_idle) on an answer, with no agent running; or, quiet for
    keepalive.SUBAGENT_QUIET, its tmux session ka-<id8> is gone. No transcript yet: None (the
    deadlines catch a session that never starts). A failed host call raises (an error).
    -> None | {"kind": "limit", "limit": weekly/session/..., "reset", "why"} | {"kind": "dead", "why"}."""
    path = ka.transcript_path(sid)
    if not path:
        return None
    action, detail, stall = ka.stall_status(sid, now)
    if action in ("WAIT_RESET", "RESET_PASSED"):
        return {"kind": "limit", "limit": stall["kind"], "reset": stall["reset"],
                "why": f"the session stopped at the usage limit: {stall['text'][:200]!r}"}
    if action == "OTHER_ERROR":
        return {"kind": "dead", "why": detail}
    failed = failed_agent(path)
    if failed:
        summary, ts = failed
        hit = limit_notice(summary, ts)
        if hit:
            return {"kind": "limit", "limit": hit["kind"], "reset": hit["reset_from_text"],
                    "why": f"a worker agent stopped at the usage limit: {summary[:200]!r}"}
        return {"kind": "dead", "why": f"a worker agent failed: {summary[:300]!r}"}
    last = ka.last_message(path)
    if last and last.get("type") == "assistant" and ka.session_idle(path, now) and not agent_running(path, now):
        return {"kind": "dead", "why": "the session ended its turn without the report and with no agent running"}
    if session_idle(sid, now, idle=ka.SUBAGENT_QUIET) and not ka.tmux_alive(sid):
        return {"kind": "dead", "why": f"its tmux session {ka.tmux_name(sid)} is gone"}
    return None


def retry_review(st, rec, when, attempt, now):
    """Schedule the retry of review `rec` (it hit a usage limit) at `when`: the monthly one by its
    next_run_at, a solo one-off by an at job, the chain's usage leg as the whole chain again (an
    at job), its ETA leg as a pending chain step pointed at the same usage report."""
    if not rec.get("one_off"):
        st["next_run_at"] = when.isoformat()
        st["retry_attempt"] = attempt
        save_checked(st, "the monthly review's retry")
    elif rec.get("chain") == "usage":
        spool_review(CHAIN, when, attempt)
    elif rec.get("chain") == "stage-eta":
        if st.get("chain"):
            raise failstop.Failed("a review chain is already pending", f"the ETA leg's retry; pending: {st['chain']}")
        st["chain"] = {"step": "stage-eta", "report": rec.get("after_report") or rec.get("report_doc"),
                       "attempt": attempt, "not_before": when.isoformat(), "since": now.isoformat()}
        save_checked(st, "the review chain's ETA retry")
        spool_chain_step(when)
    else:
        spool_review((rec.get("reviews") or ["usage"])[0], when, attempt)


def handle_death(st, now, rec, death):
    """A dead review (review_death): its record final (limit / died), then: a usage limit on the
    first attempt is a known, recoverable cause, retried once after the limit's reset
    (retry_review), with an alert but no pause; a second limit death, a limit without a reset to
    retry at, or any other death is an error (fail-stop). -> the record."""
    sid = rec["review_id"]
    attempt = int(rec.get("attempt") or 1)
    label = review_label(rec)
    limit = death["kind"] == "limit"
    done = finish_review(sid, "limit" if limit else "died", now, why=death["why"]) or rec
    detail = f"session {sid} (tmux {ka.tmux_name(sid)}), attempt {attempt}: {death['why']}"
    if not limit:
        fail_stop("a review session died", f"{label} review, {detail}")
    elif attempt >= MAX_ATTEMPTS:
        fail_stop("a review hit the usage limit again", f"{label} review, {detail}; retried once already")
    elif death.get("reset") is None:
        fail_stop("a review hit the usage limit, no reset to retry at", f"{label} review, {detail}")
    else:
        when = ceil_minute(max(death["reset"], now) + ka.RESET_GRACE)
        retry_review(st, rec, when, attempt + 1, now)
        ka.log(f"usage review: {label} review {sid[:8]} hit the {death['limit']} usage limit; "
               f"retry {attempt + 1} at {ka.berlin(when)}")
        ka.alert(f"{label} review stopped at the usage limit: retried after the reset",
                 f"{detail}. Retry (attempt {attempt + 1} of {MAX_ATTEMPTS}) at {ka.berlin(when)}; not paused.")
    return done


def track_reviews(now, st=None):
    """The hourly check of the started reviews (all but the pending chain's usage leg, which
    chain_pass() checks): finished once the report doc exists and the session went idle; dead
    (review_death, handle_death) while it has no report; no_report, an error, once
    REVIEW_REPORT_DEADLINE passed. `st`: the state (loaded if None; a retry writes it). -> the
    records changed."""
    st = st if st is not None else load()
    chain = st.get("chain") or {}
    chain_sid = chain.get("session") if chain.get("step") == "usage" else None
    changed = []
    for rec in telemetry.read_records("usage.review"):
        if rec.get("final") or rec.get("status") != "started" or rec.get("review_id") == chain_sid:
            continue
        try:
            started = datetime.fromisoformat(rec["started_at"])
        except (KeyError, TypeError, ValueError):
            continue
        sid = rec["review_id"]
        ready = report_ready(rec.get("report_doc") or report_doc(started))
        if ready and session_idle(sid, now):
            changed.append(finish_review(sid, "finished", now))
        elif now - started >= REVIEW_REPORT_DEADLINE:
            changed.append(finish_review(sid, "no_report", now))
            fail_stop("a review did not finish in time",
                      f"{review_label(rec)} review, session {sid}, started {ka.berlin(started)}: report "
                      f"{'written but the session never went idle' if ready else 'missing'} after "
                      f"{REVIEW_REPORT_DEADLINE}")
        elif not ready:
            death = review_death(sid, now)
            if death:
                changed.append(handle_death(st, now, rec, death))
    return [c for c in changed if c]


# ---------------------------------------------------------------- the D-228 chain

def start_chain(st, now, attempt=1):
    """D-228 chain (--only usage,stage-eta): the quota check, then the one-off usage review, its
    pending step st["chain"] saved and the first chain step spooled (chain_pass does the rest:
    the ETA leg in a SEPARATE fresh session once the usage review has finished). Not enough quota:
    the whole chain postponed (an at job). A chain already pending: an error. -> started."""
    if st.get("chain"):
        raise failstop.Failed("a review chain is already pending", f"not starting a second one; pending: {st['chain']}")
    q = quota_check(now, f"one-off {CHAIN}")
    if q:
        spool_review(CHAIN, q["until"], attempt)
        note_postponed(st, now, f"one-off {CHAIN}", q["until"], q["why"], attempt)
        save(st)    # the job is in the DB already: a lost st["postponed"] note loses no step
        return False
    if not run_once(st, now, "usage", record={"attempt": attempt, "chain": "usage"}):
        save(st)
        return False
    sid = st["one_off_runs"][-1]["session"]
    st["chain"] = {"step": "usage", "session": sid, "report": report_doc(now), "started_at": now.isoformat(),
                   "attempt": attempt}
    save_checked(st, "the review chain's pending step")
    spool_chain_step(now + CHAIN_POLL)
    return True


def chain_usage_step(st, c, now):
    """The chain's usage leg: finished (report + idle) -> the ETA step is next; dead -> handled
    (handle_death: a usage limit retries the chain once, anything else is an error); not finished
    within USAGE_DONE_TIMEOUT -> an error, no ETA leg; else the next check in CHAIN_POLL.
    -> "finished" | "dead" | "timed_out" | "waiting"."""
    sid, report = c["session"], c["report"]
    if usage_review_finished(sid, report, now):
        finish_review(sid, "finished", now)
        st["chain"] = {"step": "stage-eta", "report": report, "attempt": 1, "not_before": None,
                       "usage_session": sid, "since": now.isoformat()}
        save_checked(st, "the review chain's ETA step")
        return "finished"
    found = report_ready(report)
    death = None if found else review_death(sid, now)
    if death:
        rec = dict(review_record(sid) or {}, review_id=sid, reviews=["usage"], one_off=True, chain="usage",
                   attempt=c.get("attempt", 1))
        st["chain"] = None
        save_checked(st, "the dead review chain")
        handle_death(st, now, rec, death)
        return "dead"
    if now - datetime.fromisoformat(c["started_at"]) >= USAGE_DONE_TIMEOUT:
        finish_review(sid, "timed_out" if not found else "finished", now)
        st["chain"] = None
        save_checked(st, "the timed-out review chain")
        fail_stop("one-off usage review (D-228 chain) did not finish in time for the ETA review",
                  f"session {sid}; waited {USAGE_DONE_TIMEOUT}; report doc {report} "
                  f"{'found' if found else 'missing'}; the ETA review was not launched")
        return "timed_out"
    spool_chain_step(now + CHAIN_POLL)
    return "waiting"


def chain_eta_step(st, c, now):
    """The chain's ETA leg: not before its not_before (a postponement or a limit retry); the quota
    check (short: postponed, a chain step at the point with quota); then the SEPARATE fresh ETA
    session, its brief pointed at the usage review's report. The pending step is cleared before
    the launch, so a crash after it never fires the leg twice. -> "waiting" | "postponed" |
    "eta_started" | "eta_failed"."""
    nb = datetime.fromisoformat(c["not_before"]) if c.get("not_before") else None
    if nb and now < nb:
        spool_chain_step(nb)
        return "waiting"
    q = quota_check(now, "one-off stage-eta (D-228 chain)")
    if q:
        c["not_before"] = q["until"].isoformat()
        note_postponed(st, now, "one-off stage-eta (D-228 chain)", q["until"], q["why"], c.get("attempt", 1))
        save_checked(st, "the review chain's postponed ETA step")
        spool_chain_step(q["until"])
        return "postponed"
    report = c["report"]
    extra = ka.load_prompt("stage_eta_review_once_after_usage").format(
        report=f"the review doc {report} in the AFClaude DB (`python3 tasks.py doc show {report}`)")
    st["chain"] = None      # None, never deleted: the usage_review state never deletes a key
    save_checked(st, "the review chain's ETA launch")
    ok = run_once(st, now, "stage-eta", extra=extra,
                  record={"attempt": c.get("attempt", 1), "chain": "stage-eta", "after_report": report})
    save(st)
    return "eta_started" if ok else "eta_failed"


def chain_pass(st, now):
    """One pass over the pending D-228 chain (st["chain"]): from its own at job (--chain-step,
    every CHAIN_POLL) and from every hourly cron run (the backstop after a restart), under lock().
    While automation is paused it waits (no at job: the hourly run picks it up once the pause is
    cleared). -> what happened (None: no chain)."""
    c = st.get("chain")
    if not c:
        return None
    pause = failstop.paused()
    if pause["paused"]:
        ka.log(f"usage review: the review chain waits, automation is paused ({pause.get('reason') or 'no reason given'})"
               "; the hourly run goes on once the pause is cleared")
        return "paused"
    if c["step"] == "usage":
        r = chain_usage_step(st, c, now)
        if r != "finished":
            return r
    return chain_eta_step(st, st["chain"], now)


def main():
    try:
        return _main()
    except failstop.Failed as e:
        fail_stop(e.why, e.detail)
    except Exception as e:   # noqa: BLE001 - any other failure: an error (D-234/D-235)
        import traceback
        fail_stop(f"usage_review.py crashed: {type(e).__name__}", f"{e}\n{traceback.format_exc()[-1500:]}")
    return 1


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--only", type=parse_only,
                    help="run (or, with --at, schedule) only this review, once, outside the monthly schedule: "
                         f"'stage-eta', 'usage', or the chained '{CHAIN}' (D-228)")
    ap.add_argument("--at", metavar="\"YYYY-MM-DD HH:MM\"",
                    help="with --only: schedule it for this Europe/Berlin time via the at-shim, instead of "
                         "running it now")
    ap.add_argument("--attempt", type=int, default=1,
                    help="with --only: the attempt (2 = the one retry after a usage limit; set by the retry's at job)")
    ap.add_argument(CHAIN_STEP_ARG, action="store_true",
                    help="advance the pending D-228 chain (its own at job runs this)")
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
    fh = lock()
    try:
        st = load()      # under the lock: what the last pass saved
        now = datetime.now(UTC)
        return _locked(args, st, now)
    finally:
        ka.release_lock(fh)


def _locked(args, st, now):
    for rec in track_reviews(now, st):
        ka.log(f"usage review {rec['review_id'][:8]} ({'+'.join(rec.get('reviews') or [])}): {rec['status']}")
    pause = failstop.paused()     # the install-wide pause only: the reviews are no one project's (D-244)
    if pause["paused"] and (args.only or args.now or now >= datetime.fromisoformat(st["next_run_at"])):
        why = pause.get("reason") or "no reason given"
        if args.only:     # its at job is used up: say so, it has to be scheduled again
            ka.log(f"usage review: one-off {args.only} review NOT started, automation is paused ({why})")
            ka.alert(f"one-off {args.only} review not started: automation is paused",
                     f"Paused: {why}. Schedule it again (usage_review.py --only {args.only} --at ...) once the "
                     f"pause is cleared ({failstop.RESUME_CMD}).")
        else:             # the monthly one: the hourly cron run starts it once the pause is cleared
            ka.log(f"usage review: due, but automation is paused ({why}); it starts once the pause is cleared")
        return 0
    if args.only:
        if args.only == CHAIN:
            start_chain(st, now, args.attempt)
        else:
            start_one_off(st, now, args.only, args.attempt)
        return
    chain_pass(st, now)          # --chain-step, and every hourly run (the backstop)
    if args.chain_step:
        return
    if args.now or now >= datetime.fromisoformat(st["next_run_at"]):
        start_monthly(st, now)
    elif not stored():
        save(st)


if __name__ == "__main__":
    sys.exit(main())
