"""Weekly budget model "reserve envelope + yield + session guard" (stdlib only).

Idea: instead of forecasting the user's mean usage, keep a reserve that bounds what the
user could still need before the weekly reset, and let AFClaude spend only above it.

    T          = hours until the weekly reset
    env(h)     = the user's demand envelope: max weekly-% rise within any h hours
                 (piecewise-linear through (0, 0) and the table points, flat after the last)
    reserve(T) = min(100, safety * env(T - grace))
                 user active -> reserve >= safety * env(min(T, 5 h))
    target     = 100 - reserve(T)
    CONTINUE  iff  the user is idle  and  target - weekly% > min_gap
    budget for this run = target - weekly%   (re-evaluated at every decision)

Session guard: AFClaude fills a session window to at most `session_cap` (85%); a window that
ends at/before the weekly reset, within the last `glide_h` (5 h) before it, may be filled to
`glide_session_cap` (100%).

Fallbacks: weekly usage unknown -> HOLD. No / invalid data/user_model.json, or fewer than
1 closed week -> generic envelope; 1 to <4 weeks -> pointwise max(user, 0.5 * generic);
>= 4 weeks -> the user's envelope. Sampler data missing or stale -> the user counts as
active (HOLD).

User activity comes from the latest data/samples.jsonl rows (usage_sampler.py): an interval
counts as user activity if it has a human prompt (minus the prompts AFClaude itself typed in
at a recorded keepalive/dispatcher fire), turns in non-AFClaude sessions, or a weekly rise
with no local turns at all (another device / claude.ai).
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data"))
USER_MODEL_FILE = os.path.join(DATA_DIR, "user_model.json")
SAMPLES_FILE = os.path.join(DATA_DIR, "samples.jsonl")
# where AFClaude's own fires are recorded (their typed-in prompts are not user activity)
FIRE_STATE_FILES = [
    os.path.join(os.environ.get("KEEPALIVE_STATE_DIR", HERE), "keepalive_state.json"),
    os.path.join(DATA_DIR, "keepalive", "keepalive_state.json"),
    os.path.join(DATA_DIR, "dispatcher_state.json"),
]

SCHEMA = "afclaude.user_model/1"
# Generic demand envelope (hours -> weekly %): about one session window per 5 h for the
# first day, then conservative up to the whole week.
GENERIC_ENVELOPE = {1: 12.0, 3: 16.0, 5: 16.0, 10: 32.0, 24: 50.0, 48: 70.0, 96: 90.0, 168: 100.0}
DEFAULTS = {
    "safety": 1.25,             # multiplier on the envelope
    "grace_min": 0.0,           # the user may at worst wait this long before the reset
    "idle_min": 60.0,           # user counts as active this long after the last activity
    "min_gap": 1.0,             # CONTINUE only if target - weekly > this (weekly %)
    "session_cap": 85.0,        # session % AFClaude may fill a window to
    "glide_session_cap": 100.0,  # ... if that window ends at/before the weekly reset
    "glide_h": 5.0,             # ... and the reset is at most this far away (one session window)
    "active_floor_h": 5.0,      # active user: reserve covers at least this many hours
    "blend_weeks": 4.0,         # closed weeks before the user's envelope is used alone
}
RESERVE_FLOOR_FRAC = 0.5                 # envelope floor: half the generic envelope, also with >= 4 weeks
GLIDE_TOLERANCE = timedelta(minutes=1)   # resets_at jitters by up to a second between fetches
SAMPLE_MAX_AGE = timedelta(minutes=30)   # older latest sample = sampler down -> user active
TAIL_BYTES = 512 * 1024                  # only the end of samples.jsonl is read
FIRE_SLACK = timedelta(minutes=2)


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
    """-> (envelope, source). < 1 closed week: generic; 1 to < blend_weeks: pointwise
    max(user, 0.5 * generic) on the union of both grids; more: the user's envelope."""
    if not user_env or weeks is None or weeks < 1:
        return dict(GENERIC_ENVELOPE), "generic"
    if weeks < blend_weeks:
        keys = sorted({float(k) for k in user_env} | set(GENERIC_ENVELOPE))
        return ({k: max(interp_env(user_env, k), 0.5 * interp_env(GENERIC_ENVELOPE, k)) for k in keys},
                "blended")
    return {float(k): float(v) for k, v in user_env.items()}, "user"


def floored(env):
    """Never trust an envelope below RESERVE_FLOOR_FRAC x the generic one (any horizon)."""
    keys = sorted(set(env) | set(GENERIC_ENVELOPE))
    return {k: max(interp_env(env, k), RESERVE_FLOOR_FRAC * interp_env(GENERIC_ENVELOPE, k)) for k in keys}


