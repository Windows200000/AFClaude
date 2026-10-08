#!/usr/bin/env python3
"""How long an AFClaude run takes to fill the session limit (D-207).

One row per AFClaude run in data/afclaude_runs.jsonl. A run is a real fire recorded by
keepalive.py (keepalive_state.json `handled` entries with a result in keepalive.RUN_RESULTS:
a session-window start, a postponed start, a last-stretch slot, or a manual start); fires a few
minutes apart are one run (FIRE_MERGE: the double last-stretch fire of 01.10.2026).

Per run: start (the fire), kind, session % and weekly % at the start, the session-% trajectory,
the time to 95% (the task-manager's stop, D-014) and to 100% / the limit notice, the run's end
(a limit hit, D-204; the session idle after its last turn, keepalive.RUN_IDLE; the session
window's end; or the next run), the session-%/hour rate, an extrapolated time to full when the
run ended before filling, the weekly delta and the AFClaude vs user token split while it ran.

Sources (read-only):
  - the usage readings of data/samples.jsonl (the sampler, every 15 min) plus
    data/run_usage.jsonl (the watcher's extra readings every RUN_SAMPLE_EVERY while a run is
    active, watch_sample()); stale readings (usage_stale.py) are counted and skipped
  - the run session's transcripts (main + subagents): entry times (idle end) and limit notices.

compute_run() is pure (fire + readings + activity -> row), so phase 2c can move it into the DB
(D-161). update() recomputes the rows that are not final yet and rewrites the small runs file
atomically (a final row is never recomputed, D-130); the first update() after deployment
backfills every run in the keepalive state. Called by usage_sampler.py after each sample.
CLI: `python3 run_metrics.py [--out PATH] [--print]` (default: update data/afclaude_runs.jsonl).
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import usage_stale

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
RUNS_FILE = os.path.join(DATA_DIR, "afclaude_runs.jsonl")
SAMPLES_FILE = os.path.join(DATA_DIR, "samples.jsonl")
RUN_USAGE_FILE = os.path.join(DATA_DIR, "run_usage.jsonl")     # watcher readings during runs, never pruned
STATE_FILES = [   # the keepalive state (container: data/keepalive; host: the repo dir), as pacing.FIRE_FILES
    os.path.join(DATA_DIR, "keepalive", "keepalive_state.json"),
    os.path.join(os.environ.get("KEEPALIVE_STATE_DIR", HERE), "keepalive_state.json"),
]

ROW_VERSION = 1
SESSION_LENGTH = timedelta(hours=5)     # afclaude_config.SESSION_LENGTH (kept import-free for the pure part)
RUN_RESULTS = ("continued-in-place", "no-reply-within-timeout")   # keepalive.RUN_RESULTS
RUN_IDLE = timedelta(minutes=15)        # keepalive.RUN_IDLE: no transcript entry this long = the run is over
FIRE_MERGE = timedelta(minutes=15)      # fires closer than this to a run's first fire belong to that run
START_MAX_AGE = timedelta(minutes=30)   # a reading this old before the fire still gives the start values
START_SLACK = timedelta(minutes=2)      # a reading up to this long after the fire still is "at the start"
END_GRACE = timedelta(minutes=20)       # the first reading after an idle end still counts (the meter lags)
FINAL_AFTER = timedelta(minutes=30)     # a row is final this long after its end (the trailing reading is in)
RESET_JUMP = timedelta(minutes=30)      # resets_at jitters by ms; a real reset moves it by hours
MIN_SPAN = timedelta(minutes=10)        # shorter runs (or < MIN_DELTA %) give no rate
MIN_DELTA = 1.0
END_PRIO = {"limit": 0, "idle": 1, "next-run": 2, "window-end": 3}   # ties: the first named wins
RUN_SAMPLE_EVERY = timedelta(minutes=5)  # = keepalive.RUN_READING_EVERY: the watcher's extra readings during a run
RUN_SAMPLE_REFRESH = timedelta(minutes=4)  # refresh /usage (local command) only if the cache is older


# ------------------------------------------------------------------ helpers

def ts(x):
    return usage_stale.ts(x)


def iso(t):
    return t.astimezone(UTC).isoformat() if t else None


def bstr(t):
    return t.astimezone(BERLIN).strftime("%a %d.%m. %H:%M") if t else None


def _mins(d):
    return round(d.total_seconds() / 60, 1) if d is not None else None


def _r(x, n=1):
    return round(x, n) if isinstance(x, (int, float)) else None


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def fire_kind(key, rec=None):
    """keepalive's handled key (+ record) -> window-start | postponed-start | last-stretch | manual."""
    reason = str((rec or {}).get("reason") or "")
    if key.startswith("last-mile-"):
        return "last-stretch"
    if key.startswith("manual-now-") or "manualtest" in key:
        return "manual"
    if key.startswith("window-start-"):
        return "postponed-start" if "(postponed)" in reason else "window-start"
    return "other"


