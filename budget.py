#!/usr/bin/env python3
"""Weekly budget model "nightly share + last mile" (stdlib only). The default model of
keepalive.budget_decision() / budget_headroom() (data/afclaude.json "usage_model": "budget").

Goal: use the whole weekly limit by each reset with autonomous work, without getting in the
user's way. AFClaude works in the nightly windows (afclaude_config.window(), default
23:00-09:00 Europe/Berlin) and, at the end of the week, in the last mile.

    w        = weekly % used now, T = hours until the weekly reset
    ratio    = weekly % per session %  (limit_ratio.py median; DEFAULT_RATIO if it has too
               little data, and the reason says so)
    S_left   = (100 - w) / (100 * ratio)          session windows of quota left
    LM       = ceil(S_left) * SESSION_LENGTH      last-mile length ("auto"; data/afclaude.json
               last_mile_hours = a number overrides it, 0 = off)

Last mile (T <= LM): budget = 100 - w, session cap 100%, no reserve, no yield (owner rule;
user_model.json last_mile_yield: true would yield there too). The only HOLDs are unknown
usage and an exhausted week (and a session that is at its hard limit).

Every other decision belongs to one night: the period from a window start to the next one
(daytime decisions such as --now spend what is left of the last night's budget).

    t0, w0   = the window start of this night and the weekly % then (data/samples.jsonl)
    need     = safety * env(T0)                   the user's projected need until the reset
               (env = the user's demand envelope: max weekly-% rise within any h hours)
    N        = nightly windows from tonight up to the last-mile start (>= 1)
    share    = (100 - w0 - need) / N              AFClaude's fair share per night
    free     = 100 - w0 - need                    what the user's projected need leaves
    floor    = min(night_floor, max(free, night_floor_min))
               a small share is topped up to night_floor (2%) only out of `free`, so the
               user's projected need stays covered; only night_floor_min (0.5%) per night is
               not covered by it: that is what makes every night get a budget
    budget   = min(max(share, floor), 100 - w0)
    target   = w0 + budget
    headroom = target - w                         THE budget number for this run (one number,
               used in the decision reason, budget_headroom(), --decide, the continue
               message and the quickview)

CONTINUE iff headroom > min(min_gap, budget / 2), the user is idle and the session is below
session_cap (85%). A HOLD for the user is a POSTPONE: recheck at the last user activity +
idle_min (unknown activity or usage: +15 min; session guard: at the session reset), so a
whole night does not fall apart. A used-up night budget is a final HOLD until the next night.

User activity (data/samples.jsonl, usage_sampler.py): only turns or prompts in NON-AFClaude
sessions (activity.other) and a weekly-% rise in an interval without any local turns (another
device / claude.ai). The user's input to AFClaude sessions (the manager, task sessions) is
not user activity. Missing or stale (> 30 min) sampler data = unknown = treated as active.

Envelope: data/user_model.json (format below, local only); < 1 closed week -> the generic
envelope; 1 to < 4 weeks -> pointwise max(user, 0.5 x generic); >= 4 weeks -> the user's,
floored at 0.5 x generic.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import afclaude_config

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
USER_MODEL_FILE = os.path.join(DATA_DIR, "user_model.json")
SAMPLES_FILE = os.path.join(DATA_DIR, "samples.jsonl")

SCHEMA = "afclaude.user_model/1"
WEEK = timedelta(days=7)
SESSION_H = afclaude_config.SESSION_LENGTH.total_seconds() / 3600
# Generic demand envelope (hours -> weekly %): about one session window per 5 h for the
# first day, then conservative up to the whole week.
GENERIC_ENVELOPE = {1: 12.0, 3: 16.0, 5: 16.0, 10: 32.0, 24: 50.0, 48: 70.0, 96: 90.0, 168: 100.0}
DEFAULTS = {
    "safety": 1.25,       # multiplier on the envelope (review 2026-10-01: 1.0 overfits)
    "idle_min": 60.0,     # the user counts as active this long after the last activity
    "min_gap": 1.0,       # don't start for crumbs: CONTINUE iff headroom > min(min_gap, budget/2)
    "session_cap": 85.0,  # session % AFClaude may fill a window to (outside the last mile)
    "night_floor": 2.0,   # weekly % a night gets when the share is smaller, out of `free` ...
    "night_floor_min": 0.5,  # ... and at least this (every night has a budget unless exhausted)
    "last_mile_yield": False,  # owner rule: no HOLD in the last mile (True: yield there too)
    "blend_weeks": 4.0,   # closed weeks before the user's envelope is used alone
}
LAST_MILE_SESSION_CAP = 100.0
DEFAULT_RATIO = 0.2       # weekly % per session %, used while limit_ratio has too few pairs:
                          # above the review's observed 0.15-0.19, so the last mile is shorter
RATIO_RANGE = (0.01, 1.0)  # a limit_ratio median outside this is not trusted
ENV_FLOOR_FRAC = 0.5
RETRY = timedelta(minutes=15)            # recheck of a HOLD on unknown data
SAMPLE_MAX_AGE = timedelta(minutes=30)   # older latest sample = sampler down -> unknown activity
ANCHOR_MAX_AGE = timedelta(minutes=30)   # a sample this long before the night start may anchor it
TAIL_BYTES = 512 * 1024                  # only the end of samples.jsonl is read


# ------------------------------------------------------------------ envelope

def _table(env):
    return sorted((float(k), float(v)) for k, v in env.items())


def interp_env(env, hours):
    """Envelope value at `hours`: linear through (0, 0) and the table, flat after the last point."""
    pts = [(0.0, 0.0)] + _table(env)
    h = max(float(hours), 0.0)
    if h >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= h <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (h - x0) / (x1 - x0)
    return pts[-1][1]


def effective_envelope(user_env, weeks, blend_weeks=DEFAULTS["blend_weeks"]):
    """-> (envelope, source): generic (< 1 closed week), blended (< blend_weeks), user."""
    if not user_env or weeks is None or weeks < 1:
        return dict(GENERIC_ENVELOPE), "generic"
    if weeks < blend_weeks:
        keys = sorted({float(k) for k in user_env} | set(GENERIC_ENVELOPE))
        return ({k: max(interp_env(user_env, k), 0.5 * interp_env(GENERIC_ENVELOPE, k)) for k in keys},
                "blended")
    return {float(k): float(v) for k, v in user_env.items()}, "user"


def floored(env):
    """Never trust an envelope below ENV_FLOOR_FRAC x the generic one (any horizon)."""
    keys = sorted(set(env) | set(GENERIC_ENVELOPE))
    return {k: max(interp_env(env, k), ENV_FLOOR_FRAC * interp_env(GENERIC_ENVELOPE, k)) for k in keys}


def _num(x, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError(f"not a finite number: {x!r}")
    x = float(x)
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f"out of range: {x}")
    return x


def parse_user_model(d):
    """Validate a user_model.json document -> (envelope, weeks, param overrides).
    Raises ValueError on anything invalid."""
    if not isinstance(d, dict):
        raise ValueError("not an object")
    if d.get("schema") not in (None, SCHEMA):
        raise ValueError(f"unknown schema {d.get('schema')!r}")
    raw = d.get("envelope_weekly_pct_by_hours")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("envelope_weekly_pct_by_hours missing")
    env = {}
    for k, v in raw.items():
        h = float(k)
        if not (math.isfinite(h) and h > 0):
            raise ValueError(f"envelope hour {k!r} <= 0")
        env[h] = _num(v, 0, 100)
    vals = [v for _, v in _table(env)]
    if any(b < a for a, b in zip(vals, vals[1:])):
        raise ValueError("envelope not non-decreasing")
    weeks = _num(d.get("weeks_of_data"), 0)
    rec, dec = d.get("recommended") or {}, d.get("decider") or {}
    if not isinstance(rec, dict) or not isinstance(dec, dict):
        raise ValueError("recommended/decider must be objects")
    over = {}
    for key, alt, lo, hi in (("safety", rec.get("safety"), 1.0, 5.0),
                             ("idle_min", dec.get("idle_min"), 0, 1440),
                             ("min_gap", dec.get("min_gap"), 0, 50),
                             ("session_cap", dec.get("session_cap_pct"), 1, 100),
                             ("night_floor", dec.get("night_floor"), 0, 100),
                             ("night_floor_min", dec.get("night_floor_min"), 0, 100)):
        v = d.get(key, alt)
        if v is not None:
            over[key] = _num(v, lo, hi)
    lmy = d.get("last_mile_yield", dec.get("last_mile_yield"))
    if lmy is not None:
        if not isinstance(lmy, bool):
            raise ValueError(f"last_mile_yield must be true or false, got {lmy!r}")
        over["last_mile_yield"] = lmy
    return env, weeks, over


def load_params(path=None):
    """-> (params, envelope, source text). Never raises: a missing or invalid file means
    the generic defaults."""
    path = path or USER_MODEL_FILE
    P = dict(DEFAULTS)
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return P, dict(GENERIC_ENVELOPE), "generic envelope (no user model)"
    except Exception as e:   # noqa: BLE001
        return P, dict(GENERIC_ENVELOPE), f"generic envelope (user model unreadable: {type(e).__name__})"
    try:
        user_env, weeks, over = parse_user_model(doc)
    except Exception as e:   # noqa: BLE001 - any bad file means the generic defaults
        return P, dict(GENERIC_ENVELOPE), f"generic envelope (user model invalid: {e})"
    P.update(over)
    env, src = effective_envelope(user_env, weeks, P["blend_weeks"])
    if src != "generic":
        env = floored(env)
    return P, env, f"{src} envelope ({weeks:g} closed weeks)"


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


_TAIL_CACHE = {}


def tail_rows(path=None, max_bytes=TAIL_BYTES):
    """Rows of the end of samples.jsonl, oldest first (cached while the file is unchanged)."""
    path = path or SAMPLES_FILE
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if _TAIL_CACHE.get("key") == key:
        return _TAIL_CACHE["rows"]
    with open(path, "rb") as fh:
        fh.seek(max(st.st_size - max_bytes, 0))
        chunk = fh.read()
    lines = chunk.split(b"\n")
    if st.st_size > max_bytes:
        lines = lines[1:]        # first line is cut
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and _ts(r.get("at")):
            rows.append(r)
    rows.sort(key=lambda r: _ts(r["at"]))
    _TAIL_CACHE.update(key=key, rows=rows)
    return rows


def _rows_or_empty(rows):
    if rows is not None:
        return rows
    try:
        return tail_rows()
    except Exception:   # noqa: BLE001 - no samples: unknown activity (HOLD), default ratio
        return []


def _weekly(r):
    return _dict(_dict(_dict(r).get("usage")).get("weekly"))


def _count(d, key):
    """Non-negative count from a sampler field; anything malformed counts as activity (1),
    so bad data never hides the user."""
    v = d.get(key) if isinstance(d, dict) else None
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 1
    return max(int(v), 0)


TURNS = ("assistant_turns", "subagent_turns")


def user_signal(row, prev):
    """True if this sample interval shows the user using Claude OUTSIDE AFClaude: activity in a
    non-AFClaude session (activity.other: prompts or turns), or a weekly-% rise without any
    local turns (another device / claude.ai). Prompts in AFClaude's own sessions (activity.own,
    e.g. the user talking to the manager session) do not count."""
    a = _dict(row.get("activity"))
    own, oth = _dict(a.get("own")), _dict(a.get("other"))
    if _count(oth, "human_prompts") or any(_count(oth, k) for k in TURNS):
        return True
    w, pw = _weekly(row).get("percent"), (_weekly(prev).get("percent") if prev else None)
    if isinstance(w, (int, float)) and isinstance(pw, (int, float)) and w > pw \
            and not any(_count(own, k) for k in TURNS):
        return True
    return False


def minutes_since_user(now, rows=None):
    """-> minutes since the last user activity, or None if unknown (no / stale / unreadable
    samples: the caller treats the user as active). Without any activity in the rows read:
    the time since the oldest of them."""
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
    except Exception:   # noqa: BLE001 - unknown activity = active user (HOLD)
        return None


def weekly_at(rows, t0, resets_at, now):
    """Weekly % at t0 (a night's start) in the weekly cycle that resets at `resets_at`: the
    latest sample of that cycle in [t0 - ANCHOR_MAX_AGE, t0 + 1 min], else its first sample
    after t0 (up to now). None if there is none."""
    cyc = _round_reset(resets_at)
    pts = []
    for r in rows or ():
        w = _weekly(r)
        t, p = _ts(_dict(r).get("at")), w.get("percent")
        if t is None or isinstance(p, bool) or not isinstance(p, (int, float)) \
                or _round_reset(w.get("resets_at")) != cyc or t > now + timedelta(minutes=1):
            continue
        pts.append((t, float(p)))
    pts.sort(key=lambda x: x[0])
    before = [p for t, p in pts if t0 - ANCHOR_MAX_AGE <= t <= t0 + timedelta(minutes=1)]
    if before:
        return before[-1]
    after = [p for t, p in pts if t > t0]
    return after[0] if after else None


def ratio_info(rows=None, now=None):
    """-> (weekly % per session %, source text). The sampler's latest limit_ratio snapshot
    (limit_ratio.py median over all usable sample pairs), else limit_ratio over the rows read,
    else DEFAULT_RATIO with the reason."""
    rows = _rows_or_empty(rows)
    snap = None
    for r in reversed(rows):
        if isinstance(_dict(r).get("limit_ratio"), dict):
            snap = r["limit_ratio"].get("ratio")
            break
    if snap is None and rows:
        try:
            import limit_ratio
            snap = limit_ratio.compute(rows, now=now).get("ratio")
        except Exception:   # noqa: BLE001
            snap = None
    snap = _dict(snap)
    med = snap.get("median")
    if snap.get("status") == "ok" and isinstance(med, (int, float)) and not isinstance(med, bool) \
            and RATIO_RANGE[0] <= med <= RATIO_RANGE[1]:
        return float(med), f"limit_ratio median, n={snap.get('n')}"
    why = (f"insufficient data, n={snap.get('n')}" if snap.get("status") == "insufficient_data"
           else f"implausible median {med!r}" if med is not None else "no limit_ratio data")
    return DEFAULT_RATIO, f"conservative default {DEFAULT_RATIO:g} (limit_ratio: {why})"


# ------------------------------------------------------------------ last mile, nights

def sessions_left(weekly_pct, ratio):
    """Full session windows of weekly quota left: (100 - w) / (100 * ratio)."""
    return max(100.0 - float(weekly_pct), 0.0) / (100.0 * ratio)


def last_mile_hours(weekly_pct, ratio, setting="auto"):
    """Length of the last mile: "auto" = ceil(sessions_left) x SESSION_LENGTH; a number
    overrides it (0 = off)."""
    if setting != "auto":
        return max(float(setting), 0.0)
    return math.ceil(sessions_left(weekly_pct, ratio) - 1e-9) * SESSION_H


def _wall(d, t):
    return datetime.combine(d, t, tzinfo=BERLIN)


def in_window(t, win):
    start, end = win
    lt = t.astimezone(BERLIN).time()
    return start <= lt < end if start <= end else (lt >= start or lt < end)


def latest_window_start(t, win):
    """The latest window start (Berlin wall clock, DST-aware) at or before t."""
    d = t.astimezone(BERLIN).date()
    s = _wall(d, win[0])
    return s if s <= t else _wall(d - timedelta(days=1), win[0])


def window_starts(a, b, win):
    """Window starts s with a < s < b."""
    out, d = [], a.astimezone(BERLIN).date() - timedelta(days=1)
    while True:
        s = _wall(d, win[0])
        if s >= b:
            return out
        if s > a:
            out.append(s)
        d += timedelta(days=1)


def next_window_start(t, win):
    s = latest_window_start(t, win)
    return s if s > t else _wall(s.astimezone(BERLIN).date() + timedelta(days=1), win[0])


def night_start(now, resets_at, win):
    """-> (t0, has_night): the start of the night `now` belongs to. Normally the latest window
    start; if that was in the previous week, the weekly reset (a night only if the reset falls
    inside a window, else the gap before the week's first window has no budget)."""
    ws = latest_window_start(now, win)
    week_start = resets_at - WEEK
    if ws >= week_start:
        return ws, True
    return week_start, in_window(week_start, win)


def _b(t):
    return t.astimezone(BERLIN).strftime("%a %d.%m. %H:%M")


# ------------------------------------------------------------------ decision

def decide_core(weekly_pct, resets_at, now, msu, env, P=None, session_pct=None, session_resets_at=None,
                activity_known=True, ratio=DEFAULT_RATIO, lm_setting="auto", win=None, anchor=None):
    """Pure decision. msu = minutes since the user was active (activity_known=False: unknown ->
    active). anchor = the weekly % at the night's start, or a function t0 -> that % (None: use
    the current %). -> dict(go, headroom, target, mode, postpone, recheck_at, reason, ...)."""
    P = {**DEFAULTS, **(P or {})}
    win = win or afclaude_config.window()
    d = {"go": False, "headroom": None, "target": None, "mode": None, "postpone": False, "recheck_at": None,
         "session_cap": P["session_cap"], "user_active": None, "ratio": ratio, "last_mile_h": None,
         "last_mile_start": None, "nights": None, "need": None, "share": None, "night_budget": None,
         "w0": None, "t0": None}
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
    s_live = (session_pct is not None and session_resets_at is not None and session_resets_at > now)

    if lm_h > 0 and now >= lm_start:
        head = 100.0 - w
        how = (f"ceil({sessions_left(w, ratio):.2f} session windows left) x {SESSION_H:g} h" if lm_setting == "auto"
               else "last_mile_hours")
        base = (f"last mile ({lm_h:g} h before the weekly reset {_b(resets_at)}, {how}): week {w:.0f}% used, "
                f"budget for this run +{head:.1f}% (up to 100%)")
        d.update(mode="last_mile", headroom=head, target=100.0, session_cap=LAST_MILE_SESSION_CAP)
        if P.get("last_mile_yield") and ((not activity_known) or (msu is not None and msu < P["idle_min"])):
            r = now + (RETRY if not activity_known else timedelta(minutes=P["idle_min"] - msu))
            return dict(d, postpone=True, recheck_at=r, user_active=True,
                        reason=f"HOLD: user active/unknown (last-mile yield); postponed to {_b(r)}; {base}")
        if s_live and float(session_pct) >= LAST_MILE_SESSION_CAP:
            return dict(d, postpone=True, recheck_at=session_resets_at,
                        reason=f"HOLD: session limit reached, recheck at its reset {_b(session_resets_at)}; {base}")
        return dict(d, go=True, reason=f"CONTINUE: {base}")

    t0, has_night = night_start(now, resets_at, win)
    if not has_night:
        nxt = next_window_start(now, win)
        return dict(d, mode="none", headroom=0.0, target=w, t0=t0, recheck_at=nxt,
                    reason=f"HOLD: no nightly window since the weekly reset yet (next {_b(nxt)}); "
                           f"budget for this run +0.0%")
    w0 = anchor(t0) if callable(anchor) else anchor
    w0 = w if w0 is None else min(float(w0), w)
    T0 = (resets_at - t0).total_seconds() / 3600
    need = min(100.0, P["safety"] * interp_env(env, T0))
    lm0 = resets_at - timedelta(hours=last_mile_hours(w0, ratio, lm_setting))
    N = 1 + len(window_starts(t0, lm0, win))
    share = (100.0 - w0 - need) / N
    left0 = max(100.0 - w0, 0.0)
    free = left0 - need
    floor = min(P["night_floor"], max(free, P["night_floor_min"]), left0)
    nb = min(max(share, floor), left0)
    target = w0 + nb
    head = max(min(target - w, 100.0 - w), 0.0)
    d.update(mode="night", headroom=head, target=target, need=need, nights=N, share=share, night_budget=nb,
             w0=w0, t0=t0)
    how = (f"share ({100 - w0:.0f}% left - {need:.1f}% user need for {T0:.0f} h) / {N} night(s) = {share:.1f}%"
           + (f", floor {nb:.1f}%" if nb > share + 1e-9 else ""))
    base = (f"week {w:.0f}% used; tonight's budget {nb:.1f}% from {w0:.0f}% at {_b(t0)} ({how}); "
            f"budget for this run +{head:.1f}% (up to {target:.1f}%)")
    if head <= min(P["min_gap"], nb / 2):
        nxt = next_window_start(now, win)
        return dict(d, recheck_at=nxt, reason=f"HOLD: tonight's budget is used up, next night {_b(nxt)}; {base}")
    active = (not activity_known) or (msu is not None and msu < P["idle_min"])   # idle from last + idle_min
    d["user_active"] = active
    if not activity_known:
        r = now + RETRY
        return dict(d, postpone=True, recheck_at=r,
                    reason=f"HOLD: user activity unknown (sampler data missing/stale), treated as active; "
                           f"postponed, recheck {_b(r)}; {base}")
    if active:
        r = now + timedelta(minutes=P["idle_min"] - msu)
        return dict(d, postpone=True, recheck_at=r,
                    reason=f"HOLD: user active {msu:.0f} min ago (yield); postponed to {_b(r)} "
                           f"(last activity + {P['idle_min']:g} min); {base}")
    if s_live and float(session_pct) >= P["session_cap"]:
        return dict(d, postpone=True, recheck_at=session_resets_at,
                    reason=f"HOLD: session guard, session {float(session_pct):.0f}% >= {P['session_cap']:.0f}%; "
                           f"postponed to the session reset {_b(session_resets_at)}; {base}")
    return dict(d, go=True, reason=f"CONTINUE: {base}")


def decide(usage, now, params=None, rows=None, msu=None, activity_known=None):
    """Decision from a keepalive usage dict ({'weekly': {'percent', 'resets_at'}, 'session': ...}).
    `params` = (P, env, source) from load_params(); `rows` = sampler rows (default: the tail of
    data/samples.jsonl); msu/activity_known default to what the rows say."""
    P, env, src = params or load_params()
    w = (usage or {}).get("weekly") or {}
    s = (usage or {}).get("session") or {}
    rows = _rows_or_empty(rows)
    if activity_known is None:
        msu = minutes_since_user(now, rows)
        activity_known = msu is not None
    ratio, rsrc = ratio_info(rows, now)
    resets_at = w.get("resets_at")
    d = decide_core(w.get("percent"), resets_at, now, msu, env, P, s.get("percent"), s.get("resets_at"),
                    activity_known, ratio, afclaude_config.last_mile_setting(), afclaude_config.window(),
                    anchor=lambda t0: weekly_at(rows, t0, resets_at, now))
    d["source"], d["ratio_source"] = src, rsrc
    if d["headroom"] is not None:
        d["reason"] += f" [budget model, {src}, ratio {ratio:.3g} ({rsrc})]"
    return d


def budget_text(d, weekly_pct=None):
    """The 'budget for this run' line for a continue message (the same number as the reason)."""
    if d.get("headroom") is None:
        return "budget unknown"
    where = ("last mile, up to 100%" if d.get("mode") == "last_mile"
             else f"tonight's share, up to {d['target']:.1f}%")
    now_txt = f"now {float(weekly_pct):.0f}%, " if weekly_pct is not None else ""
    return f"budget for this run: about +{d['headroom']:.1f} weekly % ({now_txt}{where})"


def budget_decision(usage, now):
    """Same interface as keepalive.budget_decision(): -> (go, reason)."""
    d = decide(usage, now)
    return d["go"], d["reason"]


def budget_headroom(usage, now):
    """-> (extra weekly % for this run | None, text), like keepalive.budget_headroom()."""
    d = decide(usage, now)
    return d["headroom"], budget_text(d, ((usage or {}).get("weekly") or {}).get("percent"))


if __name__ == "__main__":     # quick look: python3 budget.py
    import keepalive
    _now = datetime.now(UTC)
    _u = keepalive.read_usage_cache()
    _d = decide(_u, _now)
    print(load_params()[2], "| minutes since user:", minutes_since_user(_now), "| ratio:", ratio_info())
    print(_d["go"], _d["reason"])
    print(budget_text(_d, ((_u or {}).get("weekly") or {}).get("percent")))