def reserve(T_h, user_active, env, P):
    """Weekly % kept for the user for the remaining T_h hours."""
    T_h = max(T_h, 0.0)
    T = max(T_h - P["grace_min"] / 60.0, 0.0)
    res = P["safety"] * interp_env(env, T)
    if user_active:   # spec: the floor is env(min(T, 5 h)) without the grace
        res = max(res, P["safety"] * interp_env(env, min(T_h, P["active_floor_h"])))
    return min(res, 100.0)


def session_cap(resets_at, session_resets_at, P, now=None):
    """Session % AFClaude may fill the current window to: glide_session_cap for a window
    that ends at/before the weekly reset while the reset is at most glide_h away (the
    window cannot leak into the new week or into the user's next day), else session_cap."""
    if session_resets_at is not None and resets_at is not None \
            and session_resets_at <= resets_at + GLIDE_TOLERANCE \
            and (now is None or resets_at - now <= timedelta(hours=P["glide_h"])):
        return P["glide_session_cap"]
    return P["session_cap"]


# ------------------------------------------------------------------ user_model.json

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
                             ("grace_min", rec.get("grace_min"), 0, 600),
                             ("idle_min", dec.get("idle_min"), 0, 1440),
                             ("min_gap", dec.get("min_gap"), 0, 50),
                             ("session_cap", dec.get("session_cap_pct"), 1, 100)):
        v = d.get(key, alt)
        if v is not None:
            over[key] = _num(v, lo, hi)
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


# ------------------------------------------------------------------ user activity

def _ts(s):
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        t = datetime.fromisoformat(str(s).replace(" ", "T").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def tail_rows(path=None, max_bytes=TAIL_BYTES):
    path = path or SAMPLES_FILE
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(size - max_bytes, 0))
        chunk = fh.read()
    lines = chunk.split(b"\n")
    if size > max_bytes:
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
    return rows


def fire_times(paths=None):
    """Timestamps at which keepalive / the dispatcher typed a message into a session."""
    out = []
    for p in (FIRE_STATE_FILES if paths is None else paths):
        try:
            with open(p) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict):
            continue
        for key, field in (("handled", "at"), ("sessions", "sent_at")):
            group = st.get(key)
            if not isinstance(group, dict):
                continue
            for h in group.values():
                if isinstance(h, dict) and h.get(field) and h.get("result") not in ("dry-run", "preflight-failed"):
                    out.append(_ts(h[field]))
    return sorted(t for t in out if t)


def _weekly_pct(r):
    return _dict(_dict(_dict(r).get("usage")).get("weekly")).get("percent")


def _count(d, key):
    """Non-negative count from a sampler field; anything malformed counts as activity
    (1), so bad data never hides the user."""
    v = d.get(key) if isinstance(d, dict) else None
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 1
    return max(int(v), 0)


def _dict(x):
    return x if isinstance(x, dict) else {}


def user_signal(row, prev, fires):
    """True if this sample interval shows the user (not AFClaude) using Claude."""
    a = _dict(row.get("activity"))
    own, oth = _dict(a.get("own")), _dict(a.get("other"))
    if _count(oth, "human_prompts") or _count(oth, "assistant_turns") or _count(oth, "subagent_turns"):
        return True
    hp = _count(own, "human_prompts")
    if hp:
        end = _ts(row["at"])
        start = _ts(row.get("since")) or (_ts(prev["at"]) if prev else end - timedelta(minutes=15))
        injected = sum(1 for f in fires if start - FIRE_SLACK < f <= end)
        if hp > injected:
            return True
    w, pw = _weekly_pct(row), (_weekly_pct(prev) if prev else None)
    local_turns = _count(own, "assistant_turns") + _count(own, "subagent_turns")
    if isinstance(w, (int, float)) and isinstance(pw, (int, float)) and w > pw and not local_turns:
        return True              # rise with no local turns at all: another device / claude.ai
    return False


def minutes_since_user(now, rows=None, fires=None):
    """-> minutes since the last user activity, or None if unknown (no / stale / unreadable
    samples: the caller treats the user as active). Without any activity in the rows read:
    the time since the oldest of them."""
    try:
        return _minutes_since_user(now, rows, fires)
    except Exception:   # noqa: BLE001 - unknown activity = active user (HOLD)
        return None