# ------------------------------------------------------------------ fires -> runs

def fires_from_states(states, default_session=None):
    """keepalive_state.json dicts -> real fires, oldest first, one per handled key:
    {key, at, kind, session, usage (the cache at the fire, if recorded), result}."""
    seen = {}
    for st in states:
        for key, rec in ((st or {}).get("handled") or {}).items():
            if not isinstance(rec, dict) or rec.get("result") not in RUN_RESULTS or key in seen:
                continue
            at = ts(rec.get("at"))
            if at is None:
                continue
            seen[key] = {"key": key, "at": at, "kind": fire_kind(key, rec), "result": rec.get("result"),
                         "session": rec.get("session") or default_session, "usage": rec.get("usage")}
    return sorted(seen.values(), key=lambda f: f["at"])


def group_fires(fires):
    """Fires closer than FIRE_MERGE to a run's first fire are one run (a duplicate fire of the same
    slot). -> [{run_id, at, kind, session, usage, fires: [keys]}], oldest first."""
    runs = []
    for f in sorted(fires, key=lambda f: f["at"]):
        if runs and f["at"] - runs[-1]["at"] < FIRE_MERGE and f["session"] == runs[-1]["session"]:
            runs[-1]["fires"].append(f["key"])
            continue
        runs.append({"run_id": f["key"], "at": f["at"], "kind": f["kind"], "session": f["session"],
                     "usage": f.get("usage"), "result": f.get("result"), "fires": [f["key"]]})
    return runs


# ------------------------------------------------------------------ readings

def reading_from_sample(row):
    """A data/samples.jsonl row -> a reading {t, session, session_resets_at, weekly, weekly_resets_at,
    stale, own, other, since}, or None without meters. t = the cache's fetch time (when the numbers
    were true), else the sample time."""
    if not isinstance(row, dict):
        return None
    u = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    s, w = u.get("session") or {}, u.get("weekly") or {}
    at = ts(row.get("at"))
    if at is None or (_num(s.get("percent")) is None and _num(w.get("percent")) is None):
        return None
    return {"t": ts(u.get("fetched_at")) or at, "at": at, "src": "sampler",
            "session": _num(s.get("percent")), "session_resets_at": ts(s.get("resets_at")),
            "weekly": _num(w.get("percent")), "weekly_resets_at": ts(w.get("resets_at")),
            "stale": usage_stale.row_stale(row),
            "own": _num(row.get("own_w_tokens")), "other": _num(row.get("other_w_tokens")),
            "since": ts(row.get("since"))}


def reading_from_watch(row):
    """A data/run_usage.jsonl row (watch_sample) -> a reading, or None."""
    if not isinstance(row, dict):
        return None
    at = ts(row.get("at"))
    if at is None or (_num(row.get("session_pct")) is None and _num(row.get("weekly_pct")) is None):
        return None
    f = ts(row.get("fetched_at"))
    return {"t": f or at, "at": at, "src": "watcher",
            "session": _num(row.get("session_pct")), "session_resets_at": ts(row.get("session_resets_at")),
            "weekly": _num(row.get("weekly_pct")), "weekly_resets_at": ts(row.get("weekly_resets_at")),
            "stale": bool(row.get("stale")) or usage_stale.is_stale(f, at),
            "own": None, "other": None, "since": None}


