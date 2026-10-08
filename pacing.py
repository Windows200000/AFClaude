#!/usr/bin/env python3
"""Weekly budget model "pacing" (stdlib only): a forecast-driven night gate, the original linear
rule as its fallback. The default of keepalive.budget_eval() (the usage_model setting: "pacing").

Goal: use the weekly limit with autonomous night work in FULL session windows, but only while the
week is predicted to end at or below the reserve threshold; then fill the last stretch right
before the reset to 100% (owner decision 04.10.2026).

Night gate (every decision; each session window of the night is re-checked, a run uses its session
window to the end):
    w             = the weekly % now
    run_cost      = the weekly % of the session window AFClaude would use: ratio x (100 - session %)
                    of the live session window, else ratio x 100 = full_session_cost (a fresh one)
    predicted_end = w + run_cost + forecast(now, weekly reset)
    RUN iff predicted_end <= threshold: budget for this run (headroom) = run_cost (one number:
    reason, --decide, the continue message, the quickview; ~ 1 session window). Otherwise no run
    (no partial runs); the next session window / night re-checks.
    threshold     = the reserve_threshold setting: "auto" (default) = one session window
                    left = 100 - full_session_cost from the measured ratio (dynamic_threshold), or a %
                    override in 50..99.
    The model has NO margin: the threshold is the only spare.
    No forecast (< MIN_FORECAST_HOURS of closed-week user data): the straight line: RUN iff
    w + run_cost <= threshold x the elapsed fraction of the week at tonight's window end.

Forecast (the predictor): the user's typical weekly-% rise per hour of the week (Berlin weekday x
hour), from the closed weekly cycles in data/samples.jsonl (last 4; user-only: intervals inside
an autonomous AFClaude run, from a recorded fire until the next prompt in an AFClaude session,
are left out; usage the user drives in AFClaude sessions counts as the user's). Hours without
data use the mean rate. forecast(a, b) = the sum over [a, b). Needs >= MIN_FORECAST_HOURS (72)
of covered hours, else the straight line above is used.

Accuracy, computed in code: forecast_backtest() back-calculates the predictor's error from the
usage data (at every past night session-window start, the profile fitted on the cycles closed
before it vs the user's real use until the reset); ratio_stats() gives the session/weekly ratio's
dispersion per session window (limit_ratio.py); threshold_info() puts both, the full-session cost
+- its uncertainty, the dynamic default and the active threshold together for the UI. Window-start
decisions are also logged (data/forecast_log.jsonl) and forecast_errors() scores them.

Last stretch: the final min(ceil(sessions_left), 2) x 5 h before the reset, sessions_left =
(100 - w) / (100 x ratio): budget 100 - w, session cap 100% (last_mile_hours "auto"; a number
overrides it, 0 = off).

Yield: only activity in NON-AFClaude sessions and a weekly-% rise without local turns (another
device) count as the user; the user's input to AFClaude sessions does not. A HOLD for the user
POSTPONES: recheck at last activity + idle_min (60). Unknown sampler data = active, recheck in
15 min. Also in the last stretch unless last_mile_yield is false. Fail-safes: unknown usage or
a stale reset time -> HOLD (recheck 15 min); exhausted -> HOLD.

ratio = weekly % per session % = limit_ratio.preferred_ratio() (the per-session-window estimate,
else the 15-min median), else 0.2 (conservative), and the reason says which.
"""
import json
import math
import os
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import afclaude_config
import usage_stale

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
SAMPLES_FILE = os.path.join(DATA_DIR, "samples.jsonl")
FORECAST_LOG = os.path.join(DATA_DIR, "forecast_log.jsonl")
FIRE_FILES = [  # (path, group, time field): AFClaude's own fires (start of an autonomous run)
    (os.path.join(DATA_DIR, "keepalive", "keepalive_state.json"), "handled", "at"),
    (os.path.join(os.environ.get("KEEPALIVE_STATE_DIR", HERE), "keepalive_state.json"), "handled", "at"),
    (os.path.join(DATA_DIR, "dispatcher_state.json"), "sessions", "sent_at"),
    (os.path.join(DATA_DIR, "usage_review_state.json"), "runs", "at"),
]

WEEK = timedelta(days=7)
SESSION_H = afclaude_config.SESSION_LENGTH.total_seconds() / 3600
LAST_MILE_MAX_SESSIONS = 2
# the code defaults of the pacing_* settings (actions.SETTINGS; test_pacing checks they agree)
DEFAULTS = {"idle_min": 60.0, "min_gap": 1.0, "session_cap": 85.0, "last_mile_yield": True}
THRESHOLD_RANGE = (50.0, 99.0)
LAST_MILE_SESSION_CAP = 100.0
DEFAULT_RATIO = 0.2
RATIO_RANGE = (0.01, 1.0)
MIN_FORECAST_HOURS = 72.0        # covered hours of user data before the forecast is trusted
FIT_CYCLES = 4
RETRY = timedelta(minutes=15)
SAMPLE_MAX_AGE = timedelta(minutes=30)
ANCHOR_MAX_AGE = timedelta(minutes=30)
FIRE_SLACK = timedelta(minutes=2)
AUTO_MAX = timedelta(hours=10)
TAIL_BYTES = 512 * 1024              # activity: about the last 40 h
LONG_BYTES = 10 * 1024 * 1024        # ratio, forecast: about the last 4-5 weeks


# ------------------------------------------------------------------ params

def load_params():
    """-> (params, source text): the pacing settings pacing_idle_min, pacing_min_gap,
    pacing_session_cap, pacing_last_mile_yield (DB settings, else the code defaults; they were
    optional overrides in data/user_model.json until phase 2a imported them). Never raises."""
    try:
        return {**DEFAULTS, **afclaude_config.pacing_params()}, "settings"
    except Exception as e:   # noqa: BLE001 - the decision must not fail on its parameters
        return dict(DEFAULTS), f"defaults (settings unreadable: {type(e).__name__})"


# ------------------------------------------------------------------ samples

def _ts(s):
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(s).replace(" ", "T").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _dict(x):
    return x if isinstance(x, dict) else {}


def _round_reset(t):
    """resets_at jitters by up to a second between fetches: round to the minute (+30 s, floor)."""
    t = _ts(t)
    return None if t is None else (t.astimezone(UTC) + timedelta(seconds=30)).replace(second=0, microsecond=0)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


_TAIL_CACHE = {}


