#!/usr/bin/env python3
"""Stage ETAs (owner decision D-213): when each open project stage is predicted to be done,
cumulative (this stage in 3 days, the next in 4, the one after in 6, ...).

The owner's formula (D-213 addendum): "Since data from user usage prediction can be used, the only
thing that needs to be estimated is how many sessions it needs. Then you can just run: sessions
required/(total available-user usage)".

    sessions required   per stage (a task of the task store = a stage of its project), in execution
                        order (store.TASK_ORDER_SQL: priority level, project rank, stage_seq), from
                        1. the stage's own estimate: tasks.estimated_sessions (actions.py task.edit /
                           task.update, MCP afclaude_update_task, tasks.py edit --sessions; the
                           task-manager may set it, D-185; the stage-ETA review tunes it),
                        2. else the default from history: the median session windows per finished
                           stage (AFClaude's runs in data/afclaude_runs.jsonl, run_metrics, spread
                           over the stages that were in progress during each run), once at least
                           MIN_HISTORY stages have a measurement,
                        3. else FALLBACK_SESSIONS (one full session window per stage).
    available capacity  AFClaude's session windows as the night gate would actually allow them
                        (D-141, D-202/D-203, D-020), with pacing's forecast of the user's use (the
                        same prediction the gate uses; pacing.py is reused, not duplicated): every
                        session-window start of the night windows (schedule.py) until the weekly
                        reset passes iff weekly % then (now + the user's forecast use until then +
                        AFClaude's runs before it) + its cost + the user's forecast until the reset
                        <= the reserve threshold (no forecast: the straight line); then the last
                        stretch fills what is left (100 - weekly % - the user's forecast during it)
                        in its slot starts; later weeks the same from 0%. A session window's
                        weekly cost = ratio x 100 (limit_ratio.preferred_ratio via pacing).
    ETA                 the stages' cumulative sessions laid onto those slots in order: stage k is
                        done in the slot where the capacity reaches the sum of stages 1..k.

Summary rate (the tooltip): sessions/day = the capacity of a full typical week / 7, next to the
owner's formula as written: (100 - the user's forecast week) / the weekly % of one session window.

predict() is pure (stages + Gate in, a JSON-ready dict out). gather() reads the live inputs
(the task store, pacing's ratio/forecast/threshold, the schedule settings, the run history).
record() appends one prediction per day to data/stage_eta_log.jsonl, score() compares the logged
predictions with when the stages were really done (the rare stage-ETA review, prompts/
stage_eta_review.md, run by the monthly usage review: D-142 analogue, it tunes the estimates only).
Phase 2c (telemetry.py): each logged prediction also goes into the DB table stage_eta_log
(dual-write); --score reads the live log from the DB once it is imported.

    python3 stage_eta.py [--db PATH]            the current prediction (JSON)
    python3 stage_eta.py --score [--db PATH]    the logged predictions vs the real completions
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, cast

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schedule  # noqa: E402
import telemetry  # noqa: E402

UTC = timezone.utc
WEEK = timedelta(days=7)
DAY = timedelta(days=1)
FALLBACK_SESSIONS = 1.0     # session windows per stage without an estimate or enough history
MIN_HISTORY = 3             # measured finished stages before the history default is used
MAX_WEEKS = 26              # horizon: a stage beyond it is "?" (unknown)
EPS = 1e-9
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
LOG_FILE = os.path.join(DATA_DIR, "stage_eta_log.jsonl")
RUNS_FILE = os.path.join(DATA_DIR, "afclaude_runs.jsonl")
OPEN = ("pending", "in_progress", "blocked")
KINDS = ("task",)           # questions and backlog projects are not worked as stages

Forecast = Callable[[datetime, datetime], float]   # the user's forecast weekly % in [a, b)


@dataclass(frozen=True)
class Stage:
    id: int
    title: str
    project: Optional[str] = None
    status: str = "pending"
    estimated: Optional[float] = None     # tasks.estimated_sessions


@dataclass(frozen=True)
class Gate:
    """What the night gate sees (pacing.decide's inputs): the weekly % now and its reset, the
    session/weekly ratio, the reserve threshold (resolved to a %), the user's forecast (None = the
    straight line), the schedule, the last-stretch setting and pacing_min_gap; the live session
    window (its % and reset) if one is open. sources = where each input came from (the tooltip)."""
    weekly_pct: Optional[float]
    resets_at: Optional[datetime]
    ratio: float
    threshold: float
    forecast: Optional[Forecast]
    sched: schedule.Config
    lm_setting: str | float = "auto"
    min_gap: float = 1.0
    session_pct: Optional[float] = None
    session_resets_at: Optional[datetime] = None
    sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Slot:
    """AFClaude capacity: `sessions` session windows (<= 1) starting at `at`."""
    at: datetime
    sessions: float
    kind: str        # night | last_stretch


# ------------------------------------------------------------------ pacing (reused, not duplicated)

def _last_mile_hours(weekly_pct: float, ratio: float, setting: str | float) -> float:
    import pacing
    fn = cast(Callable[[float, float, Any], float], pacing.last_mile_hours)
    return float(fn(weekly_pct, ratio, setting))


def _full_session_cost(ratio: float) -> float:
    import pacing
    return float(cast(Callable[[float], float], pacing.full_session_cost)(ratio))


def _no_usage(a: datetime, b: datetime) -> float:
    return 0.0


# ------------------------------------------------------------------ estimates

def history_default(done: Sequence[tuple[datetime, datetime]],
                    runs: Sequence[tuple[datetime, datetime, float]]) -> tuple[Optional[float], int]:
    """Median session windows per finished stage. done = (started, done) per finished stage; runs =
    (start, end, session windows used = session % / 100) per AFClaude run. A run's sessions are
    split evenly over the finished stages whose [started, done] overlaps it; a stage no run
    overlapped (done interactively) has no measurement. -> (median | None below MIN_HISTORY, n)."""
    per = [0.0] * len(done)
    for r0, r1, used in runs:
        if used <= 0:
            continue
        hit = [i for i, (a, b) in enumerate(done) if a <= r1 and b >= r0 and b > a]
        for i in hit:
            per[i] += used / len(hit)
    measured = [x for x in per if x > 0]
    if len(measured) < MIN_HISTORY:
        return None, len(measured)
    return float(statistics.median(measured)), len(measured)


def sessions_for(stages: Sequence[Stage], default: Optional[float]) -> list[tuple[Stage, float, str]]:
    """(stage, sessions required, source): its own estimate, else the history default, else
    FALLBACK_SESSIONS."""
    out = []
    for s in stages:
        if s.estimated is not None and s.estimated >= 0:
            out.append((s, float(s.estimated), "estimate"))
        elif default is not None:
            out.append((s, default, "history"))
        else:
            out.append((s, FALLBACK_SESSIONS, "fallback"))
    return out


# ------------------------------------------------------------------ capacity (the night gate)

def _week(g: Gate, t0: datetime, w0: float, reset: datetime, fc: Forecast, live: bool) -> list[Slot]:
    """AFClaude's slots in [t0, reset) with the week at w0 % at t0."""
    if w0 >= 100:
        return []
    full = _full_session_cost(g.ratio)

    def nights(lm: datetime) -> tuple[list[Slot], float]:
        out, used = [], 0.0
        for s, win in schedule.session_slots(t0, lm, g.sched):
            ws = w0 + fc(t0, s) + used
            if ws >= 100:
                break
            cost = full
            if live and g.session_pct is not None and g.session_resets_at is not None and g.session_resets_at > s:
                cost = g.ratio * max(100.0 - g.session_pct, 0.0)
            cost = min(cost, 100.0 - ws)
            if g.forecast is not None:
                ok = ws + cost + fc(s, reset) <= g.threshold
            else:   # pacing's straight line: threshold x the elapsed share of the week at the window end
                end = min(win.end, reset)
                ok = ws + cost <= g.threshold * min(max((end - (reset - WEEK)) / WEEK, 0.0), 1.0)
            if ok and cost > g.min_gap:
                out.append(Slot(s, cost / full, "night"))
                used += cost
        return out, used

    # the last stretch's length depends on the week at its start, which the nights before it change
    h = _last_mile_hours(w0, g.ratio, g.lm_setting)
    for _ in range(4):
        lm = max(reset - timedelta(hours=h), t0) if h > 0 else reset
        night, used = nights(lm)
        h2 = _last_mile_hours(min(w0 + fc(t0, lm) + used, 100.0), g.ratio, g.lm_setting)
        if h2 == h:
            break
        h = h2
    lm = max(reset - timedelta(hours=h), t0) if h > 0 else reset
    night, used = nights(lm)
    out = list(night)
    if h > 0:
        left = max(100.0 - (w0 + fc(t0, lm) + used) - fc(lm, reset), 0.0) / full
        at = lm
        while left > EPS and at < reset:
            x = min(1.0, left)
            out.append(Slot(at, x, "last_stretch"))
            left -= x
            at += g.sched.session
    return out


