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
    (reserve: usage_model.user_signal; pacing: pacing.user_signal); linear does not yield.
  * AFClaude, while allowed, burns RAF weekly-%/h and RAF/K session-%/h (unlimited work). It
    stops at its target (the run budget), at its session cap, at 100% weekly, or outside its
    allowed period (the nightly window, or the last mile).
  * Session windows are re-simulated (5 h from the first usage, shared by user and AFClaude).
  * blocked = user demand that did not fit under 100% weekly; blocked steps = steps with any.
  * user headroom = 100 - weekly at each step with user demand or activity (min and p10).
  * session hits by AF = user session demand that hit 100% in a window AFClaude spent in.
  * nights with work = nightly windows in which AFClaude spent > 0.1%.
Models: linear (the original rule: projection < 90%, 11:00 cutoff, 5 h last mile, decided at
the window start / after a session reset in the window / every 15 min in the last mile),
reserve (the retired usage_model.py, continuous), pacing (pacing.py, continuous: the straight
line, and with the forecast fitted on the replayed cycle itself, i.e. IN-SAMPLE).
fcErr = mean / mean absolute error of the night-start forecasts of the user's remaining use.
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
import pacing  # noqa: E402
import usage_model  # noqa: E402

UTC = timezone.utc
STEP = timedelta(minutes=15)
SESSION = afclaude_config.SESSION_LENGTH
WEEK = timedelta(days=7)


def ts(s):
    return pacing._ts(s)


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
    w = pacing._weekly(r)
    return w.get("percent"), ts(w.get("resets_at"))


def sess(r):
    s = pacing._dict(pacing._dict(r.get("usage")).get("session"))
    return s.get("percent")