def _file_sig(path, st):
    """Cache key for a file: mtime + size + its last bytes. mtime alone is too coarse (a
    same-size rewrite within a few ms keeps mtime_ns), which made tests read stale rows."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(st.st_size - 256, 0))
            tail = fh.read(256)
    except OSError:
        tail = b""
    return (st.st_mtime_ns, st.st_size, tail)


def tail_rows(path=None, max_bytes=TAIL_BYTES):
    """Rows of the end of samples.jsonl, oldest first (cached while the file is unchanged)."""
    path = path or SAMPLES_FILE
    st = os.stat(path)
    key = _file_sig(path, st)
    hit = _TAIL_CACHE.get((path, max_bytes))
    if hit and hit[0] == key:
        return hit[1]
    with open(path, "rb") as fh:
        fh.seek(max(st.st_size - max_bytes, 0))
        chunk = fh.read()
    lines = chunk.split(b"\n")
    if st.st_size > max_bytes:
        lines = lines[1:]
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and _ts(r.get("at")):
            rows.append(r)
    rows.sort(key=lambda r: _ts(r["at"]))
    _TAIL_CACHE[(path, max_bytes)] = (key, rows)
    return rows


def _weekly(r):
    """The row's weekly meter; {} for a stale row (usage_stale: the /usage cache was not
    refreshed, the frozen % would fake a flat or jumping week)."""
    return _dict(usage_stale.row_usage(_dict(r)).get("weekly"))


def _count(d, key):
    """Non-negative count from a sampler field; anything malformed counts as activity (1)."""
    v = d.get(key) if isinstance(d, dict) else None
    if v is None:
        return 0
    if not _num(v):
        return 1
    return max(int(v), 0)


TURNS = ("assistant_turns", "subagent_turns")


def user_signal(row, prev):
    """True if this interval shows the user OUTSIDE AFClaude: activity in a non-AFClaude session
    (activity.other), or a weekly-% rise without local turns (another device / claude.ai).
    Prompts in AFClaude's own sessions (activity.own) do not count."""
    a = _dict(row.get("activity"))
    own, oth = _dict(a.get("own")), _dict(a.get("other"))
    if _count(oth, "human_prompts") or any(_count(oth, k) for k in TURNS):
        return True
    w, pw = _weekly(row).get("percent"), (_weekly(prev).get("percent") if prev else None)
    return _num(w) and _num(pw) and w > pw and not any(_count(own, k) for k in TURNS)


def minutes_since_user(now, rows=None):
    """-> minutes since the last user activity, or None if unknown (no / stale / unreadable
    samples: the caller treats the user as active)."""
    try:
        rows = tail_rows() if rows is None else rows
        rows = [r for r in rows if isinstance(r, dict) and _ts(r.get("at"))]
        rows = [r for r in rows if _ts(r["at"]) <= now + timedelta(minutes=1)]
        if not rows or now - _ts(rows[-1]["at"]) > SAMPLE_MAX_AGE:
            return None
        for i in range(len(rows) - 1, -1, -1):
            try:
                hit = user_signal(rows[i], rows[i - 1] if i else None)
            except Exception:   # noqa: BLE001 - a malformed row counts as user activity
                hit = True
            if hit:
                return max((now - _ts(rows[i]["at"])).total_seconds() / 60, 0.0)
        return (now - _ts(rows[0]["at"])).total_seconds() / 60
    except Exception:   # noqa: BLE001
        return None


# ------------------------------------------------------------------ ratio, last stretch

def _ratio_snapshot(rows):
    """The latest limit_ratio snapshot (the sampler stores one in every row), else computed over
    the rows, else None."""
    if rows is None:
        try:
            rows = tail_rows(max_bytes=LONG_BYTES)
        except Exception:   # noqa: BLE001
            rows = []
    for r in reversed(rows):
        if isinstance(_dict(r).get("limit_ratio"), dict):
            return r["limit_ratio"]
    if rows:
        try:
            import limit_ratio
            return limit_ratio.compute(rows)
        except Exception:   # noqa: BLE001
            return None
    return None


def _ratio_pick(full):
    """-> (ratio, source text, kind "windows" | "15min_median" | "default")."""
    try:
        import limit_ratio
        pr = limit_ratio.preferred_ratio(full)
        v = pr.get("value")
        if pr.get("source") == "windows" and _num(v) and RATIO_RANGE[0] <= v <= RATIO_RANGE[1]:
            return float(v), f"per-session-window ratio, n={pr.get('n')} windows", "windows"
    except Exception:   # noqa: BLE001
        pass
    snap = _dict(_dict(full).get("ratio"))
    med = snap.get("median")
    if snap.get("status") == "ok" and _num(med) and RATIO_RANGE[0] <= med <= RATIO_RANGE[1]:
        return float(med), f"limit_ratio median, n={snap.get('n')}", "15min_median"
    why = (f"insufficient data, n={snap.get('n')}" if snap.get("status") == "insufficient_data"
           else "no limit_ratio data")
    return DEFAULT_RATIO, f"conservative default {DEFAULT_RATIO:g} (limit_ratio: {why})", "default"


def ratio_info(rows=None):
    """-> (weekly % per session %, source text): limit_ratio.preferred_ratio() (the
    per-session-window estimate, unbiased by the integer weekly %), else the 15-min median,
    else DEFAULT_RATIO (conservative: a shorter last stretch, a lower dynamic threshold)."""
    return _ratio_pick(_ratio_snapshot(rows))[:2]


def ratio_stats(rows=None):
    """The ratio the model uses and its dispersion across session windows (limit_ratio.py's
    per-window measurement, data/session_windows.jsonl): value, source, n, mean, stdev,
    weighted_stdev (the spread), intrinsic_stdev (the spread minus integer-rounding noise),
    se (standard error of the estimate), rounding_sd, p10 / p90. Fields are None when unknown."""
    full = _ratio_snapshot(rows)
    v, src, kind = _ratio_pick(full)
    rw = _dict(_dict(full).get("ratio_windows"))
    windows = kind == "windows"
    out = {"value": v, "source": src, "kind": kind,
           "n": rw.get("n") if windows else _dict(_dict(full).get("ratio")).get("n")}
    for k in ("mean", "median", "stdev", "weighted_stdev", "intrinsic_stdev", "se", "rounding_sd", "p10", "p90"):
        x = rw.get(k) if windows else None
        out[k] = float(x) if _num(x) else None
    return out


def full_session_cost(ratio):
    """Weekly % of one full session window (100 session %)."""
    return 100.0 * float(ratio)


def dynamic_threshold(ratio):
    """The default reserve threshold: one session window left = 100 - full_session_cost."""
    return min(max(100.0 - full_session_cost(ratio), THRESHOLD_RANGE[0]), THRESHOLD_RANGE[1])


def threshold_for(ratio, setting="auto"):
    """-> (threshold %, source text). setting "auto"/None = dynamic_threshold(ratio), else a %."""
    if setting in (None, "auto"):
        return dynamic_threshold(ratio), (f"dynamic: one session window left = 100 - "
                                          f"{full_session_cost(ratio):.1f}%")
    return float(setting), "override"


