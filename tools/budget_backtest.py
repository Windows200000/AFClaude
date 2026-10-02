#!/usr/bin/env python3
"""Counterfactual replay of the weekly budget models over data/samples.jsonl (the method of
the usage review: the observed user demand, a simulated AFClaude with unlimited work).

    python3 tools/budget_backtest.py --reset 2026-10-01T17:00Z --user-until 2026-10-01T12:00Z
                                     [--raf 8] [--k 0.16] [--ratio 0.118] [--user-scale 1]
                                     [--late-burst 5] [--floor 2] [--trace MODEL]

Simulation (the 15-min sample grid of one weekly cycle):
  * USER demand per step = the observed weekly-% rise (and session-% rise) of every step up to
    --user-until (the first autonomous AFClaude run); 0 after it and after the last sample.
    --user-scale scales it; --late-burst H copies the user's worst H-hour burst into the last
    H hours before the reset (stress test for the last mile).
  * User ACTIVITY (for the yield) comes from the same rows: each model's own rule
    (reserve: usage_model.user_signal; budget: budget.user_signal); linear does not yield.
  * AFClaude, while allowed, burns RAF weekly-%/h and RAF/K session-%/h (unlimited work). It
    stops at its target (the run budget), at its session cap, at 100% weekly, or outside its
    allowed period (the nightly window, or the last mile).
  * Session windows are re-simulated (5 h from the first usage, shared by user and AFClaude).
  * blocked = user demand that did not fit under 100% weekly; blocked steps = steps with any.
  * user headroom = 100 - weekly at each step with user demand or activity (min and p10).
  * session hits by AF = user session demand that hit 100% in a window AFClaude spent in.
  * nights with work = nightly windows in which AFClaude spent > 0.1%.
Models: linear (keepalive's linear rule: projection < 90%, 11:00 cutoff, 5 h last mile, decided
at the window start / after a session reset in the window / every 15 min in the last mile),
reserve (usage_model.py, continuous), budget (budget.py, continuous; envelope variants
generic / blended / user from data/user_model.json).
"""
import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import afclaude_config  # noqa: E402
import budget  # noqa: E402
import usage_model  # noqa: E402

UTC = timezone.utc
STEP = timedelta(minutes=15)
SESSION = afclaude_config.SESSION_LENGTH
WEEK = timedelta(days=7)


def ts(s):
    return budget._ts(s)