def capacity_slots(g: Gate, now: datetime, until: datetime) -> list[Slot]:
    """AFClaude's predicted session windows from now until `until`, week by week (the current week
    from the weekly % now, later weeks from 0%). [] if the weekly usage is unknown or stale."""
    if g.weekly_pct is None or g.resets_at is None or g.resets_at <= now:
        return []
    fc = g.forecast or _no_usage
    out: list[Slot] = []
    t0, w0, reset, live = now, float(g.weekly_pct), g.resets_at, True
    while t0 < until:
        out += _week(g, t0, w0, reset, fc, live)
        t0, w0, reset, live = reset, 0.0, reset + WEEK, False
    return [s for s in out if s.at < until]


# ------------------------------------------------------------------ prediction

def eta_label(days: Optional[float]) -> str:
    """'~3 d' ('<1 d' below a day), '?' unknown."""
    if days is None:
        return "?"
    if days < 1:
        return "<1 d"
    return f"~{round(days)} d"


def _iso(t: Optional[datetime]) -> Optional[str]:
    return t.astimezone(UTC).isoformat(timespec="minutes").replace("+00:00", "Z") if t is not None else None


def _r(x: Optional[float], k: int = 2) -> Optional[float]:
    return round(x, k) if x is not None else None


def predict(stages: Sequence[Stage], g: Gate, now: datetime, default: Optional[float] = None,
            default_info: str = "", max_weeks: int = MAX_WEEKS) -> dict[str, Any]:
    """The cumulative ETA of each open stage (in the given order). -> {status ok|unknown, reason,
    stages: [{id, title, project, status, sessions, source, cum_sessions, done_at, eta_days, eta,
    this_week}], assumptions: {...}}."""
    req = sessions_for(stages, default)
    total = sum(x for _, x, _ in req)
    full = _full_session_cost(g.ratio)
    fc = g.forecast or _no_usage
    srcs = {k: sum(1 for _, _, s in req if s == k) for k in ("estimate", "history", "fallback")}
    a: dict[str, Any] = {
        "sessions_total": _r(total), "ratio": _r(g.ratio, 4), "full_session_cost": _r(full, 1),
        "threshold": _r(g.threshold, 1), "default_sessions": _r(default) if default is not None else None,
        "fallback_sessions": FALLBACK_SESSIONS, "default_info": default_info, "estimate_sources": srcs,
        "order": "execution order: priority level, project rank, stage", "sources": dict(g.sources),
        "sessions_per_day": None, "gate_sessions_per_week": None, "formula_sessions_per_week": None,
        "this_week_sessions": None, "user_forecast_week": None}
    out: dict[str, Any] = {"status": "ok", "reason": None, "now": _iso(now), "stages": [], "assumptions": a}
    unknown = g.weekly_pct is None or g.resets_at is None or g.resets_at <= now
    slots: list[Slot] = []
    if not unknown and g.resets_at is not None:
        typ0 = g.resets_at
        slots = capacity_slots(g, now, max(now + max_weeks * WEEK, typ0 + WEEK + timedelta(seconds=1)))
        week = sum(s.sessions for s in slots if typ0 <= s.at < typ0 + WEEK)
        user = fc(typ0, typ0 + WEEK) if g.forecast is not None else None
        a.update(this_week_sessions=_r(sum(s.sessions for s in slots if s.at < typ0)),
                 gate_sessions_per_week=_r(week), sessions_per_day=_r(week / 7, 3),
                 user_forecast_week=_r(user, 1),
                 formula_sessions_per_week=_r(max(100.0 - (user or 0.0), 0.0) / full) if full > 0 else None)
        slots = [s for s in slots if s.at < now + max_weeks * WEEK]
        if week <= EPS:
            out.update(status="unknown", reason="no AFClaude capacity in a typical week (the night gate passes "
                                                "no session window and nothing is left for the last stretch)")
    else:
        out.update(status="unknown", reason="weekly usage unknown or stale (no reset time)")
    cum, used, i = 0.0, 0.0, 0
    for s, x, src in req:
        cum += x
        done: Optional[datetime] = None
        if not unknown:
            if cum <= EPS:
                done = now
            else:
                while i < len(slots) and used + slots[i].sessions < cum - EPS:
                    used += slots[i].sessions
                    i += 1
                if i < len(slots):
                    done = slots[i].at + (cum - used) * g.sched.session
        days = (done - now) / DAY if done is not None else None
        out["stages"].append({
            "id": s.id, "title": s.title, "project": s.project, "status": s.status, "sessions": _r(x),
            "source": src, "cum_sessions": _r(cum), "done_at": _iso(done), "eta_days": _r(days, 1),
            "eta": eta_label(days),
            "this_week": bool(done is not None and g.resets_at is not None and done <= g.resets_at)})
    return out