def _minutes_since_user(now, rows, fires):
    rows = tail_rows() if rows is None else rows
    rows = [r for r in rows if isinstance(r, dict) and _ts(r.get("at"))]
    rows = [r for r in rows if _ts(r["at"]) <= now + timedelta(minutes=1)]
    if not rows or now - _ts(rows[-1]["at"]) > SAMPLE_MAX_AGE:
        return None
    fires = fire_times() if fires is None else fires
    for i in range(len(rows) - 1, -1, -1):
        try:
            hit = user_signal(rows[i], rows[i - 1] if i else None, fires)
        except Exception:   # noqa: BLE001 - a malformed row counts as user activity
            hit = True
        if hit:
            return max((now - _ts(rows[i]["at"])).total_seconds() / 60, 0.0)
    return (now - _ts(rows[0]["at"])).total_seconds() / 60


# ------------------------------------------------------------------ decision

def decide_core(weekly_pct, resets_at, now, msu, env, P=None, session_pct=None, session_resets_at=None,
                activity_known=True):
    """Pure decision. msu = minutes since the user was active (None + activity_known=False:
    unknown -> active). -> dict(go, target, reserve, headroom, T_h, user_active, reason)."""
    P = {**DEFAULTS, **(P or {})}
    d = {"go": False, "target": None, "reserve": None, "headroom": None, "T_h": None, "user_active": None}
    if weekly_pct is None or resets_at is None:
        return dict(d, reason="HOLD: weekly usage unknown (fail-safe)")
    weekly_pct = float(weekly_pct)
    if resets_at <= now:
        return dict(d, reason="HOLD: weekly reset time already passed (stale usage data)")
    T = (resets_at - now).total_seconds() / 3600
    active = (not activity_known) or (msu is not None and msu <= P["idle_min"])
    res = reserve(T, active, env, P)
    target = 100.0 - res
    head = max(target - weekly_pct, 0.0)
    d.update(target=target, reserve=res, headroom=head, T_h=T, user_active=active)
    if weekly_pct >= 100:
        return dict(d, headroom=0.0, reason="HOLD: weekly limit exhausted")
    base = (f"week {weekly_pct:.0f}% used, reserve {res:.1f}% kept for the user for the {T:.1f}h until the "
            f"weekly reset, target {target:.1f}%, headroom {target - weekly_pct:+.1f}%")
    if active:
        who = ("user activity unknown (sampler data missing/stale), treated as active" if not activity_known
               else f"user active {msu:.0f} min ago")
        return dict(d, reason=f"HOLD: {who} (yield); {base}")
    if session_pct is not None and session_resets_at is not None and session_resets_at > now:
        cap = session_cap(resets_at, session_resets_at, P, now)
        if float(session_pct) >= cap:
            return dict(d, reason=f"HOLD: session guard, session {float(session_pct):.0f}% >= {cap:.0f}%; {base}")
    if target - weekly_pct > P["min_gap"]:
        return dict(d, go=True, reason=f"CONTINUE: {base}")
    return dict(d, reason=f"HOLD: {base} (needs > {P['min_gap']:g}%)")


def decide(usage, now, params=None, msu=None, activity_known=None):
    """Decision from a keepalive usage dict ({'weekly': {'percent', 'resets_at'}, 'session': ...}).
    `params` = (P, env, source) from load_params(); msu/activity_known default to the sampler data."""
    P, env, src = params or load_params()
    w = (usage or {}).get("weekly") or {}
    s = (usage or {}).get("session") or {}
    if activity_known is None:
        msu = minutes_since_user(now)
        activity_known = msu is not None
    d = decide_core(w.get("percent"), w.get("resets_at"), now, msu, env, P,
                    s.get("percent"), s.get("resets_at"), activity_known)
    d["source"] = src
    if d["target"] is not None:
        d["reason"] += f" [reserve model, {src}, safety {P['safety']:g}]"
    return d


def budget_decision(usage, now):
    """Same interface as keepalive.budget_decision(): -> (go, reason)."""
    d = decide(usage, now)
    return d["go"], d["reason"]


def budget_headroom(usage, now):
    """-> (extra weekly % for this run | None, text), like keepalive.budget_headroom()."""
    d = decide(usage, now)
    if d["target"] is None:
        return None, "budget unknown"
    pct = float(usage["weekly"]["percent"])
    return d["headroom"], (f"budget for this run: about +{d['headroom']:.1f} weekly % (now {pct:.0f}%) before "
                           f"reaching the target {d['target']:.1f}% (reserve {d['reserve']:.1f}% kept for the "
                           f"user for the {d['T_h']:.1f}h until the weekly reset)")


if __name__ == "__main__":     # quick look: python3 usage_model.py
    import keepalive
    now = datetime.now(UTC)
    u = keepalive.read_usage_cache()
    print(load_params()[2], "| minutes since user:", minutes_since_user(now))
    print(budget_decision(u, now))
    print(budget_headroom(u, now)[1])
