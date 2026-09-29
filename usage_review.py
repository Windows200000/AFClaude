#!/usr/bin/env python3
"""Periodic usage-model review (user rules, 2026-09-29).

ONE run that tests hypotheses on data/ (samples, Haiku judgements, weekly cycles,
keep-alive decisions), then sorts every finding into universal (goes into the code,
public) or user-specific (data/user_model.json, local only). It starts a NEW
Opus 5.5 session at MEDIUM effort (tmux, Remote-Control visible, via ka_resume.sh)
with prompts/usage_review.md + guard_respect.md + manager.md, and runs without user
input. The results are surfaced via PROGRESS.md, a push, and an "unread review"
memory entry that the user's next conversation picks up.

Schedule: at `next_run_at` in the state file. The first run is Thu 2026-10-01 17:00
Berlin, 2 h before that week's reset, so it spends quota that would expire anyway.
It deliberately IGNORES the weekly budget rule and the night window. Each later run
is 2 h before the first weekly reset that is at least MIN_GAP_DAYS after the
previous run. Cron checks hourly.

  usage_review.py            # cron mode: run if next_run_at has passed
  usage_review.py --status
  usage_review.py --now      # run immediately
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402

UTC = timezone.utc
STATE = os.path.join(HERE, "data", "usage_review_state.json")
FIRST_RUN = datetime(2026, 10, 1, 17, 0, tzinfo=ka.BERLIN)
MIN_GAP_DAYS = 28                    # "once a month, or every other month"; tune here
BEFORE_RESET = timedelta(hours=2)
MODEL, EFFORT = "claude-opus-5-5", "medium"


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"next_run_at": FIRST_RUN.isoformat(), "runs": []}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=1)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", action="store_true")
    args = ap.parse_args()
    st = load()
    now = datetime.now(UTC)
    if args.status:
        print(json.dumps(st, indent=1), "\nnext:", ka.berlin(datetime.fromisoformat(st["next_run_at"])))
        return
    if args.now or now >= datetime.fromisoformat(st["next_run_at"]):
        run(st, now)
        save(st)
    elif not os.path.exists(STATE):
        save(st)


if __name__ == "__main__":
    main()