def group_eta(result: Mapping[str, Any], ids: Iterable[int]) -> Optional[dict[str, Any]]:
    """The ETA of a group of stages (a dashboard phase): when its last open stage is done.
    None if none of `ids` is open; eta '?' if any of them is unknown."""
    by = {st["id"]: st for st in result.get("stages", [])}
    mine = [by[i] for i in ids if i in by]
    if not mine:
        return None
    if any(st["eta_days"] is None for st in mine):
        return {"eta_days": None, "eta": "?", "done_at": None, "sessions": _r(sum(st["sessions"] for st in mine))}
    last = max(mine, key=lambda st: float(st["eta_days"]))
    return {"eta_days": last["eta_days"], "eta": last["eta"], "done_at": last["done_at"],
            "sessions": _r(sum(st["sessions"] for st in mine))}


def assumptions_text(result: Mapping[str, Any]) -> str:
    """One tooltip line: the rate, capacity and estimate sources behind the ETAs."""
    a = result.get("assumptions") or {}
    if result.get("status") != "ok":
        return f"ETA unknown: {result.get('reason')}"
    src = a.get("estimate_sources") or {}
    d = a.get("default_sessions")
    return (f"ETA = cumulative sessions / AFClaude capacity (D-213): ~{a.get('sessions_per_day')} session windows/day "
            f"({a.get('gate_sessions_per_week')}/week the night gate + last stretch allow; owner's formula "
            f"(100 - user forecast {a.get('user_forecast_week')}%) / {a.get('full_session_cost')}% per session = "
            f"{a.get('formula_sessions_per_week')}/week; this week {a.get('this_week_sessions')} left). "
            f"Ratio {a.get('ratio')}, threshold {a.get('threshold')}%. Estimates: {src.get('estimate', 0)} set, "
            f"{src.get('history', 0)} history{f' ({d})' if d is not None else ''}, {src.get('fallback', 0)} fallback ({a.get('fallback_sessions')} "
            f"session/stage); {a.get('sessions_total')} sessions open.")