def merge_readings(readings):
    """Sorted by reading time; the same cache reading seen by the sampler and the watcher (same fetch
    time) is kept once (the sampler's, which carries the token split)."""
    out = {}
    for r in sorted((r for r in readings if r), key=lambda r: (r["t"], r["src"] != "sampler")):
        k = r["t"].replace(microsecond=0)
        if k not in out:
            out[k] = r
    return [out[k] for k in sorted(out)]


def _same_window(r, window_end):
    """The reading belongs to the session window ending at window_end."""
    if window_end is None:
        return True
    ra = r.get("session_resets_at")
    if ra is None:      # no live window (0% before its first message, or after the reset)
        return r["t"] < window_end
    return abs(ra - window_end) <= RESET_JUMP


def _cross(points, level):
    """First time the session % reaches `level` along points [(t, pct)], linearly interpolated
    between the reading before and the first one at/above it. -> datetime or None."""
    prev = None
    for t, p in points:
        if p is None:
            continue
        if p >= level:
            if prev is None or prev[1] >= level or p == prev[1]:
                return t
            t0, p0 = prev
            return t0 + (t - t0) * ((level - p0) / (p - p0))
        prev = (t, p)
    return None


# ------------------------------------------------------------------ the pure part

def compute_run(run, readings, activity=None, limit_hits=(), now=None, next_fire=None):
    """One run's metrics row (pure).

    run:        {run_id, at, kind, session, fires, usage?} (group_fires); usage = the cache at the fire
                ({session: {percent, resets_at}, weekly: {...}, fetched_at}), optional
    readings:   merge_readings() output, any time range (stale ones are counted and skipped)
    activity:   sorted datetimes of the run session's transcript entries (main + subagents), or
                None if unknown (then no idle end can be detected)
    limit_hits: [(datetime, kind)] limit notices of the run session
    now:        the current time (an unfinished run ends "ongoing" at now)
    next_fire:  the next run's fire time (a run ends there at the latest)
    """
    now = now or datetime.now(UTC)
    fire = run["at"]
    cap = fire + SESSION_LENGTH
    if next_fire and next_fire > fire:
        cap = min(cap, next_fire)
    near = [r for r in readings if fire - START_MAX_AGE <= r["t"] <= cap + END_GRACE]
    stale = [r for r in near if r.get("stale")]
    good = [r for r in near if not r.get("stale")]

    # -- start values: the fire's own cache snapshot, else the latest reading at the fire
    start = None
    fu = run.get("usage") if isinstance(run.get("usage"), dict) else None
    if fu and not usage_stale.is_stale(fu.get("fetched_at"), fire, START_MAX_AGE):
        start = {"t": ts(fu.get("fetched_at")) or fire, "src": "fire",
                 "session": _num((fu.get("session") or {}).get("percent")),
                 "session_resets_at": ts((fu.get("session") or {}).get("resets_at")),
                 "weekly": _num((fu.get("weekly") or {}).get("percent")),
                 "weekly_resets_at": ts((fu.get("weekly") or {}).get("resets_at"))}
    if start is None:
        before = [r for r in good if r["t"] <= fire + START_SLACK]
        start = before[-1] if before else None
    after = [r for r in good if r["t"] > (start["t"] if start else fire - timedelta(seconds=1))]

    # -- the session window the run started in
    window_end, estimated = None, False
    if start and start.get("session_resets_at") and start["session_resets_at"] > fire:
        window_end = start["session_resets_at"]
    else:
        nxt = next((r for r in after if r.get("session_resets_at") and r["session_resets_at"] > fire
                    and r["t"] <= cap), None)
        if nxt:
            window_end = nxt["session_resets_at"]
    if window_end is not None:
        window_end = (window_end + timedelta(seconds=30)).replace(second=0, microsecond=0)
    else:
        window_end, estimated = fire + SESSION_LENGTH, True

    start_session = start.get("session") if start else None
    if start and start_session is not None and start.get("session_resets_at") and start["session_resets_at"] <= fire:
        start_session = 0.0     # the window the reading belonged to had already reset at the fire
    if start_session is None and not estimated and window_end - SESSION_LENGTH >= fire - timedelta(minutes=5):
        start_session = 0.0     # a fresh window opened with the run
    start_weekly = start.get("weekly") if start else None
    if start_weekly is None:      # no reading at the fire: the first one in the run
        first = next((r for r in after if r.get("weekly") is not None and r["t"] <= cap), None)
        start_weekly, wr0 = (first["weekly"], first.get("weekly_resets_at")) if first else (None, None)
    else:
        wr0 = start.get("weekly_resets_at")

    # -- the end
    cands = []
    hit = next(((t, k) for t, k in sorted(limit_hits or ()) if fire <= t < min(window_end, cap)), None)
    if hit:
        cands.append((hit[0], "limit", hit[1]))
    full = next((r for r in after if r["t"] < min(window_end, cap) and _same_window(r, window_end)
                 and ((r.get("session") or 0) >= 100 or (r.get("weekly") or 0) >= 100)), None)
    if full and not hit:
        cands.append((full["t"], "limit", "session" if (full.get("session") or 0) >= 100 else "weekly"))
    active_after = None
    if activity is not None:
        # a limit notice is the run's last activity (the limit, not the idleness, ends it)
        acts = sorted([t for t in activity if fire - timedelta(minutes=1) <= t] +
                      [t for t, _ in (limit_hits or ()) if fire <= t])
        last, idle_end = fire, None
        for t in acts:
            if t - last >= RUN_IDLE:
                idle_end = last
                break
            last = max(last, t)
        if idle_end is None and now - last >= RUN_IDLE:
            idle_end = last
        if idle_end is not None and idle_end < window_end:
            cands.append((idle_end, "idle", None))
        active_after = any(window_end < t <= window_end + RUN_IDLE for t in acts)
    if window_end <= now:
        cands.append((window_end, "window-end", None))
    if next_fire and fire < next_fire <= now:
        cands.append((next_fire, "next-run", None))
    cands = [c for c in cands if c[0] <= now and c[0] <= cap] or \
        ([(cap, "window-end", None)] if cap <= now else [])
    if cands:
        end, reason, limit_kind = min(cands, key=lambda c: (c[0], END_PRIO[c[1]]))
        ongoing = False
    else:
        end, reason, limit_kind, ongoing = now, "ongoing", None, True

    # -- the trajectory inside the window (plus one trailing reading after an idle end)
    traj = [r for r in after if r["t"] <= end and _same_window(r, window_end)]
    if reason in ("idle", "next-run"):
        trail = next((r for r in after if end < r["t"] <= end + END_GRACE and r["t"] < window_end
                      and _same_window(r, window_end)), None)
        if trail:
            traj.append(trail)
    elif reason == "limit":
        trail = next((r for r in after if end < r["t"] <= end + END_GRACE and _same_window(r, window_end)), None)
        if trail:
            traj.append(trail)

    pts = ([(fire, start_session)] if start_session is not None else []) + \
        [(r["t"], r.get("session")) for r in traj if r.get("session") is not None]
    t95 = _cross(pts, 95.0)
    t100, t100_src = None, None
    if reason == "limit" and limit_kind == "session" and hit:
        t100, t100_src = hit[0], "limit-notice"
    else:
        t100 = _cross(pts, 100.0)
        t100_src = "readings" if t100 else None
    if t95 and t100 and t95 > t100:
        t95 = t100

    # -- final values and the rate
    last = traj[-1] if traj else None
    end_session = max([p for _, p in pts if p is not None], default=None)
    if t100 is not None:
        end_session = 100.0
    base_t, base_p = (fire, start_session) if start_session is not None else \
        ((pts[0][0], pts[0][1]) if pts else (None, None))
    if t100 is not None:
        span_end = t100
    elif last is not None:      # a trailing reading after the end holds the use up to the end
        span_end = min(last["t"], end)
    else:
        span_end = None
    rate = None
    if base_t is not None and span_end is not None and end_session is not None and base_p is not None:
        span = span_end - base_t
        delta = end_session - base_p
        if span >= MIN_SPAN and delta >= MIN_DELTA:
            rate = delta / (span.total_seconds() / 3600)

    fill = _mins(t100 - fire) if t100 else None
    fill_est = t95_est = None
    if t100 is None and rate and start_session is not None:
        fill_est = _r((100.0 - start_session) / rate * 60)
        if t95 is None:
            t95_est = _r(max(0.0, 95.0 - start_session) / rate * 60)

    # -- weekly
    end_weekly = None
    weekly_reset = False
    for r in traj:
        if r.get("weekly") is None:
            continue
        if wr0 and r.get("weekly_resets_at") and abs(r["weekly_resets_at"] - wr0) > RESET_JUMP:
            weekly_reset = True
            break
        end_weekly = r["weekly"] if end_weekly is None else max(end_weekly, r["weekly"])
    if reason == "limit" and limit_kind == "weekly" and not weekly_reset:
        end_weekly = 100.0
    weekly_delta = end_weekly - start_weekly if (end_weekly is not None and start_weekly is not None
                                                 and not weekly_reset) else None

    # -- AFClaude vs user tokens while the run went (the sampler intervals overlapping it; the
    #    first and last one also hold a little before / after the run)
    own = other = None
    tok = [r for r in near if r["src"] == "sampler" and r["at"] > fire
           and (r.get("since") or r["at"] - timedelta(minutes=15)) < end
           and (r.get("own") is not None or r.get("other") is not None)]
    seen = set()
    for r in tok:
        if r["at"] in seen:
            continue
        seen.add(r["at"])
        own = (own or 0) + (r.get("own") or 0)
        other = (other or 0) + (r.get("other") or 0)
    user_share = other / (own + other) if own is not None and (own + other) > 0 else None

    return {
        "v": ROW_VERSION, "run_id": run["run_id"], "session": run.get("session"), "kind": run.get("kind"),
        "fires": list(run.get("fires") or [run["run_id"]]),
        "start": iso(fire), "start_berlin": bstr(fire),
        "end": iso(end), "end_berlin": bstr(end), "end_reason": reason, "limit_kind": limit_kind,
        "ongoing": ongoing, "final": (not ongoing) and now - end >= FINAL_AFTER,
        "duration_min": _mins(end - fire),
        "window_end": iso(window_end), "window_end_estimated": estimated,
        "active_after_window_end": active_after if reason == "window-end" else None,
        "session_start_pct": _r(start_session), "session_end_pct": _r(end_session),
        "session_delta": _r(end_session - start_session) if end_session is not None and start_session is not None else None,
        "weekly_start_pct": _r(start_weekly), "weekly_end_pct": _r(end_weekly), "weekly_delta": _r(weekly_delta),
        "weekly_reset_in_run": weekly_reset,
        "t95_min": _mins(t95 - fire) if t95 else None, "t100_min": fill, "t100_source": t100_src,
        "rate_pct_per_h": _r(rate), "fill_min": fill, "fill_min_est": fill_est, "t95_min_est": t95_est,
        "empty_to_full_min": _r(100.0 / rate * 60) if rate else None,
        "trajectory": [[_mins(r["t"] - fire), r.get("session"), r.get("weekly")] for r in traj],
        "readings": len(traj), "stale_skipped": len([r for r in stale if fire <= r["t"] <= end + END_GRACE]),
        "own_w_tokens": _r(own, 0), "other_w_tokens": _r(other, 0), "user_share": _r(user_share, 3),
        "computed_at": iso(now),
    }


