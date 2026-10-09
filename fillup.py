"""Fill-up runs (owner decision D-212): use the last percentages of an AFClaude session window.

The task-manager stops cleanly at the session_stop_pct setting (default 95%, D-014), so a run
leaves the rest of its session window unused. D-212 (owner, verbatim): "While stopping at 95 was a
rule, i think, from the session fill time and session to week ratio, dynamically calculate time it
takes to fill the last percentages (wether set to 95 or 90 or whatever). Then do a fillup run at
t = reset - calculated_time*factor. Set factor default to 110%, but keep editable and allow option
to disable".

As implemented ("reset" = the SESSION reset of the session window the run used):
    remaining  = 100 - session % (after the run ended early)
    rate       = median session-%/h of the recent runs (run_metrics, data/afclaude_runs.jsonl),
                 else DEFAULT_RATE
    time       = remaining / rate
    start      = session reset - time x fillup_factor (default 1.10)
    weekly cost = remaining x the session-to-week ratio (pacing.ratio_info / limit_ratio)
The keep-alive watcher plans and fires it once per session window (dedup key fillup-<reset>).
D-202 says runs begin only at session-window starts: the fill-up is the owner's explicit
exception (D-212); it continues a run's own session window, it never opens a new one.

Not when: fillup_enabled is false; automation_paused; the run reached the session limit (D-204);
the reset is outside the night window and not in the last stretch; a session-window start falls
strictly before the reset by more than the fill-up duration (that start would itself use this
same session window, so a separate fill-up is unneeded) -- a start at or after the reset opens a
NEW session window and does not excuse the fill-up (D-219); the weekly cost exceeds what is left
of the run's budget; the owner is active (D-018: wait for idle, but never past the reset); too
close to the reset.

decide() is pure; keepalive.fillup_pass() gathers its inputs and fires through handle_fire.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

UTC = timezone.utc
DEFAULT_RATE = 60.0                     # session-%/h without run data
RECENT_RUNS = 10                        # the rate is the median over this many recent runs with a rate
MIN_LEFT = timedelta(minutes=1)         # no fill-up this close to the reset (a 3% fill-up starts ~3 min before)
MIN_REMAINING = 1.0                     # session % worth a fill-up
BUDGET_SLACK = 1.0                      # weekly % (the weekly meter is an integer)
FIRE_SLACK = timedelta(minutes=5)       # a run fired this long before the window opened still used it
SESSION_LENGTH = timedelta(hours=5)     # afclaude_config.SESSION_LENGTH
RUN_RESULTS = ("continued-in-place", "no-reply-within-timeout")   # keepalive.RUN_RESULTS
KEY_PREFIX = "fillup-"
TEST_PREFIX = "fillup-test-"
_BUDGET_RE = re.compile(r"budget for this run \+?(?:about \+)?([\d.]+)")


def _ts(x: Any) -> Optional[datetime]:
    if isinstance(x, datetime):
        return x
    if not isinstance(x, str) or not x:
        return None
    try:
        t = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _num(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def round_reset(t: datetime) -> datetime:
    """resets_at jitters by milliseconds between fetches: round to the minute."""
    r = t.astimezone(UTC) + timedelta(seconds=30)
    return r.replace(second=0, microsecond=0)


def key(reset: datetime) -> str:
    """Dedup key: one fill-up per session window (per session reset)."""
    return KEY_PREFIX + round_reset(reset).isoformat()


def test_key(reset: datetime) -> str:
    return TEST_PREFIX + round_reset(reset).isoformat()


def fill_rate(rows: Sequence[Any], n: int = RECENT_RUNS) -> tuple[float, str]:
    """-> (session-%/h, source): the median rate of the last n runs that have one, else DEFAULT_RATE."""
    rated = [r for r in rows if isinstance(r, dict) and (_num(r.get("rate_pct_per_h")) or 0) > 0]
    rated.sort(key=lambda r: str(r.get("start") or ""))
    rates = [float(r["rate_pct_per_h"]) for r in rated[-n:]]
    if not rates:
        return DEFAULT_RATE, f"default {DEFAULT_RATE:g} %/h (no measured runs)"
    return float(statistics.median(rates)), f"median of the last {len(rates)} runs"


def fill_time(remaining: float, rate: float) -> timedelta:
    return timedelta(hours=max(remaining, 0.0) / rate) if rate > 0 else timedelta(0)


def start_time(reset: datetime, remaining: float, rate: float, factor: float) -> datetime:
    """THE formula (D-212): reset - remaining / rate x factor."""
    return reset - fill_time(remaining, rate) * factor


def run_budget(rec: Mapping[str, Any]) -> Optional[float]:
    """The weekly % the run was granted: the fire record's budget_pct, else parsed from its reason."""
    b = _num(rec.get("budget_pct"))
    if b is not None:
        return b
    m = _BUDGET_RE.search(str(rec.get("reason") or ""))
    return float(m.group(1)) if m else None


def window_run(handled: Mapping[str, Any], reset: datetime,
               length: timedelta = SESSION_LENGTH) -> Optional[tuple[str, dict[str, Any]]]:
    """The latest real fire (not a fill-up) inside the session window that resets at `reset`."""
    best: Optional[tuple[str, dict[str, Any]]] = None
    best_at: Optional[datetime] = None
    for k, rec in handled.items():
        if not isinstance(rec, dict) or k.startswith(KEY_PREFIX) or rec.get("result") not in RUN_RESULTS:
            continue
        at = _ts(rec.get("at"))
        if at is None or not reset - length - FIRE_SLACK <= at < reset:
            continue
        if best_at is None or at > best_at:
            best, best_at = (k, rec), at
    return best