# ------------------------------------------------------------------ log + score (the rare review)

def record(result: Mapping[str, Any], path: Optional[str] = None, now: Optional[datetime] = None) -> bool:
    """Append the prediction to data/stage_eta_log.jsonl, at most once per UTC day. -> written."""
    path = path or LOG_FILE
    now = now or datetime.now(UTC)
    day = now.astimezone(UTC).date().isoformat()
    last = None
    try:
        with open(path) as fh:
            for ln in fh:
                if ln.strip():
                    last = ln
    except OSError:
        pass
    if last is not None:
        try:
            if str(json.loads(last).get("at", ""))[:10] == day:
                return False
        except (ValueError, AttributeError):
            pass
    a = result.get("assumptions") or {}
    row = {"at": _iso(now), "status": result.get("status"), "sessions_per_day": a.get("sessions_per_day"),
           "default_sessions": a.get("default_sessions"),
           "stages": [{k: st.get(k) for k in ("id", "sessions", "source", "cum_sessions", "done_at", "eta_days")}
                      for st in result.get("stages", [])]}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(row, separators=(",", ":"))
    with open(path, "a") as fh:
        fh.write(line + "\n")
    telemetry.record("stage_eta.prediction", line, path, actor="runner:quickview")   # the live file only
    return True


def _ts(x: object) -> Optional[datetime]:
    if not isinstance(x, str) or not x:
        return None
    try:
        t = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def score(log_rows: Iterable[Mapping[str, Any]], done: Mapping[int, datetime],
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Logged predictions vs reality. done = {stage id: when it was done}. Per prediction of a
    stage that is done now: error = actual - predicted (days; > 0 = later than predicted); a
    stage still open past its predicted time counts as overdue. -> {n, bias_days, mae_days,
    overdue, per_stage: {id: {n, bias_days}}}."""
    now = now or datetime.now(UTC)
    errs: list[float] = []
    per: dict[int, list[float]] = {}
    overdue = 0
    for row in log_rows:
        for st in row.get("stages") or []:
            p = _ts(st.get("done_at"))
            if p is None:
                continue
            sid = st.get("id")
            if isinstance(sid, int) and sid in done:
                e = (done[sid] - p) / DAY
                errs.append(e)
                per.setdefault(sid, []).append(e)
            elif p < now:
                overdue += 1
    return {"n": len(errs), "bias_days": _r(statistics.fmean(errs), 2) if errs else None,
            "mae_days": _r(statistics.fmean(abs(e) for e in errs), 2) if errs else None, "overdue": overdue,
            "per_stage": {k: {"n": len(v), "bias_days": _r(statistics.fmean(v), 2)} for k, v in per.items()}}


# ------------------------------------------------------------------ live inputs (I/O)

def open_stages(conn: sqlite3.Connection) -> list[Stage]:
    """Every open stage (task), in execution order (store.list_tasks)."""
    import store
    return [Stage(int(t["id"]), str(t["title"] or ""), t.get("project"), str(t["status"]),
                  float(t["estimated_sessions"]) if t.get("estimated_sessions") is not None else None)
            for t in store.list_tasks(conn, status=OPEN) if t["kind"] in KINDS]


def done_stages(conn: sqlite3.Connection) -> list[tuple[int, datetime, datetime]]:
    """(id, first started, done) of every finished stage that has both times."""
    rows = conn.execute("SELECT t.id, t.done_at, MIN(e.ts) FROM tasks t JOIN task_events e ON e.task_id = t.id "
                        "AND e.event = 'started' WHERE t.status = 'done' AND t.kind = 'task' GROUP BY t.id")
    out = []
    for tid, d, s in rows:
        a, b = _ts(s), _ts(d)
        if a is not None and b is not None:
            out.append((int(tid), a, b))
    return out


def run_sessions(path: Optional[str] = None) -> list[tuple[datetime, datetime, float]]:
    """(start, end, session windows used) per AFClaude run (run_metrics rows)."""
    import run_metrics
    rows = cast(Callable[[Optional[str]], list[dict[str, Any]]], run_metrics.read_rows)(path or RUNS_FILE)
    out = []
    for r in rows:
        a, b, d = _ts(r.get("start")), _ts(r.get("end")), r.get("session_delta")
        if a is not None and b is not None and isinstance(d, (int, float)) and not isinstance(d, bool):
            out.append((a, b, float(d) / 100.0))
    return out


def gate_inputs(usage: Optional[Mapping[str, Any]], now: datetime) -> Gate:
    """The night gate's inputs now, as pacing.decide() reads them (ratio, forecast, threshold,
    settings). usage = a keepalive usage dict ({weekly: {percent, resets_at}, session: ...})."""
    import afclaude_config
    import pacing
    w = dict((usage or {}).get("weekly") or {})
    s = dict((usage or {}).get("session") or {})
    ratio, rsrc = cast(Callable[[], tuple[float, str]], pacing.ratio_info)()
    rates, fsrc = cast(Callable[..., tuple[Optional[list[float]], str]], pacing.profile)(None, now)
    fu = cast(Callable[[Optional[list[float]], datetime, datetime], float], pacing.forecast_user)
    fc: Optional[Forecast] = None if rates is None else (lambda a, b: float(fu(rates, a, b)))
    setting, tsrc = afclaude_config.reserve_threshold_setting()
    thr, thr_src = cast(Callable[[float, Any], tuple[float, str]], pacing.threshold_for)(ratio, setting)
    params, _ = cast(Callable[[], tuple[dict[str, Any], str]], pacing.load_params)()
    reset = _ts(w.get("resets_at")) if isinstance(w.get("resets_at"), str) else w.get("resets_at")
    sreset = _ts(s.get("resets_at")) if isinstance(s.get("resets_at"), str) else s.get("resets_at")
    wp, sp = w.get("percent"), s.get("percent")
    return Gate(float(wp) if isinstance(wp, (int, float)) else None, reset, ratio, thr, fc, schedule.load(),
                afclaude_config.last_mile_setting(), float(params.get("min_gap", 1.0)),
                float(sp) if isinstance(sp, (int, float)) else None, sreset,
                {"ratio": rsrc, "forecast": fsrc, "threshold": thr_src if setting == "auto" else tsrc})


def gather(conn: sqlite3.Connection, usage: Optional[Mapping[str, Any]], now: Optional[datetime] = None,
           runs_path: Optional[str] = None) -> dict[str, Any]:
    """predict() with the live inputs (read-only)."""
    now = now or datetime.now(UTC)
    try:
        runs = run_sessions(runs_path)
    except Exception:   # noqa: BLE001 - no run history: the fallback
        runs = []
    default, n = history_default([(a, b) for _, a, b in done_stages(conn)], runs)
    info = (f"median of {n} measured finished stages" if default is not None else
            f"{n} measured finished stage(s) < {MIN_HISTORY}: fallback {FALLBACK_SESSIONS:g} session/stage")
    return predict(open_stages(conn), gate_inputs(usage, now), now, default, info)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage ETAs (D-213)")
    ap.add_argument("--db", help="SQLite file (default: store.DB_PATH)")
    ap.add_argument("--score", action="store_true", help="score the logged predictions")
    args = ap.parse_args(argv)
    import store
    conn = store.connect(args.db or store.DB_PATH)
    try:
        if args.score:
            rows = []
            try:
                rows = [json.loads(ln) for ln in telemetry.lines(LOG_FILE) if ln.strip()]
            except OSError:
                pass
            done = {i: b for i, _, b in done_stages(conn)}
            for tid, d in conn.execute("SELECT id, done_at FROM tasks WHERE status='done'"):
                t = _ts(d)
                if t is not None:
                    done.setdefault(int(tid), t)
            print(json.dumps(score(rows, done), indent=1))
        else:
            import keepalive
            u = cast(Callable[[], Optional[dict[str, Any]]], keepalive.read_usage_cache)()
            print(json.dumps(gather(conn, u), indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