def summary(rows):
    """The quickview's fill-time line: the last run (fill time, or rate + extrapolated time to
    full) and the median empty-to-full time over the recorded runs with a rate (D-140: plus the
    spread). Pure."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        return {"n": 0}
    last = max(rows, key=lambda r: r.get("start") or "")
    rated = [r["empty_to_full_min"] for r in rows if r.get("empty_to_full_min")]
    fills = [r["fill_min"] for r in rows if r.get("fill_min") and (r.get("session_start_pct") or 0) <= 5]
    keys = ("run_id", "kind", "start_berlin", "end_berlin", "end_reason", "limit_kind", "ongoing",
            "duration_min", "session_start_pct", "session_end_pct", "weekly_delta", "t95_min",
            "fill_min", "fill_min_est", "rate_pct_per_h", "empty_to_full_min", "user_share")
    return {"n": len(rows), "n_rated": len(rated),
            "median_empty_to_full_min": _r(statistics.median(rated)) if rated else None,
            "p25_p75_empty_to_full_min": [_r(x) for x in statistics.quantiles(rated, n=4)[::2]]
            if len(rated) >= 2 else None,
            "median_fill_min": _r(statistics.median(fills)) if fills else None, "n_fills": len(fills),
            "last": {k: last.get(k) for k in keys}}


# ------------------------------------------------------------------ I/O

_AT_RE = re.compile(r'^\{"at": "([^"]+)"')


def load_readings(lo, hi, samples_path=None, watch_path=None):
    """Readings with a sample time in [lo, hi] from the sampler rows and the watcher rows. Lines
    outside the range are skipped by their leading "at" without a full JSON parse."""
    out = []
    for path, fn in ((samples_path or SAMPLES_FILE, reading_from_sample),
                     (watch_path or RUN_USAGE_FILE, reading_from_watch)):
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = _AT_RE.match(line)
                if m:
                    t = ts(m.group(1))
                    if t is not None and not lo <= t <= hi:
                        continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                t = ts(row.get("at")) if isinstance(row, dict) else None
                if t is not None and lo <= t <= hi:
                    out.append(fn(row))
    return merge_readings(out)


def session_activity(session_id, lo, hi, projects_dir=None):
    """The session's transcript entry times in [lo, hi] (user/assistant entries of the main
    transcripts in every project dir plus its subagents) and its limit notices
    [(time, kind)] (keepalive.stall_info). -> (activity, hits); (None, []) without a transcript."""
    import keepalive as ka
    pdir = projects_dir or ka.PROJECTS_DIR
    mains = glob.glob(os.path.join(pdir, "*", f"{session_id}.jsonl"))
    subs = glob.glob(os.path.join(pdir, "*", session_id, "subagents", "*.jsonl"))
    if not mains:
        return None, []
    acts, hits = [], []
    for path in mains + subs:
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), UTC) < lo:
                continue
            entries = ka.iter_entries(path)
            for e in entries:
                if not isinstance(e, dict) or e.get("type") not in ("user", "assistant"):
                    continue
                t = ts(e.get("timestamp"))
                if t is None or not lo <= t <= hi:
                    continue
                stall = ka.stall_info(e) if path in mains and not e.get("isSidechain") else None
                if stall:
                    hits.append((t, stall.get("kind") or "unknown"))
                else:
                    acts.append(t)
        except OSError:
            continue
    return sorted(acts), sorted(hits)


def load_states(paths=None):
    out = []
    for p in paths or STATE_FILES:
        try:
            with open(p) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def read_rows(path=None):
    rows = []
    try:
        with open(path or RUNS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def write_rows(rows, path=None):
    path = path or RUNS_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def update(now=None, out_path=None, state_paths=None, samples_path=None, watch_path=None,
           projects_dir=None, session=None, activity_fn=None):
    """Recompute every run whose row is missing or not final yet, keep the final rows untouched,
    rewrite the runs file (only if something changed). -> all rows, oldest first."""
    now = now or datetime.now(UTC)
    out_path = out_path or RUNS_FILE
    if session is None:
        try:
            import afclaude_config
            session = afclaude_config.manager_session()
        except Exception:   # noqa: BLE001 - the fire records carry the session from now on
            session = None
    runs = group_fires(fires_from_states(load_states(state_paths), session))
    rows = {r.get("run_id"): r for r in read_rows(out_path) if isinstance(r, dict)}
    todo = [i for i, r in enumerate(runs) if not (rows.get(r["run_id"]) or {}).get("final")]
    if not todo:
        return [rows[r["run_id"]] for r in runs if r["run_id"] in rows]
    lo = min(runs[i]["at"] for i in todo) - START_MAX_AGE
    hi = min(max(runs[i]["at"] for i in todo) + SESSION_LENGTH + END_GRACE + timedelta(hours=1), now)
    readings = load_readings(lo, hi, samples_path, watch_path)
    activity_fn = activity_fn or (lambda sid, a, b: session_activity(sid, a, b, projects_dir))
    hits_state = []
    for st in load_states(state_paths):
        for v in (st.get("limit_hits") or {}).values():
            t = ts(v)
            if t:
                hits_state.append((t, "unknown"))
    for i in todo:
        run = runs[i]
        nxt = runs[i + 1]["at"] if i + 1 < len(runs) else None
        acts, hits = (None, [])
        if run.get("session"):
            acts, hits = activity_fn(run["session"], run["at"] - timedelta(minutes=1),
                                     run["at"] + SESSION_LENGTH + RUN_IDLE)
        if not hits:   # the watcher's detection times (its own session only)
            hits = [h for h in hits_state if run["at"] <= h[0] <= run["at"] + SESSION_LENGTH]
        rows[run["run_id"]] = compute_run(run, readings, acts, hits, now=now, next_fire=nxt)
    ordered = [rows[r["run_id"]] for r in runs if r["run_id"] in rows]
    ordered += [r for k, r in rows.items() if k not in {x["run_id"] for x in runs}]   # never drop a row
    ordered.sort(key=lambda r: r.get("start") or "")
    write_rows(ordered, out_path)
    return ordered


def watch_sample(session_id, now, st=None, path=None):
    """The watcher's extra usage reading while a run is active (every RUN_SAMPLE_EVERY): reuse the
    ~/.claude.json cache if it is younger than RUN_SAMPLE_REFRESH (the sampler or a start just
    refreshed it), else one `claude -p /usage` (a local command, no model call; the shared retry
    of keepalive.refresh_usage_checked). Appends one row to data/run_usage.jsonl. -> the row or None."""
    import keepalive as ka
    fire = ka.run_active(session_id, now, st)
    if fire is None:
        return None
    u = ka.read_usage_cache()
    if not u or now - u["fetched_at"] > RUN_SAMPLE_REFRESH:
        u = ka.refresh_usage_checked(now=now).get("usage") or u
    if not u:
        return None
    s, w = u.get("session") or {}, u.get("weekly") or {}
    row = {"at": iso(now), "run_start": iso(fire), "session": session_id, "fetched_at": iso(u.get("fetched_at")),
           "session_pct": s.get("percent"), "session_resets_at": iso(s.get("resets_at")),
           "weekly_pct": w.get("percent"), "weekly_resets_at": iso(w.get("resets_at")),
           "stale": usage_stale.is_stale(u.get("fetched_at"), now)}
    path = path or RUN_USAGE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def fire_usage(u):
    """keepalive.handle_fire's snapshot of the usage cache at the fire (JSON-safe), or None."""
    if not u:
        return None
    out = {"fetched_at": iso(u.get("fetched_at"))}
    for k in ("session", "weekly"):
        m = u.get(k) or {}
        out[k] = {"percent": m.get("percent"), "resets_at": iso(m.get("resets_at"))}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="runs file (default data/afclaude_runs.jsonl)")
    ap.add_argument("--print", action="store_true", help="print the rows and the summary")
    a = ap.parse_args()
    rows = update(out_path=a.out)
    if a.print:
        for r in rows:
            print(json.dumps({k: v for k, v in r.items() if k != "trajectory"}, ensure_ascii=False))
        print(json.dumps(summary(rows), ensure_ascii=False))
    else:
        print(f"{len(rows)} run(s) in {a.out or RUNS_FILE}")


if __name__ == "__main__":
    sys.exit(main())