def sessions_left(weekly_pct, ratio):
    """Full session windows of weekly quota left: (100 - w) / (100 x ratio)."""
    return max(100.0 - float(weekly_pct), 0.0) / (100.0 * ratio)


def last_mile_hours(weekly_pct, ratio, setting="auto"):
    """"auto": min(ceil(sessions_left), 2) x session length; a number overrides it (0 = off)."""
    if setting != "auto":
        return max(float(setting), 0.0)
    return min(math.ceil(sessions_left(weekly_pct, ratio) - 1e-9), LAST_MILE_MAX_SESSIONS) * SESSION_H


# ------------------------------------------------------------------ windows

def _wall(d, t):
    return datetime.combine(d, t, tzinfo=BERLIN)


def in_window(t, win):
    start, end = win
    lt = t.astimezone(BERLIN).time()
    return start <= lt < end if start <= end else (lt >= start or lt < end)


def latest_window_start(t, win):
    d = t.astimezone(BERLIN).date()
    s = _wall(d, win[0])
    return s if s <= t else _wall(d - timedelta(days=1), win[0])


def window_end(start, win):
    """End of the window that starts at `start` (wall clock; DST nights are 9 or 11 h)."""
    d = start.astimezone(BERLIN).date() + (timedelta(days=1) if win[1] <= win[0] else timedelta(0))
    return _wall(d, win[1])


def next_window_start(t, win):
    s = latest_window_start(t, win)
    return s if s > t else _wall(s.astimezone(BERLIN).date() + timedelta(days=1), win[0])


# Session-window starts (D-202): the night gate is checked at the start of EACH 5-h session window
# inside the night window (23:00 and 04:00 by default), never at arbitrary times in between.

def window_minutes(win):
    """Wall-clock length of the window in minutes (it may span midnight)."""
    a, b = (t.hour * 60 + t.minute for t in win)
    return (b - a) % (24 * 60) or 24 * 60