def _weekly_at_fire(rec: Mapping[str, Any]) -> Optional[float]:
    u = rec.get("usage")
    if not isinstance(u, dict) or not isinstance(u.get("weekly"), dict):
        return None
    return _num(u["weekly"].get("percent"))


def plan(reset: datetime, session_pct: float, rows: Sequence[Any], factor: float, ratio: float) -> dict[str, Any]:
    """The numbers of a fill-up (pure): remaining, rate, time, start, weekly cost."""
    remaining = max(100.0 - session_pct, 0.0)
    rate, src = fill_rate(rows)
    t = fill_time(remaining, rate)
    return {"reset": reset, "remaining": remaining, "rate": rate, "rate_src": src, "factor": factor,
            "time_min": t.total_seconds() / 60, "start": reset - t * factor, "ratio": ratio,
            "cost": remaining * ratio}


def decide(*, now: datetime, session_pct: Optional[float], session_reset: Optional[datetime],
           weekly_pct: Optional[float], handled: Mapping[str, Any], rows: Sequence[Any], ratio: float,
           enabled: bool, factor: float, paused: bool, run_active: bool, limit_hit: bool,
           in_night: bool, in_last_stretch: bool, next_start: Optional[datetime],
           user_active: bool, user_recheck: Optional[datetime],
           budget_fallback: Callable[[], Optional[float]]) -> dict[str, Any]:
    """The fill-up decision for the current session window (pure). -> dict status (off | none |
    skip | wait | hold | done | fire), reason, key, recheck_at and the plan numbers (plan())."""
    if not enabled:
        return {"status": "off", "reason": "fillup_enabled is off"}
    if session_reset is None or session_reset <= now or session_pct is None:
        return {"status": "none", "reason": "no live session window"}
    k = key(session_reset)
    if k in handled:
        return {"status": "done", "key": k, "reason": "this session window's fill-up was already handled"}
    run = window_run(handled, session_reset)
    if run is None:
        return {"status": "none", "key": k, "reason": "no AFClaude run in this session window"}
    out: dict[str, Any] = {"key": k, "run": run[0], **plan(session_reset, session_pct, rows, factor, ratio)}

    def res(status: str, reason: str, recheck: Optional[datetime] = None) -> dict[str, Any]:
        return dict(out, status=status, reason=reason, recheck_at=recheck)
    if limit_hit or session_pct >= 100:
        return res("skip", "the run reached the session limit (D-204: nothing continues after it)")
    if not in_night and not in_last_stretch:
        return res("skip", "the session reset is outside the night window and not in the last stretch")
    if out["remaining"] < MIN_REMAINING:
        return res("skip", f"only {out['remaining']:.0f}% left in this session window")
    if next_start is not None and next_start < session_reset:
        duration = timedelta(minutes=out["time_min"])
        if session_reset - next_start > duration:
            # that start happens early enough in THIS window that it would use up the
            # remaining % itself; a start at/after the reset opens a new window instead
            # and never excuses the fill-up (D-219).
            return res("skip", f"the session-window start at {next_start.isoformat()} continues this window anyway")
    budget = run_budget(run[1])
    w0 = _weekly_at_fire(run[1])
    spent = (weekly_pct - w0) if budget is not None and weekly_pct is not None and w0 is not None else 0.0
    if budget is None:
        budget, spent = budget_fallback(), 0.0
    out.update(budget=budget, spent=spent)
    if budget is None or out["cost"] > budget - spent + BUDGET_SLACK:
        return res("skip", f"weekly cost {out['cost']:.1f}% exceeds what is left of the run's budget "
                           f"({'unknown' if budget is None else f'{budget - spent:.1f}%'})")
    if run_active:
        return res("wait", "the run is still going", now + timedelta(minutes=1))
    if now < out["start"]:
        return res("wait", "planned", out["start"])
    if session_reset - now < MIN_LEFT:
        return res("skip", "too close to the session reset")
    if paused:
        return res("hold", "automation_paused", now + timedelta(minutes=1))
    if user_active:
        if user_recheck is not None and user_recheck < session_reset - MIN_LEFT:
            return res("hold", "the owner is active (D-018 yield)", user_recheck)
        return res("skip", "the owner is active and idle only after the reset (D-018: not postponed past it)")
    return res("fire", "due")


def decide_test(*, now: datetime, test: Mapping[str, Any], handled: Mapping[str, Any],
                paused: bool) -> dict[str, Any]:
    """The --plan-fillup-test entry (owner live test): fire at its start, bypassing the run /
    window / budget conditions; still dedup, automation_paused (and in the caller db_paused,
    preflight). -> dict status (wait | hold | done | expired | fire)."""
    k = str(test.get("key") or "")
    start, reset = _ts(test.get("start")), _ts(test.get("reset"))
    base = {"key": k, "start": start, "reset": reset, "remaining": test.get("remaining"), "test": True}
    if not k or start is None or reset is None:
        return dict(base, status="expired", reason="broken test entry")
    if k in handled:
        return dict(base, status="done", reason="test fill-up already handled")
    if now >= reset:
        return dict(base, status="expired", reason="the session reset has passed")
    if now < start:
        return dict(base, status="wait", reason="planned (TEST)", recheck_at=start)
    if paused:
        return dict(base, status="hold", reason="automation_paused", recheck_at=now + timedelta(minutes=1))
    return dict(base, status="fire", reason="due (TEST)")
