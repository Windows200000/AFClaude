#!/usr/bin/env python3
"""Weekly budget model "pacing" (stdlib only): forecast-driven, the original linear rule as its
fallback. The default of keepalive.budget_eval() (data/afclaude.json "usage_model": "pacing").

Goal: during the week fill the weekly limit to `week_target` (data/afclaude.json, default 90,
80..95) with autonomous night work, leaving room for what the user is forecast to use, then
fill the last stretch right before the reset to 100%.

Forecast (the predictor): the user's typical weekly-% rise per hour of the week (Berlin weekday x
hour), from the closed weekly cycles in data/samples.jsonl (last 4; user-only: intervals inside
an autonomous AFClaude run, from a recorded fire until the next prompt in an AFClaude session,
are left out; usage the user drives in AFClaude sessions counts as the user's). Hours without
data use the mean rate. forecast(a, b) = forecast_margin (1.25) x the sum over [a, b).
Needs >= MIN_FORECAST_HOURS (72) of covered hours, else the straight line below is used.
Every window-start decision is logged (data/forecast_log.jsonl) and forecast_errors()
compares it with what the user really used once the week has closed.

Night plan (every decision belongs to a night: the window start t0 to the next one):
    w0       = weekly % at t0 (data/samples.jsonl)
    allow    = week_target - w0 - forecast(t0, reset)        AFClaude's share of the rest
    tonight  = allow x tonight's session windows / the night session windows before the
               last stretch                                   (spread over the nights)
    target   = w0 + max(tonight, 0)
    no forecast: target = the original linear line at the window end, week_target x elapsed / 7 d
    budget for this run (headroom) = target - w   (one number: reason, --decide, the continue
               message, the quickview; ~ session windows = headroom / (100 x ratio))
    CONTINUE iff headroom > min_gap (1%): run full session windows (cap 85%) to the target.
    Re-planned every night with the updated forecast; daytime decisions (--now, --work-on)
    spend what is left of the last night's target.

Last stretch: the final min(ceil(sessions_left), 2) x 5 h before the reset, sessions_left =
(100 - w) / (100 x ratio): budget 100 - w, session cap 100% (last_mile_hours "auto"; a number
overrides it, 0 = off).

Yield: only activity in NON-AFClaude sessions and a weekly-% rise without local turns (another
device) count as the user; the user's input to AFClaude sessions does not. A HOLD for the user
POSTPONES: recheck at last activity + idle_min (60). Unknown sampler data = active, recheck in
15 min. Also in the last stretch unless last_mile_yield is false. Fail-safes: unknown usage or
a stale reset time -> HOLD (recheck 15 min); exhausted -> HOLD.

ratio = weekly % per session % = limit_ratio.py's median (biased low by the integer weekly %, so
the last stretch tends to its 2-session cap; a per-session-window measurement is being added to
limit_ratio separately), else 0.2 (conservative), and the reason says which.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import afclaude_config
import usage_stale

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
USER_MODEL_FILE = os.path.join(DATA_DIR, "user_model.json")
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
DEFAULTS = {"idle_min": 60.0, "min_gap": 1.0, "session_cap": 85.0, "last_mile_yield": True,
            "forecast_margin": 1.25}
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
TAIL_BYTES = 512 * 1024              # activity, anchor: about the last 40 h
LONG_BYTES = 10 * 1024 * 1024        # ratio, forecast: about the last 4-5 weeks


# ------------------------------------------------------------------ params

def parse_params(d):
    """Optional overrides from user_model.json (top level or a "pacing" object)."""
    if not isinstance(d, dict):
        raise ValueError("not an object")
    src = d.get("pacing") if isinstance(d.get("pacing"), dict) else d
    over = {}
    for key, lo, hi in (("idle_min", 0, 1440), ("min_gap", 0, 50), ("session_cap", 1, 100),
                        ("forecast_margin", 1, 3)):
        v = src.get(key)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{key} out of range: {v!r}")
            over[key] = float(v)
    if src.get("last_mile_yield") is not None:
        if not isinstance(src["last_mile_yield"], bool):
            raise ValueError("last_mile_yield must be true or false")
        over["last_mile_yield"] = src["last_mile_yield"]
    return over


def load_params(path=None):
    """-> (params, source text). Never raises: a missing or invalid file means the defaults."""
    P = dict(DEFAULTS)
    try:
        with open(path or USER_MODEL_FILE) as fh:
            P.update(parse_params(json.load(fh)))
    except FileNotFoundError:
        return P, "defaults"
    except Exception as e:   # noqa: BLE001
        return P, f"defaults (user model ignored: {type(e).__name__})"
    return P, "user model"


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


def weekly_at(rows, t0, resets_at, now):
    """Weekly % at t0 in the cycle that resets at `resets_at`: the latest sample in
    [t0 - 30 min, t0 + 1 min], else the first one after t0. None if there is none."""
    cyc = _round_reset(resets_at)
    pts = []
    for r in rows or ():
        w = _weekly(r)
        t, p = _ts(_dict(r).get("at")), w.get("percent")
        if t is None or not _num(p) or _round_reset(w.get("resets_at")) != cyc or t > now + timedelta(minutes=1):
            continue
        pts.append((t, float(p)))
    pts.sort(key=lambda x: x[0])
    before = [p for t, p in pts if t0 - ANCHOR_MAX_AGE <= t <= t0 + timedelta(minutes=1)]
    if before:
        return before[-1]
    after = [p for t, p in pts if t > t0]
    return after[0] if after else None


# ------------------------------------------------------------------ ratio, last stretch

def ratio_info(rows=None):
    """-> (weekly % per session %, source text): limit_ratio.py's median (the sampler stores a
    snapshot in every row; else computed over the rows), else DEFAULT_RATIO (conservative: a
    shorter last stretch)."""
    if rows is None:
        try:
            rows = tail_rows(max_bytes=LONG_BYTES)
        except Exception:   # noqa: BLE001
            rows = []
    full = None
    for r in reversed(rows):
        if isinstance(_dict(r).get("limit_ratio"), dict):
            full = r["limit_ratio"]
            break
    if full is None and rows:
        try:
            import limit_ratio
            full = limit_ratio.compute(rows)
        except Exception:   # noqa: BLE001
            full = None
    # prefer the per-session-window estimate (unbiased by the integer weekly %) once it has
    # enough windows; limit_ratio.preferred_ratio() falls back to the old 15-min median
    try:
        import limit_ratio
        pr = limit_ratio.preferred_ratio(full)
        v = pr.get("value")
        if pr.get("source") == "windows" and _num(v) and RATIO_RANGE[0] <= v <= RATIO_RANGE[1]:
            return float(v), f"per-session-window ratio, n={pr.get('n')} windows"
    except Exception:   # noqa: BLE001
        pass
    snap = _dict(_dict(full).get("ratio"))
    med = snap.get("median")
    if snap.get("status") == "ok" and _num(med) and RATIO_RANGE[0] <= med <= RATIO_RANGE[1]:
        return float(med), f"limit_ratio median, n={snap.get('n')}"
    why = (f"insufficient data, n={snap.get('n')}" if snap.get("status") == "insufficient_data"
           else "no limit_ratio data")
    return DEFAULT_RATIO, f"conservative default {DEFAULT_RATIO:g} (limit_ratio: {why})"


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


def night_sessions(a, b, win):
    """Session windows of night-window time in [a, b) (fractional)."""
    if b <= a:
        return 0.0
    s, tot = latest_window_start(a, win), timedelta(0)
    while s < b:
        lo, hi = max(s, a), min(window_end(s, win), b)
        if hi > lo:
            tot += hi.astimezone(UTC) - lo.astimezone(UTC)   # same-tzinfo subtraction ignores DST
        s = _wall(s.astimezone(BERLIN).date() + timedelta(days=1), win[0])
    return tot.total_seconds() / 3600 / SESSION_H


def night_start(now, resets_at, win):
    """-> (t0, has_night): the night `now` belongs to; if its window started last week, the
    weekly reset (a night only if the reset falls inside a window)."""
    ws = latest_window_start(now, win)
    week_start = resets_at - WEEK
    if ws >= week_start:
        return ws, True
    return week_start, in_window(week_start, win)


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


def forecast_user(rates, a, b, margin=1.0):
    """Forecast of the user's weekly-% use in [a, b)."""
    if rates is None or b <= a:
        return 0.0
    tot, t = 0.0, a
    while t < b:
        nxt = min((t.astimezone(UTC) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), b)
        tot += rates[_how(t)] * (nxt - t).total_seconds() / 3600
        t = nxt
    return margin * tot


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
                activity_known=True, ratio=DEFAULT_RATIO, lm_setting="auto", win=None, week_target=90.0,
                forecast=None, anchor=None):
    """Pure decision. forecast = a function (a, b) -> the user's forecast weekly % in [a, b), or
    None (no forecast: the straight line). anchor = the weekly % at the night's start, or a
    function t0 -> that (None: the current %). -> dict(go, headroom, target, mode, postpone,
    recheck_at, session_cap, reason, ...)."""
    P = {**DEFAULTS, **(P or {})}
    win = win or afclaude_config.window()
    d = {"go": False, "headroom": None, "target": None, "mode": None, "postpone": False, "recheck_at": None,
         "session_cap": P["session_cap"], "user_active": None, "ratio": ratio, "last_mile_h": None,
         "last_mile_start": None, "t0": None, "w0": None, "forecast": None, "allow": None}
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
        t0, has_night = night_start(now, resets_at, win)
        if not has_night:
            nxt = next_window_start(now, win)
            return dict(d, mode="none", headroom=0.0, target=w, t0=t0, recheck_at=nxt,
                        reason=f"HOLD: no night window since the weekly reset yet (next {_b(nxt)}); "
                               f"budget for this run +0.0%")
        w0 = anchor(t0) if callable(anchor) else anchor
        w0 = w if w0 is None else min(float(w0), w)
        end = min(window_end(t0, win), resets_at)
        if forecast is not None:
            lm0 = resets_at - timedelta(hours=last_mile_hours(w0, ratio, lm_setting))
            fc = forecast(t0, resets_at)
            allow = week_target - w0 - fc
            tonight = night_sessions(t0, min(end, lm0), win)
            nights = night_sessions(t0, lm0, win)
            share = allow * tonight / nights if nights > 0 else allow
            target = w0 + max(share, 0.0)
            how = (f"plan: {week_target:g}% target - {w0:.0f}% at {_b(t0)} - {fc:.1f}% forecast user use "
                   f"until the reset = {allow:.1f}% x {tonight:.1f} of {nights:.1f} night sessions = {share:.1f}%")
            d.update(forecast=fc, allow=allow)
        else:
            frac = min(max((end - (resets_at - WEEK)) / WEEK, 0.0), 1.0)
            target = week_target * frac
            how = f"straight line {week_target:g}% x elapsed at the window end {_b(end)} = {target:.1f}%"
        head = max(min(target - w, 100.0 - w), 0.0)
        d.update(mode="night", headroom=head, target=target, t0=t0, w0=w0)
        base = (f"week {w:.0f}% used; tonight up to {target:.1f}% ({how}); budget for this run +{head:.1f}% "
                f"(≈ {head / (100.0 * ratio):.1f} session windows)")
        if head <= P["min_gap"]:
            nxt = next_window_start(now, win)
            return dict(d, recheck_at=nxt, reason=f"HOLD: on or ahead of the plan, next night {_b(nxt)}; {base}")
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
    """Decision from a keepalive usage dict. rows = sampler rows for activity and the anchor
    (default: the tail of data/samples.jsonl); long_rows for the ratio and the forecast
    (default: rows if given, else a longer tail)."""
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
    fc = None if rates is None else (lambda a, b: forecast_user(rates, a, b, P["forecast_margin"]))
    d = decide_core(w.get("percent"), resets_at, now, msu, P, s.get("percent"), s.get("resets_at"),
                    activity_known, ratio, afclaude_config.last_mile_setting(), afclaude_config.window(),
                    afclaude_config.week_target(), fc, lambda t0: weekly_at(rows, t0, resets_at, now))
    d.update(source=src, ratio_source=rsrc, forecast_source=fsrc)
    if d["headroom"] is not None:
        d["reason"] += f" [pacing: {fsrc}; ratio {ratio:.3g} ({rsrc})]"
    return d


def budget_text(d, weekly_pct=None):
    """The 'budget for this run' line of a continue message (the same number as the reason)."""
    if d.get("headroom") is None:
        return "budget unknown"
    where = "last stretch, up to 100%" if d.get("mode") == "last_mile" else f"up to {d['target']:.1f}% tonight"
    now_txt = f"now {float(weekly_pct):.0f}%, " if weekly_pct is not None else ""
    sw = f" ≈ {d['headroom'] / (100.0 * d['ratio']):.1f} session windows" if d.get("ratio") else ""
    return f"budget for this run: about +{d['headroom']:.1f} weekly % ({now_txt}{where}){sw}"


# ------------------------------------------------------------------ forecast quality

def record_forecast(d, resets_at, path=None):
    """Log one night's plan (once per window start) so forecast_errors() can score it later."""
    if d.get("forecast") is None or d.get("t0") is None:
        return False
    rec = {"at": d["t0"].astimezone(UTC).isoformat(), "reset": _round_reset(resets_at).isoformat(),
           "w0": d["w0"], "forecast_user": round(d["forecast"], 2), "allow": round(d["allow"], 2),
           "target": round(d["target"], 2), "logged_at": datetime.now(UTC).isoformat()}
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