def build_steps(rows, reset, user_until, user_scale=1.0, late_burst_h=0.0):
    """-> (w0, t0, steps). Each step: t, dt (h), dw, ds (user demand), act_new, act_old."""
    cyc = pacing._round_reset(reset)
    rs = [r for r in rows if wk(r)[0] is not None and pacing._round_reset(wk(r)[1]) == cyc]
    steps = []
    for p, c in zip(rs, rs[1:]):
        t = ts(c["at"])
        if (t - ts(p["at"])) < timedelta(minutes=1):
            continue
        user = t <= user_until
        dw = max(wk(c)[0] - wk(p)[0], 0.0) if user else 0.0
        ds = max((sess(c) or 0) - (sess(p) or 0), 0.0) if user else 0.0
        steps.append(dict(t=t, dt=(t - ts(p["at"])).total_seconds() / 3600, dw=dw * user_scale, ds=ds * user_scale,
                          act_new=user and pacing.user_signal(c, p),
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
    return pacing.window_end(pacing.latest_window_start(t, win), win)


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
    cutoff = datetime.combine(window_end(t, ctx["win"]).astimezone(pacing.BERLIN).date(),
                              datetime.min.time().replace(hour=11), tzinfo=pacing.BERLIN)
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


def new_pacing(ctx, t, w, s_pct, s_open):
    msu = None if ctx["last_act"] is None else (t - ctx["last_act"]).total_seconds() / 60
    if msu is None:
        msu = 10 ** 6
    d = pacing.decide_core(w, ctx["reset"], t, msu, ctx["P"], s_pct, (s_open + SESSION) if s_open else None,
                           True, ctx["ratio"], ctx.get("lm", "auto"), ctx["win"], ctx["week_target"],
                           ctx["forecast"], anchor=ctx["anchor"])
    ctx["last_d"] = d
    if d.get("forecast") is not None and d.get("t0") is not None:
        ctx["forecasts"].setdefault(d["t0"], d["forecast"])
    return d["go"], (d["target"] if d["target"] is not None else w), d["session_cap"]


def run(model, steps, w0, reset, win, raf, k, P=None, env=None, ratio=0.16, lm="auto", trace=False,
        forecast=None, week_target=90.0):
    ctx = dict(reset=reset, win=win, P=P, env=env, ratio=ratio, lm=lm, last_act=None, forecast=forecast,
               week_target=week_target, forecasts={})
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
        allowed_win = pacing.in_window(t, win)
        if model == "linear":
            in_lm = timedelta(0) < reset - t <= timedelta(hours=5)
            lt = t.astimezone(pacing.BERLIN)
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
                go, target, cap = new_pacing(ctx, t, w, s_val, s_open)
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
                night = pacing.latest_window_start(t, win).date().isoformat()
                out["night_spend"][night] = out["night_spend"].get(night, 0.0) + af
                if trace:
                    print("   ", t.astimezone(pacing.BERLIN).strftime("%a %H:%M"),
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
        if st["act_new"] if model == "pacing" else st["act_old"]:
            ctx["last_act"] = t
        if st["act_new"] or st["act_old"] or st["dw"] > 0 or st["ds"] > 0:
            out["heads"].append(100 - w)        # the same user moments for every model
    out["final"] = w
    out["nights"] = {n for n, v in out["night_spend"].items() if v > 0.1}
    errs = [fc - sum(x["dw"] for x in steps if x["t"] > t0) for t0, fc in ctx["forecasts"].items()]
    out["fc_err"] = (sum(errs) / len(errs), sum(abs(e) for e in errs) / len(errs)) if errs else None
    return out


def nights_in(steps, win, reset):
    first = steps[0]["t"]
    return sorted({pacing.latest_window_start(s["t"], win).date().isoformat() for s in steps
                   if pacing.in_window(s["t"], win) and s["t"] >= first})


def p10(xs):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[max(int(len(xs) * 0.1) - 1, 0)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default=os.path.join(HERE, "data", "samples.jsonl"))
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="for the recorded fires")
    ap.add_argument("--user-model", default=os.path.join(HERE, "data", "user_model.json"),
                    help="the reserve model's envelope (comparison only)")
    ap.add_argument("--reset", required=True, help="the weekly reset (ISO) of the cycle to replay")
    ap.add_argument("--user-until", required=True, help="observed demand counts as the user's up to here (ISO)")
    ap.add_argument("--raf", type=float, default=8.0, help="AFClaude burn rate, weekly %%/h")
    ap.add_argument("--k", type=float, default=0.16, help="simulated weekly %% per session %%")
    ap.add_argument("--ratio", type=float, default=None, help="ratio pacing uses (default: per session window)")
    ap.add_argument("--user-scale", type=float, default=1.0)
    ap.add_argument("--late-burst", type=float, default=0.0)
    ap.add_argument("--week-target", type=float, default=90.0)
    ap.add_argument("--margin", type=float, default=None, help="forecast_margin")
    ap.add_argument("--no-lm-yield", action="store_true")
    ap.add_argument("--trace", default=None)
    a = ap.parse_args()
    reset, until = ts(a.reset), ts(a.user_until)
    rows = load_rows(a.samples)
    w0, t0, steps = build_steps(rows, reset, until, a.user_scale, a.late_burst)
    win = afclaude_config.window()
    upto = [r for r in rows if ts(r["at"]) <= reset + timedelta(minutes=10)]
    ratio = a.ratio if a.ratio is not None else pacing.ratio_info(upto)[0]
    P = dict(pacing.DEFAULTS)
    if a.margin is not None:
        P["forecast_margin"] = a.margin
    if a.no_lm_yield:
        P["last_mile_yield"] = False
    pacing.FIRE_FILES = [(os.path.join(a.data, "keepalive", "keepalive_state.json"), "handled", "at"),
                         (os.path.join(a.data, "dispatcher_state.json"), "sessions", "sent_at"),
                         (os.path.join(a.data, "usage_review_state.json"), "runs", "at")]
    rates, fsrc = pacing.fit_profile(upto, pacing.fire_times(), reset + timedelta(minutes=10))   # in-sample
    if rates:
        rates = [x * a.user_scale for x in rates]
    fc = (lambda x, y: pacing.forecast_user(rates, x, y, P["forecast_margin"])) if rates else None
    try:
        with open(a.user_model) as fh:
            user_env, weeks, _ = usage_model.parse_user_model(json.load(fh))
        user_env = {h: v * a.user_scale for h, v in user_env.items()}
    except (OSError, ValueError):
        user_env = None
    nights = nights_in(steps, win, reset)
    print(f"cycle reset {reset:%Y-%m-%d %H:%M}Z, replay from {t0:%a %d.%m. %H:%M}Z at {w0:.0f}%, "
          f"user demand {sum(s['dw'] for s in steps):.1f}%, {len(nights)} observed nights; "
          f"RAF {a.raf:g} %/h, k {a.k:g}, ratio {ratio:.3g}, week_target {a.week_target:g}, "
          f"margin {P['forecast_margin']:g}, last_mile_yield {P['last_mile_yield']}, user x{a.user_scale:g}, "
          f"late burst {a.late_burst:g} h; {fsrc}")
    print(f"{'model':28s} {'final':>6s} {'AF+':>6s} {'minH':>5s} {'p10H':>5s} {'blk%':>5s} {'blkN':>4s} "
          f"{'sessAF':>6s} {'nights':>7s} {'fcErr':>11s}  per-night AF spend")
    cases = [("linear (original)", "linear", None, None, None)]
    cases.append(("reserve (generic)", "reserve", dict(usage_model.DEFAULTS), dict(usage_model.GENERIC_ENVELOPE), None))
    if user_env:
        cases.append(("reserve (user env)", "reserve", dict(usage_model.DEFAULTS),
                      usage_model.floored(usage_model.effective_envelope(user_env, 2)[0]), None))
    cases.append(("pacing, straight line", "pacing", P, None, None))
    if fc:
        cases.append(("pacing, forecast", "pacing", P, None, fc))
    for label, model, PP, env, f in cases:
        o = run(model, steps, w0, reset, win, a.raf, a.k, PP, env, ratio, trace=(a.trace == label),
                forecast=f, week_target=a.week_target)
        per = " ".join(f"{o['night_spend'].get(n, 0):.1f}" for n in nights)
        fe = f"{o['fc_err'][0]:+.1f}/{o['fc_err'][1]:.1f}" if o.get("fc_err") else "-"
        print(f"{label:28s} {o['final']:6.1f} {o['af']:6.1f} {min(o['heads'] or [100]):5.1f} {p10(o['heads']):5.1f} "
              f"{o['blocked']:5.1f} {o['blocked_steps']:4d} {o['sess_hits_af']:6d} "
              f"{len(o['nights'] & set(nights)):3d}/{len(nights):<3d} {fe:>11s}  {per}")


if __name__ == "__main__":
    main()