def session_starts(ws, win):
    """The session-window starts of the night window that starts at `ws`: ws + k x 5 h on the wall
    clock for every k whose 5-h session window still ends by the window end (D-148: the window is
    N x 5 h, so k < N; at least the window start itself). Wall clock, so on a DST night the last
    one still ends at the window end (09:00): 23:00 and 04:00 for the default 23:00 x 10 h."""
    step = round(SESSION_H * 60)
    n = max(int((window_minutes(win) + 1e-6) // step), 1)
    d0, m0 = ws.astimezone(BERLIN).date(), win[0].hour * 60 + win[0].minute
    out = []
    for k in range(n):
        m = m0 + k * step
        out.append(_wall(d0 + timedelta(days=m // (24 * 60)),
                         dtime(m % (24 * 60) // 60, m % 60)))
    return out


def latest_session_start(t, win):
    """The latest session-window start <= t of the night window `t` is in; None outside the window."""
    if not in_window(t, win):
        return None
    starts = [s for s in session_starts(latest_window_start(t, win), win) if s <= t]
    return starts[-1] if starts else None


def next_session_start(t, win):
    """The first session-window start strictly after t (this night's next one, or the next night's first)."""
    ws = latest_window_start(t, win)
    for s in session_starts(ws, win) + session_starts(next_window_start(ws + timedelta(minutes=1), win), win):
        if s > t:
            return s
    return next_window_start(t, win)


def postpone_deadline(s, win):
    """A start postponed from the session-window start `s` (yield, D-018) must begin before this:
    the next session-window start of the same night (which checks the gate itself), so its 5-h
    session window still ends by the window end. For the last start it is `s` itself: a
    postponement there skips to the next night's first start (D-202)."""
    starts = session_starts(latest_window_start(s, win), win)
    later = [x for x in starts if x > s]
    return later[0] if later else s


def _b(t):
    return t.astimezone(BERLIN).strftime("%a %d.%m. %H:%M")


# ------------------------------------------------------------------ the predictor

def fire_times(files=None):
    out = []
    for path, group, field in (FIRE_FILES if files is None else files):
        try:
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        g = st.get(group) if isinstance(st, dict) else None
        for h in (g.values() if isinstance(g, dict) else g if isinstance(g, list) else []):
            if isinstance(h, dict) and h.get(field) and h.get("result") not in ("dry-run", "preflight-failed"):
                t = _ts(h[field])
                if t:
                    out.append(t)
    return sorted(set(out))


def autonomous_spans(rows, fires):
    """[(start, end)]: from each fire to the next AFClaude-session prompt that is not the fire's
    own typed-in message (the user came back), at most AUTO_MAX."""
    spans = []
    for f in fires:
        end = f + AUTO_MAX
        for r in rows:
            t = _ts(r["at"])
            if t <= f + FIRE_SLACK:
                continue
            if t >= end:
                break
            if _count(_dict(_dict(r.get("activity")).get("own")), "human_prompts"):
                end = t
                break
        spans.append((f, end))
    return spans


def _how(t):
    lt = t.astimezone(BERLIN)
    return lt.weekday() * 24 + lt.hour


def user_intervals(rows, fires):
    """-> [(t0, t1, user weekly-% rise)] of consecutive samples in one weekly cycle (<= 1 h apart),
    0 inside an autonomous AFClaude run."""
    rows = [r for r in rows if _num(_weekly(r).get("percent")) and _ts(r.get("at"))]
    spans = autonomous_spans(rows, fires)
    out = []
    for p, c in zip(rows, rows[1:]):
        t0, t1 = _ts(p["at"]), _ts(c["at"])
        if _round_reset(_weekly(p).get("resets_at")) != _round_reset(_weekly(c).get("resets_at")) \
                or not timedelta(0) < t1 - t0 <= timedelta(hours=1):
            continue
        auto = any(a < t1 and t0 < b for a, b in spans)
        dw = max(float(_weekly(c)["percent"]) - float(_weekly(p)["percent"]), 0.0)
        out.append((t0, t1, 0.0 if auto else dw))
    return out


def fit_profile(rows, fires, now):
    """The user's typical weekly-% rise per hour of the week (168 Berlin weekday x hour slots) from
    the last FIT_CYCLES closed cycles. -> (rates [168] | None, info text)."""
    cyc_of = {}
    for r in rows:
        c = _round_reset(_weekly(r).get("resets_at"))
        if c is not None and c <= now:
            cyc_of.setdefault(c, []).append(r)
    keep = sorted(cyc_of)[-FIT_CYCLES:]
    use = [r for c in keep for r in cyc_of[c]]
    use.sort(key=lambda r: _ts(r["at"]))
    hours, rise = [0.0] * 168, [0.0] * 168
    for t0, t1, dw in user_intervals(use, fires):
        k = _how(t1 - timedelta(seconds=1))
        hours[k] += (t1 - t0).total_seconds() / 3600
        rise[k] += dw
    covered = sum(hours)
    if covered < MIN_FORECAST_HOURS:
        return None, (f"too little user data for a forecast ({covered:.0f} h of closed weeks < "
                      f"{MIN_FORECAST_HOURS:g} h): the straight line")
    mean = sum(rise) / covered
    rates = [rise[k] / hours[k] if hours[k] >= 0.5 else mean for k in range(168)]
    return rates, f"forecast from {len(keep)} closed week(s), {covered:.0f} h, {sum(rise):.0f}% user usage"


def forecast_user(rates, a, b):
    """Forecast of the user's weekly-% use in [a, b) (no margin: the threshold is the spare)."""
    if rates is None or b <= a:
        return 0.0
    tot, t = 0.0, a
    while t < b:
        nxt = min((t.astimezone(UTC) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), b)
        tot += rates[_how(t)] * (nxt - t).total_seconds() / 3600
        t = nxt
    return tot


_PROFILE_CACHE = {}


def profile(rows=None, now=None, fires=None):
    """fit_profile() over the long samples tail, cached while the samples are unchanged."""
    now = now or datetime.now(UTC)
    if rows is not None:
        return fit_profile(rows, fire_times() if fires is None else fires, now)
    try:
        st = os.stat(SAMPLES_FILE)
    except OSError:
        return None, "no samples: the straight line"
    key = (SAMPLES_FILE, _file_sig(SAMPLES_FILE, st), now.date())
    if _PROFILE_CACHE.get("key") != key:
        _PROFILE_CACHE.update(key=key, val=fit_profile(tail_rows(max_bytes=LONG_BYTES),
                                                       fire_times() if fires is None else fires, now))
    return _PROFILE_CACHE["val"]


# ------------------------------------------------------------------ decision

def decide_core(weekly_pct, resets_at, now, msu, P=None, session_pct=None, session_resets_at=None,
                activity_known=True, ratio=DEFAULT_RATIO, lm_setting="auto", win=None, threshold="auto",
                forecast=None):
    """Pure decision. forecast = a function (a, b) -> the user's forecast weekly % in [a, b), or
    None (no forecast: the straight line). threshold = "auto"/None (dynamic_threshold(ratio)) or a
    %. -> dict(go, headroom, target, mode, postpone, recheck_at, session_cap, reason, threshold,
    predicted_end, run_cost, ...)."""
    P = {**DEFAULTS, **(P or {})}
    win = win or afclaude_config.window()
    thr, thr_src = threshold_for(ratio, threshold)
    d = {"go": False, "headroom": None, "target": None, "mode": None, "postpone": False, "recheck_at": None,
         "session_cap": P["session_cap"], "user_active": None, "ratio": ratio, "last_mile_h": None,
         "last_mile_start": None, "t0": None, "w0": None, "forecast": None, "run_cost": None,
         "full_session_cost": full_session_cost(ratio), "predicted_end": None, "threshold": thr,
         "threshold_source": thr_src}
    if weekly_pct is None or resets_at is None:
        return dict(d, postpone=True, recheck_at=now + RETRY,
                    reason=f"HOLD: weekly usage unknown (fail-safe), recheck {_b(now + RETRY)}")
    if resets_at <= now:
        return dict(d, postpone=True, recheck_at=now + RETRY,
                    reason=f"HOLD: weekly reset time already passed (stale usage data), recheck {_b(now + RETRY)}")
    w = float(weekly_pct)
    if w >= 100:
        return dict(d, headroom=0.0, target=100.0, recheck_at=resets_at,
                    reason=f"HOLD: weekly limit exhausted until {_b(resets_at)}; budget for this run +0.0%")
    lm_h = last_mile_hours(w, ratio, lm_setting)
    lm_start = resets_at - timedelta(hours=lm_h)
    d.update(last_mile_h=lm_h, last_mile_start=lm_start)
    s_live = session_pct is not None and session_resets_at is not None and session_resets_at > now
    active = (not activity_known) or (msu is not None and msu < P["idle_min"])   # idle from last + idle_min
    d["user_active"] = active

    if lm_h > 0 and now >= lm_start:
        head = 100.0 - w
        d.update(mode="last_mile", headroom=head, target=100.0, session_cap=LAST_MILE_SESSION_CAP)
        base = (f"last stretch (final {lm_h:g} h before the weekly reset {_b(resets_at)}"
                + (f" = min(ceil({sessions_left(w, ratio):.2f}), {LAST_MILE_MAX_SESSIONS}) session windows"
                   if lm_setting == "auto" else "")
                + f"): week {w:.0f}% used, budget for this run +{head:.1f}% (up to 100%)")
        cap, check_user = LAST_MILE_SESSION_CAP, P["last_mile_yield"]
    else:
        # the night gate: a full session window, only if the week still ends <= the threshold
        full = full_session_cost(ratio)
        if s_live:
            cost = ratio * max(100.0 - float(session_pct), 0.0)
            what = f"the rest of this session window ({float(session_pct):.0f}% used)"
        else:
            cost, what = full, "a full session window"
        cost = min(cost, 100.0 - w)
        thr_txt = f"threshold {thr:.1f}% ({thr_src})"
        if forecast is not None:
            fc = forecast(now, resets_at)
            pred = w + cost + fc
            ok = pred <= thr
            how = (f"predicted end {w:.0f}% now + {cost:.1f}% for {what} + {fc:.1f}% forecast user use until "
                   f"the reset {_b(resets_at)} = {pred:.1f}% {'<=' if ok else '>'} {thr_txt}")
            d.update(forecast=fc, predicted_end=pred)
        else:
            ws = latest_window_start(now, win)
            end = min(max(window_end(ws, win), now), resets_at)
            frac = min(max((end - (resets_at - WEEK)) / WEEK, 0.0), 1.0)
            line = thr * frac
            pred = w + cost
            ok = pred <= line
            how = (f"straight line: {w:.0f}% now + {cost:.1f}% for {what} = {pred:.1f}% "
                   f"{'<=' if ok else '>'} {thr:.1f}% x elapsed at the window end {_b(end)} = {line:.1f}% "
                   f"({thr_src})")
            d.update(predicted_end=pred)
        head = cost if ok else 0.0
        d.update(mode="night", headroom=head, target=w + head, t0=now, w0=w, run_cost=cost)
        base = (f"week {w:.0f}% used; {how}; budget for this run +{head:.1f}% "
                f"(≈ {head / full:.1f} session windows)")
        if not ok or head <= P["min_gap"]:
            nxt = next_session_start(now, win)     # D-202: the next session-window start re-checks
            why = "the week would end above the threshold" if not ok else "no room left"
            return dict(d, recheck_at=nxt, reason=f"HOLD: no run ({why}), next check at the session-window "
                                                  f"start {_b(nxt)}; {base}")
        cap, check_user = P["session_cap"], True
    if check_user and not activity_known:
        r = now + RETRY
        return dict(d, postpone=True, recheck_at=r,
                    reason=f"HOLD: user activity unknown (sampler data missing/stale), treated as active; "
                           f"postponed, recheck {_b(r)}; {base}")
    if check_user and active:
        r = now + timedelta(minutes=P["idle_min"] - msu)
        return dict(d, postpone=True, recheck_at=r,
                    reason=f"HOLD: user active {msu:.0f} min ago (yield); postponed to {_b(r)} "
                           f"(last activity + {P['idle_min']:g} min); {base}")
    if s_live and float(session_pct) >= cap:
        return dict(d, postpone=True, recheck_at=session_resets_at,
                    reason=f"HOLD: session guard, session {float(session_pct):.0f}% >= {cap:.0f}%; "
                           f"postponed to the session reset {_b(session_resets_at)}; {base}")
    return dict(d, go=True, reason=f"CONTINUE: {base}")


def decide(usage, now, params=None, rows=None, long_rows=None, msu=None, activity_known=None, fires=None):
    """Decision from a keepalive usage dict. rows = sampler rows for activity (default: the tail
    of data/samples.jsonl); long_rows for the ratio and the forecast (default: rows if given,
    else a longer tail)."""
    P, src = params or load_params()
    w = (usage or {}).get("weekly") or {}
    s = (usage or {}).get("session") or {}
    if long_rows is None:
        long_rows = rows
    if rows is None:
        try:
            rows = tail_rows()
        except Exception:   # noqa: BLE001 - no samples: unknown activity (HOLD)
            rows = []
    if activity_known is None:
        msu = minutes_since_user(now, rows)
        activity_known = msu is not None
    ratio, rsrc = ratio_info(long_rows)
    rates, fsrc = profile(long_rows, now, fires)
    resets_at = w.get("resets_at")
    fc = None if rates is None else (lambda a, b: forecast_user(rates, a, b))
    setting, tsrc = afclaude_config.reserve_threshold_setting()
    d = decide_core(w.get("percent"), resets_at, now, msu, P, s.get("percent"), s.get("resets_at"),
                    activity_known, ratio, afclaude_config.last_mile_setting(), afclaude_config.window(),
                    setting, fc)
    if setting != "auto":
        d["threshold_source"] = tsrc
        d["reason"] = d["reason"].replace("(override)", f"({tsrc})")
    d.update(source=src, ratio_source=rsrc, forecast_source=fsrc)
    if d["headroom"] is not None:
        d["reason"] += f" [pacing: {fsrc}; ratio {ratio:.3g} ({rsrc})]"
    return d


# ------------------------------------------------------------------ next run (D-166)

NEXT_RUN_LABELS = {"now": "running now", "night": "night window", "last_stretch": "last stretch",
                   "postponed": "postponed after activity", "after_reset": "after the weekly reset",
                   "unknown": "unknown"}


def _bs(t):
    """Short Berlin time for one-line reasons: 'Thu 14:00'."""
    return t.astimezone(BERLIN).strftime("%a %H:%M")


def _last_stretch_start(w, resets_at, now, ratio, lm_setting, fc):
    """Start of the last stretch, with its length from the weekly % at its start (w + fc(now, start):
    the user's forecast use until then, or 0 = no more usage). None = off, or the week is used up."""
    h, seen = last_mile_hours(min(w, 100.0), ratio, lm_setting), []
    while h not in seen:
        seen.append(h)
        wl = w + fc(now, max(resets_at - timedelta(hours=h), now))
        h2 = last_mile_hours(min(wl, 100.0), ratio, lm_setting)
        if h2 == h:
            break
        if h2 in seen:      # 5 h <-> 10 h flip-flop: take the longer stretch (the earlier run)
            h = max(seen[seen.index(h2):])
            break
        h = h2
    return resets_at - timedelta(hours=h) if h > 0 else None


def _no_usage(a, b):
    return 0.0


START_DUE = timedelta(hours=1)   # the cron runs --window-start at :00 of each session-window start's hour


def next_run_core(weekly_pct, resets_at, now, ratio=DEFAULT_RATIO, forecast=None, threshold="auto",
                  lm_setting="auto", win=None, current=None, session_pct=None, session_resets_at=None,
                  P=None, max_nights=8, active=None, deferred=None, slot_next=None):
    """When the next autonomous run will take place (pure): only what the runner will actually do
    (D-202). The runner checks the night gate only at each SESSION-WINDOW START inside the night
    window (session_starts(): 23:00 and 04:00 by default), so this walks the upcoming session-window
    starts until the weekly reset and evaluates the gate at each: predicted_end = w_start + run_cost
    + forecast(start, reset) <= threshold (the straight line without a forecast), as the real gate
    will at that time. The primary result assumes NO MORE USAGE (D-200): w_start = w now. The first
    start that passes is the next run; if none passes, the start of the last stretch before the reset
    (D-020, its length from w now); without one, the first session-window start after the reset.
    "expected" holds the same walk with the user's forecast use until each start
    (w_start = w + forecast(now, start)).
    "now" only when the runner runs at this moment: `active` (a run is going; truthy, e.g. the time
    of its fire), current = the decide() dict for now is a go inside the last stretch, or a go at a
    session-window start whose check is due now (within START_DUE of it). Between starts a passing
    gate is NOT "now" (e.g. 08:18: the next start). A postponed start (yield, D-018) is "postponed"
    to its recheck only while that is before postpone_deadline() (the next start of the night; a
    postponed start never runs past the window end), else the walk. deferred = the runner's pending
    postponed start (its recheck time), False = none, None = unknown (inferred from current).
    In the last stretch a postponed current is "postponed" (the last-stretch pass rechecks).
    slot_next = the runner's last-stretch slot state (keepalive.last_mile_next_slot()): a datetime
    = this slot already ran, the next slot starts then; False = the final slot already ran; None =
    the slot start is still due / unknown. A run ended at a limit is not continued at the reset
    (D-204), so in the last stretch "now" needs a due slot start (or `active`).
    -> {at, kind, label, reason, predicted_end, threshold, last_stretch_at,
        expected: {at, kind, label, reason, predicted_end, last_stretch_at}}; kind in NEXT_RUN_LABELS."""
    P = {**DEFAULTS, **(P or {})}
    win = win or afclaude_config.window()
    thr, _ = threshold_for(ratio, threshold)
    fc = forecast or _no_usage
    out = {"at": None, "kind": "unknown", "reason": None, "predicted_end": None, "threshold": thr,
           "last_stretch_at": None}

    def res(kind, at, reason, **kw):
        return dict(out, kind=kind, label=NEXT_RUN_LABELS[kind], at=at, reason=reason, **kw)

    def both(r, expected=None):
        e = expected or r
        r["expected"] = {k: e.get(k) for k in ("at", "kind", "label", "reason", "predicted_end",
                                               "last_stretch_at")}
        return r

    resets_at = _round_reset(resets_at)
    if weekly_pct is None or resets_at is None or resets_at <= now:
        return both(res("unknown", None, "weekly usage unknown or stale: no forecast of the next run"))
    w = float(weekly_pct)
    after = next_session_start(resets_at - timedelta(seconds=1), win)
    if w >= 100:
        return both(res("after_reset", after, f"weekly limit used up until the reset {_bs(resets_at)}"))
    out["last_stretch_at"] = _last_stretch_start(w, resets_at, now, ratio, lm_setting, _no_usage)
    cur = current or {}
    if active:
        since = f" (started {_bs(active)})" if isinstance(active, datetime) else ""
        return both(res("now", now, f"a run is going{since}", predicted_end=cur.get("predicted_end")))
    if slot_next is False or isinstance(slot_next, datetime):
        # the current last-stretch slot already ran (D-204: no continue at a limit reset)
        if isinstance(slot_next, datetime) and slot_next > now:
            return both(res("last_stretch", slot_next, f"this last-stretch slot already ran (no continue at a "
                                                       f"limit reset, D-204); next slot start {_bs(slot_next)}"))
        return both(res("after_reset", after, f"the final last-stretch slot already ran (no continue at a "
                                              f"limit reset, D-204); first session-window start after the "
                                              f"reset {_bs(after)}"))
    why = str(cur.get("reason") or "").split(";")[0].replace("HOLD: ", "")

    def go_now(what):
        return both(res("now", now, f"{what}: budget +{float(cur.get('headroom') or 0):.1f}% "
                                    f"(up to {float(cur.get('target') or 0):.1f}%)",
                        predicted_end=cur.get("predicted_end")))

    def postponed(at, reason=None):
        r = res("postponed", at, reason or why, predicted_end=cur.get("predicted_end"))
        if "session guard" in r["reason"] or "unknown" in r["reason"]:
            r["label"] = "postponed (" + ("session guard" if "session guard" in r["reason"]
                                          else "activity unknown") + ")"
        return both(r)

    s0 = latest_session_start(now, win) if cur.get("mode") == "night" else None
    if cur.get("mode") == "last_mile":
        if cur.get("go"):
            return go_now("last stretch")
        if cur.get("postpone") and cur.get("recheck_at"):
            return postponed(cur["recheck_at"])
    elif s0 is not None:
        if cur.get("go") and now - s0 < START_DUE:
            return go_now(f"session-window start {_bs(s0)}, night gate passed")
        dl = postpone_deadline(s0, win)
        pend = deferred if isinstance(deferred, datetime) else \
            (cur.get("recheck_at") if deferred is None and cur.get("postpone") else None)
        if pend is not None and pend < dl:
            if cur.get("go"):
                if now >= pend:
                    return go_now(f"postponed session-window start {_bs(s0)}, night gate passed")
                return postponed(pend, f"postponed session-window start {_bs(s0)}, recheck {_bs(pend)}")
            if cur.get("postpone") and cur.get("recheck_at") and cur["recheck_at"] < dl:
                return postponed(cur["recheck_at"])
        # otherwise no run before the next session-window start (a postponement past it is
        # skipped, D-202): the walk below
    full = full_session_cost(ratio)

    def walk(pre):
        """The walk over the session-window starts with pre(now, start) = the user's use until each."""
        lm = _last_stretch_start(w, resets_at, now, ratio, lm_setting, pre)
        if lm is not None and now >= lm:
            return res("last_stretch", now, f"in the last stretch before the reset {_bs(resets_at)}",
                       last_stretch_at=lm)
        first, s = None, next_session_start(now, win)
        for _ in range(max_nights * len(session_starts(s, win))):
            if s >= resets_at or (lm is not None and s >= lm):
                break
            ws = w + pre(now, s)
            if ws >= 100:
                break
            cost = full
            if session_pct is not None and session_resets_at is not None and _round_reset(session_resets_at) > s:
                cost = ratio * max(100.0 - float(session_pct), 0.0)
            cost = min(cost, 100.0 - ws)
            if forecast is not None:
                pred, line = ws + cost + fc(s, resets_at), thr
                txt = f"predicted {pred:.1f}% {{}} {thr:.1f}%"
            else:
                end = min(window_end(latest_window_start(s, win), win), resets_at)
                line = thr * min(max((end - (resets_at - WEEK)) / WEEK, 0.0), 1.0)
                pred = ws + cost
                txt = f"straight line {pred:.1f}% {{}} {line:.1f}% (threshold {thr:.1f}% x elapsed)"
            if first is None or pred < first[0]:
                first = (pred, txt)          # the closest start: the reason shows the best case
            if pred <= line and cost > P["min_gap"]:
                return res("night", s, txt.format("≤") + f" at {_bs(s)}", predicted_end=pred,
                           last_stretch_at=lm)
            s = next_session_start(s, win)
        head = first[1].format(">") + f" until {_bs(lm or resets_at)}" if first else \
            "no session-window start before " + ("the last stretch" if lm else "the reset")
        pe = first[0] if first else None
        if lm is not None:
            return res("last_stretch", lm, f"{head}; last stretch {_bs(lm)}", predicted_end=pe,
                       last_stretch_at=lm)
        return res("after_reset", after, f"{head}; no last stretch (off or the week used up); "
                                         f"first session-window start after the reset {_bs(after)}",
                   predicted_end=pe, last_stretch_at=lm)

    return both(walk(_no_usage), walk(fc))


def next_run(usage, now, decision=None, rows=None, fires=None, active=None, deferred=None, slot_next=None):
    """next_run_core() from a keepalive usage dict with the live ratio, forecast and settings
    (as decide()); decision = the decide()/budget_eval() dict for now (computed if None); active /
    deferred / slot_next = the runner's state (keepalive.run_active(), keepalive.pending_deferral(),
    keepalive.last_mile_next_slot())."""
    w = (usage or {}).get("weekly") or {}
    s = (usage or {}).get("session") or {}
    if decision is None:
        decision = decide(usage, now, rows=rows, fires=fires)
    ratio, _ = ratio_info(rows)
    rates, fsrc = profile(rows, now, fires)
    fc = None if rates is None else (lambda a, b: forecast_user(rates, a, b))
    setting, _ = afclaude_config.reserve_threshold_setting()
    P, _ = load_params()
    d = next_run_core(w.get("percent"), w.get("resets_at"), now, ratio, fc, setting,
                      afclaude_config.last_mile_setting(), afclaude_config.window(), decision,
                      s.get("percent"), s.get("resets_at"), P, active=active, deferred=deferred,
                      slot_next=slot_next)
    d["forecast_source"] = fsrc
    return d


def budget_text(d, weekly_pct=None):
    """The 'budget for this run' line of a continue message (the same number as the reason)."""
    if d.get("headroom") is None:
        return "budget unknown"
    where = ("last stretch, up to 100%" if d.get("mode") == "last_mile" else
             f"one session window, up to {d['target']:.1f}%")
    now_txt = f"now {float(weekly_pct):.0f}%, " if weekly_pct is not None else ""
    sw = f" ≈ {d['headroom'] / (100.0 * d['ratio']):.1f} session windows" if d.get("ratio") else ""
    return f"budget for this run: about +{d['headroom']:.1f} weekly % ({now_txt}{where}){sw}"


# ------------------------------------------------------------------ forecast quality

def _r2(x):
    return round(x, 2) if _num(x) else None


def record_forecast(d, resets_at, path=None):
    """Log one window-start decision (the forecast from d["t0"], the decision time, to the
    reset) so forecast_errors() can score it later."""
    if d.get("forecast") is None or d.get("t0") is None:
        return False
    rec = {"at": d["t0"].astimezone(UTC).isoformat(), "reset": _round_reset(resets_at).isoformat(),
           "w0": d["w0"], "forecast_user": round(d["forecast"], 2), "run_cost": _r2(d.get("run_cost")),
           "predicted_end": _r2(d.get("predicted_end")), "threshold": _r2(d.get("threshold")),
           "go": d.get("go"), "target": _r2(d.get("target")), "logged_at": datetime.now(UTC).isoformat()}
    with open(path or FORECAST_LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return True


def forecast_errors(rows, fires, now, path=None):
    """Score the logged forecasts of closed weeks against the user's real use from the logged
    time to the reset. -> list of {at, forecast, actual, error} (error = forecast - actual)."""
    try:
        with open(path or FORECAST_LOG) as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
    except (OSError, ValueError):
        return []
    iv = user_intervals(sorted(rows, key=lambda r: _ts(r["at"])), fires)
    out, seen = [], set()
    for r in recs:
        a, reset = _ts(r.get("at")), _ts(r.get("reset"))
        if not a or not reset or reset > now or (r["at"], r["reset"]) in seen:
            continue
        seen.add((r["at"], r["reset"]))
        actual = sum(dw for t0, t1, dw in iv if t0 >= a and t1 <= reset + timedelta(minutes=5))
        out.append({"at": r["at"], "forecast": r["forecast_user"], "actual": actual,
                    "error": r["forecast_user"] - actual})
    return out


# ------------------------------------------------------------------ model accuracy (for the UI)

MIN_BACKTEST_CLOSED = 7     # closed-horizon points (about a week of nights) before the error is "ok"


def forecast_backtest(rows, fires, now, win=None, min_coverage=0.8):
    """Back-calculate the predictor's error from the usage data. At every past night
    session-window start t (the window start, + 5 h, ... inside the window) the profile fitted on
    the cycles closed before t (what the gate would have used) forecasts the user's use until
    the weekly reset (for the open cycle: until now, a partial horizon); actual = the user's real
    use over the same span (user_intervals, the definition the fit uses). Spans the samples cover
    less than min_coverage are skipped. -> [{at, reset, end, closed, horizon_h, predicted, actual,
    error}], error = actual - predicted (> 0: the model under-predicted, the week ended higher)."""
    win = win or afclaude_config.window()
    rows = sorted((r for r in rows or () if _ts(_dict(r).get("at"))), key=lambda r: _ts(r["at"]))
    cycles = sorted({c for c in (_round_reset(_weekly(r).get("resets_at")) for r in rows) if c is not None})
    iv = user_intervals(rows, fires)
    step = timedelta(hours=SESSION_H)
    out = []
    for c in cycles:
        start, end = c - WEEK, min(c, now)
        prior = [r for r in rows if _ts(r["at"]) <= start]
        rates = fit_profile(prior, fires, start)[0] if prior else None
        if rates is None:
            continue
        cyc_iv = [x for x in iv if x[0] >= start and x[1] <= c + timedelta(minutes=5)]
        ws = latest_window_start(start, win)
        while ws < end:
            k, we = ws, window_end(ws, win)
            while k < we:
                if start <= k and end - k >= step:
                    span = [x for x in cyc_iv if x[0] >= k and x[1] <= end + timedelta(minutes=5)]
                    hz = (end - k).total_seconds() / 3600
                    if sum((x[1] - x[0]).total_seconds() for x in span) / 3600 >= min_coverage * hz:
                        pred, act = forecast_user(rates, k, end), sum(x[2] for x in span)
                        out.append({"at": k.astimezone(UTC).isoformat(), "reset": c.isoformat(),
                                    "end": end.isoformat(), "closed": end == c, "horizon_h": round(hz, 1),
                                    "predicted": pred, "actual": act, "error": act - pred})
                k = (k.astimezone(UTC) + step).astimezone(BERLIN)
            ws = _wall(ws.astimezone(BERLIN).date() + timedelta(days=1), win[0])
    return out


def error_stats(errs):
    """bias (mean error), sd, rmse, worst under-prediction, n of a forecast_backtest() list.
    status "ok" with >= MIN_BACKTEST_CLOSED closed-horizon points, else "preliminary" (or
    "insufficient_data" without any)."""
    e = [x["error"] for x in errs]
    n, closed = len(e), sum(1 for x in errs if x.get("closed"))
    out = {"n": n, "n_closed": closed, "bias": None, "sd": None, "rmse": None, "worst": None,
           "mean_horizon_h": None}
    if not n:
        return dict(out, status="insufficient_data",
                    note="no past decision point with a fitted forecast yet (needs a closed week before it)")
    mean = sum(e) / n
    out.update(bias=mean, rmse=(sum(x * x for x in e) / n) ** 0.5, worst=max(e),
               mean_horizon_h=sum(x["horizon_h"] for x in errs) / n,
               sd=(sum((x - mean) ** 2 for x in e) / (n - 1)) ** 0.5 if n >= 2 else None)
    if closed >= MIN_BACKTEST_CLOSED:
        return dict(out, status="ok", note="back-calculated at past night session-window starts")
    return dict(out, status="preliminary",
                note=f"only {closed} point(s) with a full horizon to the reset (< {MIN_BACKTEST_CLOSED}); "
                     f"the rest end at now; points of one week overlap (correlated)")


def _rnd(x, k=1):
    return round(x, k) if _num(x) else None


def threshold_info(rows=None, fires=None, now=None, decision=None):
    """Everything the threshold setting shows, computed in code: the session/weekly ratio +- its
    spread per session window, the full-session weekly cost +- its uncertainty, the predictor's
    back-calculated error (forecast_backtest), the uncertainty of the predicted week end, the
    dynamic default threshold (one session window left) and the active threshold and its source.
    decision = a decide() dict to add the current gate numbers ("now")."""
    now = now or datetime.now(UTC)
    if rows is None:
        try:
            rows = tail_rows(max_bytes=LONG_BYTES)
        except Exception:   # noqa: BLE001
            rows = []
    fires = fire_times() if fires is None else fires
    rs = ratio_stats(rows)
    ratio = rs["value"]
    cost = full_session_cost(ratio)
    intr, se, spread = rs["intrinsic_stdev"], rs["se"], rs["weighted_stdev"]
    cost_sd = 100.0 * ((intr or 0.0) ** 2 + (se or 0.0) ** 2) ** 0.5 if intr is not None or se is not None else None
    try:
        me = error_stats(forecast_backtest(rows, fires, now))
    except Exception as e:   # noqa: BLE001 - advisory
        me = dict(error_stats([]), note=f"backtest failed: {type(e).__name__}")
    try:
        logged = [x["actual"] - x["forecast"] for x in forecast_errors(rows, fires, now)]
    except Exception:   # noqa: BLE001
        logged = []
    setting, tsrc = afclaude_config.reserve_threshold_setting()
    dyn = dynamic_threshold(ratio)
    active = dyn if setting == "auto" else float(setting)
    end_sd = (me["sd"] ** 2 + cost_sd ** 2) ** 0.5 if me["sd"] is not None and cost_sd is not None else None
    out = {
        "ratio": {"value": _rnd(ratio, 4), "source": rs["source"], "kind": rs["kind"], "n": rs["n"],
                  "spread": _rnd(spread, 4), "stdev": _rnd(rs["stdev"], 4),
                  "intrinsic_stdev": _rnd(intr, 4), "se": _rnd(se, 4), "rounding_sd": _rnd(rs["rounding_sd"], 4),
                  "p10": _rnd(rs["p10"], 4), "p90": _rnd(rs["p90"], 4)},
        "full_session_cost": {"value": _rnd(cost), "sd": _rnd(cost_sd), "spread": _rnd(100 * spread if spread is not None else None),
                              "se": _rnd(100 * se if se is not None else None),
                              "note": "weekly % of one full session window = 100 x ratio; sd = per-session "
                                      "variation without integer-rounding noise (intrinsic) and the estimate's se"},
        "model_error": {**{k: _rnd(v) if isinstance(v, float) else v for k, v in me.items()},
                        "sign": "actual - predicted user use until the reset (> 0: under-predicted)",
                        "logged": {"n": len(logged), "bias": _rnd(sum(logged) / len(logged)) if logged else None}},
        "predicted_end_sd": _rnd(end_sd),
        "threshold": {"active": _rnd(active), "source": "dynamic" if setting == "auto" else tsrc,
                      "dynamic_default": _rnd(dyn), "dynamic_default_sd": _rnd(cost_sd),
                      "override": None if setting == "auto" else float(setting),
                      "range": list(THRESHOLD_RANGE)},
    }
    if decision is not None:
        pe, thr = decision.get("predicted_end"), decision.get("threshold")
        out["now"] = {"mode": decision.get("mode"), "go": decision.get("go"),
                      "predicted_end": _rnd(pe), "threshold": _rnd(thr), "run_cost": _rnd(decision.get("run_cost")),
                      "forecast_user": _rnd(decision.get("forecast")),
                      "slack": _rnd(thr - pe) if _num(pe) and _num(thr) else None,
                      "slack_in_sd": _rnd((thr - pe) / end_sd, 2) if _num(pe) and _num(thr) and end_sd else None}
    return out


def threshold_lines(ti):
    """threshold_info() as a few text lines (keepalive.py --decide, python3 pacing.py)."""
    r, c, m, t = ti["ratio"], ti["full_session_cost"], ti["model_error"], ti["threshold"]
    pm = lambda x: f" ± {x:g}" if x is not None else ""   # noqa: E731
    out = [f"ratio {r['value']:g}{pm(r['spread'])} (spread over {r['n']} session windows; {r['source']})",
           f"full session window = {c['value']:g}{pm(c['sd'])} weekly %",
           (f"model error ({m['status']}): bias {m['bias']:+g}, sd {m['sd'] if m['sd'] is not None else '?'}, "
            f"rmse {m['rmse']:g}, worst {m['worst']:+g} weekly %, n={m['n']} ({m['n_closed']} to the reset), "
            f"actual - predicted user use" if m["n"] else f"model error: {m['note']}"),
           f"predicted week end ± {ti['predicted_end_sd']:g} weekly %" if ti["predicted_end_sd"] is not None
           else "predicted week end ± ? (too little data)",
           f"threshold {t['active']:g}% ({t['source']}; dynamic default {t['dynamic_default']:g}%"
           f"{pm(t['dynamic_default_sd'])} = one session window left)"]
    n = ti.get("now")
    if n and n.get("predicted_end") is not None:
        out.append(f"now: predicted end {n['predicted_end']:g}% vs threshold {n['threshold']:g}% "
                   f"(slack {n['slack']:+g}%" + (f", {n['slack_in_sd']:+g} sd" if n.get("slack_in_sd") is not None
                                                 else "") + ")")
    return out


def budget_decision(usage, now):
    d = decide(usage, now)
    return d["go"], d["reason"]


def budget_headroom(usage, now):
    d = decide(usage, now)
    return d["headroom"], budget_text(d, ((usage or {}).get("weekly") or {}).get("percent"))


if __name__ == "__main__":     # quick look: python3 pacing.py
    import keepalive
    _now = datetime.now(UTC)
    _u = keepalive.read_usage_cache()
    _d = decide(_u, _now)
    print(load_params()[1], "| minutes since user:", minutes_since_user(_now), "| ratio:", ratio_info())
    print(_d["go"], _d["reason"])
    print(budget_text(_d, ((_u or {}).get("weekly") or {}).get("percent")))
    print("\n".join(threshold_lines(threshold_info(now=_now, decision=_d))))