def load_rows(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and ts(r.get("at")):
                rows.append(r)
    rows.sort(key=lambda r: ts(r["at"]))
    return rows


def wk(r):
    w = budget._weekly(r)
    return w.get("percent"), ts(w.get("resets_at"))


def sess(r):
    s = budget._dict(budget._dict(r.get("usage")).get("session"))
    return s.get("percent")


def build_steps(rows, reset, user_until, user_scale=1.0, late_burst_h=0.0):
    """-> (w0, t0, steps). Each step: t, dt (h), dw, ds (user demand), act_new, act_old."""
    cyc = budget._round_reset(reset)
    rs = [r for r in rows if wk(r)[0] is not None and budget._round_reset(wk(r)[1]) == cyc]
    steps = []
    for p, c in zip(rs, rs[1:]):
        t = ts(c["at"])
        if (t - ts(p["at"])) < timedelta(minutes=1):
            continue
        user = t <= user_until
        dw = max(wk(c)[0] - wk(p)[0], 0.0) if user else 0.0
        ds = max((sess(c) or 0) - (sess(p) or 0), 0.0) if user else 0.0
        steps.append(dict(t=t, dt=(t - ts(p["at"])).total_seconds() / 3600, dw=dw * user_scale, ds=ds * user_scale,
                          act_new=user and budget.user_signal(c, p),
                          act_old=user and usage_model.user_signal(c, p, [])))
    t = steps[-1]["t"]
    while t + STEP <= reset - timedelta(minutes=4):
        t += STEP
        steps.append(dict(t=t, dt=0.25, dw=0.0, ds=0.0, act_new=False, act_old=False))
    if late_burst_h:
        n = int(late_burst_h * 4)
        user_steps = [s for s in steps if s["t"] <= user_until]
        best = max(range(max(len(user_steps) - n, 1)), key=lambda i: sum(s["dw"] for s in user_steps[i:i + n]))
        burst = user_steps[best:best + n]
        tail = steps[-n:]
        for s, b in zip(tail, burst):
            s.update(dw=s["dw"] + b["dw"], ds=s["ds"] + b["ds"], act_new=True, act_old=True)
    return float(wk(rs[0])[0]) * user_scale, ts(rs[0]["at"]), steps


# ------------------------------------------------------------------ models

def window_end(t, win):
    s = budget.latest_window_start(t, win)
    end_d = s.astimezone(budget.BERLIN).date() + (timedelta(days=1) if win[1] <= win[0] else timedelta(0))
    return datetime.combine(end_d, win[1], tzinfo=budget.BERLIN)


def linear(ctx, t, w):
    reset = ctx["reset"]
    T = reset - t
    if w >= 100:
        return False, w, 95.0
    if timedelta(0) < T <= timedelta(hours=5):
        return True, 100.0, 100.0
    el = max(t - (reset - WEEK), timedelta(hours=24))
    proj = w + w * (T / el)
    if proj < 90:
        return True, 90 * el / (el + T), 95.0
    cutoff = datetime.combine(window_end(t, ctx["win"]).astimezone(budget.BERLIN).date(),
                              datetime.min.time().replace(hour=11), tzinfo=budget.BERLIN)
    return (reset <= cutoff), (100.0 if reset <= cutoff else w), 95.0


def reserve(ctx, t, w, s_pct, s_open):
    P, env = ctx["P"], ctx["env"]
    msu = None if ctx["last_act"] is None else (t - ctx["last_act"]).total_seconds() / 60
    if msu is None:
        msu = 10 ** 6
    d = usage_model.decide_core(w, ctx["reset"], t, msu, env, P, s_pct,
                                (s_open + SESSION) if s_open else None, True)
    cap = usage_model.session_cap(ctx["reset"], (s_open + SESSION) if s_open else t + SESSION, P, t)
    return d["go"], (d["target"] if d["target"] is not None else w), cap


def new_budget(ctx, t, w, s_pct, s_open):
    P, env = ctx["P"], ctx["env"]
    msu = None if ctx["last_act"] is None else (t - ctx["last_act"]).total_seconds() / 60
    if msu is None:
        msu = 10 ** 6
    d = budget.decide_core(w, ctx["reset"], t, msu, env, P, s_pct, (s_open + SESSION) if s_open else None,
                           True, ctx["ratio"], ctx.get("lm", "auto"), ctx["win"], anchor=ctx["anchor"])
    ctx["last_d"] = d
    return d["go"], (d["target"] if d["target"] is not None else w), d["session_cap"]


def run(model, steps, w0, reset, win, raf, k, P=None, env=None, ratio=0.16, lm="auto", trace=False):
    ctx = dict(reset=reset, win=win, P=P, env=env, ratio=ratio, lm=lm, last_act=None)
    w = w0
    hist = []                       # (t, w) of the simulated weekly %, for the night anchor

    def anchor(t0):
        best = None
        for t, x in hist:
            if t <= t0 + timedelta(minutes=1):
                best = x
            else:
                break
        return best if best is not None else (hist[0][1] if hist else w)
    ctx["anchor"] = anchor
    s_open, s_val, af_in_win = None, 0.0, False
    running, target, cap = False, w, 85.0
    out = dict(af=0.0, blocked=0.0, blocked_steps=0, heads=[], sess_hits_af=0, sess_hits_self=0,
               nights=set(), night_spend={}, min_sess_head_af=100.0)
    for st in steps:
        t, dt = st["t"], st["dt"]
        if s_open is not None and t >= s_open + SESSION:
            s_open, s_val, af_in_win = None, 0.0, False
        hist.append((t, w))
        allowed_win = budget.in_window(t, win)
        if model == "linear":
            in_lm = timedelta(0) < reset - t <= timedelta(hours=5)
            lt = t.astimezone(budget.BERLIN)
            start_tick = allowed_win and lt.hour == win[0].hour and lt.minute < 15
            decide_now = start_tick or (allowed_win and s_open is None and not running) or in_lm
            ok = allowed_win or in_lm
            if decide_now and ok:
                running, target, cap = linear(ctx, t, w)
        else:
            if model == "reserve":
                ok = allowed_win or (reset - t) <= timedelta(hours=5)
                go, target, cap = reserve(ctx, t, w, s_val, s_open)
                ctx["last_act_kind"] = "act_old"
            else:
                go, target, cap = new_budget(ctx, t, w, s_val, s_open)
                ok = allowed_win or ctx["last_d"]["mode"] == "last_mile"
            running = go and ok
        if not ok:
            running = False
        af = 0.0
        if running and w < min(target, 100):
            af = min(raf * dt, target - w, 100 - w)
            af = max(min(af, max(cap - s_val, 0.0) * k), 0.0)
            if af > 0:
                if s_open is None:
                    s_open = t
                s_val += af / k
                af_in_win = True
                w += af
                out["af"] += af
                night = budget.latest_window_start(t, win).date().isoformat()
                out["night_spend"][night] = out["night_spend"].get(night, 0.0) + af
                if trace:
                    print("   ", t.astimezone(budget.BERLIN).strftime("%a %H:%M"),
                          f"AF +{af:.2f} w={w:.1f} s={s_val:.0f} target={target:.1f} cap={cap:.0f}")
        if st["dw"] > 0 or st["ds"] > 0:
            if s_open is None:
                s_open = t
            if st["ds"] > 0 and s_val + st["ds"] >= 100 - 1e-9:
                if af_in_win:
                    out["sess_hits_af"] += 1
                else:
                    out["sess_hits_self"] += 1
            s_val = min(s_val + st["ds"], 100.0)
            if af_in_win:
                out["min_sess_head_af"] = min(out["min_sess_head_af"], 100.0 - s_val)
            fit = min(st["dw"], max(100 - w, 0.0))
            if fit < st["dw"] - 1e-9:
                out["blocked"] += st["dw"] - fit
                out["blocked_steps"] += 1
            w += fit
        if st["act_new"] if model == "budget" else st["act_old"]:
            ctx["last_act"] = t
        if st["act_new"] or st["act_old"] or st["dw"] > 0 or st["ds"] > 0:
            out["heads"].append(100 - w)        # the same user moments for every model
    out["final"] = w
    out["nights"] = {n for n, v in out["night_spend"].items() if v > 0.1}
    return out


def nights_in(steps, win, reset):
    first = steps[0]["t"]
    return sorted({budget.latest_window_start(s["t"], win).date().isoformat() for s in steps
                   if budget.in_window(s["t"], win) and s["t"] >= first})


def p10(xs):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[max(int(len(xs) * 0.1) - 1, 0)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default=os.path.join(HERE, "data", "samples.jsonl"))
    ap.add_argument("--user-model", default=os.path.join(HERE, "data", "user_model.json"))
    ap.add_argument("--reset", required=True, help="the weekly reset (ISO) of the cycle to replay")
    ap.add_argument("--user-until", required=True, help="observed demand counts as the user's up to here (ISO)")
    ap.add_argument("--raf", type=float, default=8.0, help="AFClaude burn rate, weekly %%/h")
    ap.add_argument("--k", type=float, default=0.16, help="simulated weekly %% per session %%")
    ap.add_argument("--ratio", type=float, default=None, help="ratio the budget model uses (default: limit_ratio)")
    ap.add_argument("--user-scale", type=float, default=1.0)
    ap.add_argument("--late-burst", type=float, default=0.0)
    ap.add_argument("--floor", type=float, default=None, help="night_floor for the budget model")
    ap.add_argument("--safety", type=float, default=None)
    ap.add_argument("--floor-min", type=float, default=None)
    ap.add_argument("--lm-yield", action="store_true")
    ap.add_argument("--trace", default=None)
    a = ap.parse_args()
    reset, until = ts(a.reset), ts(a.user_until)
    rows = load_rows(a.samples)
    w0, t0, steps = build_steps(rows, reset, until, a.user_scale, a.late_burst)
    win = afclaude_config.window()
    ratio = a.ratio if a.ratio is not None else budget.ratio_info([r for r in rows if ts(r["at"]) <= reset])[0]
    try:
        with open(a.user_model) as fh:
            user_env, weeks, _ = budget.parse_user_model(json.load(fh))
    except (OSError, ValueError):
        user_env, weeks = None, 0
    P = dict(budget.DEFAULTS)
    if a.floor is not None:
        P["night_floor"] = a.floor
    if a.safety is not None:
        P["safety"] = a.safety
    if a.floor_min is not None:
        P["night_floor_min"] = a.floor_min
    if a.lm_yield:
        P["last_mile_yield"] = True
    envs = [("generic", dict(budget.GENERIC_ENVELOPE))]
    if user_env:
        envs += [("blended", budget.floored(budget.effective_envelope(user_env, 2)[0])),
                 ("user", budget.floored(budget.effective_envelope(user_env, 4)[0]))]
    nights = nights_in(steps, win, reset)
    print(f"cycle reset {reset:%Y-%m-%d %H:%M}Z, replay from {t0:%a %d.%m. %H:%M}Z at {w0:.0f}%, "
          f"user demand {sum(s['dw'] for s in steps):.1f}%, {len(nights)} observed nights; "
          f"RAF {a.raf:g} %/h, k {a.k:g}, budget ratio {ratio:.3g}, floor {P['night_floor']:g}, "
          f"safety {P['safety']:g}, user x{a.user_scale:g}, late burst {a.late_burst:g} h")
    print(f"{'model':28s} {'final':>6s} {'AF+':>6s} {'minH':>5s} {'p10H':>5s} {'blk%':>5s} {'blkN':>4s} "
          f"{'sessAF':>6s} {'nights':>7s}  per-night AF spend")
    cases = [("linear", "linear", None, None)]
    for name, env in envs:
        cases.append((f"reserve ({name})", "reserve", dict(usage_model.DEFAULTS), env))
    for name, env in envs:
        cases.append((f"budget ({name})", "budget", P, env))
    for label, model, PP, env in cases:
        o = run(model, steps, w0, reset, win, a.raf, a.k, PP, env, ratio, trace=(a.trace == label))
        per = " ".join(f"{o['night_spend'].get(n, 0):.1f}" for n in nights)
        print(f"{label:28s} {o['final']:6.1f} {o['af']:6.1f} {min(o['heads'] or [100]):5.1f} {p10(o['heads']):5.1f} "
              f"{o['blocked']:5.1f} {o['blocked_steps']:4d} {o['sess_hits_af']:6d} "
              f"{len(o['nights'] & set(nights)):3d}/{len(nights):<3d}  {per}")


if __name__ == "__main__":
    main()
