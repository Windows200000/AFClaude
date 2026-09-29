#!/usr/bin/env python3
"""How fast a session limit converts into weekly limit, and who's spending it.

Reads data/samples.jsonl (usage_sampler.py, every 15 min) and, between
consecutive samples that fall in the same 5-hour session window AND the same
weekly cycle (both resets_at unchanged, mod clock jitter), computes:

  - delta session% / delta weekly% for that interval;
  - a robust ratio "weekly % spent per 1 session %" (median over all usable
    pairs, plus a trimmed mean over the last N days), from which:
      windows_per_week           = 100 / (100 * ratio)   -- full session
                                    windows that fit in one weekly budget
      windows_left_this_week     = (100 - weekly_pct_now) / (100 * ratio)
  - an attribution of that ratio's weekly % between AFClaude ("own":
    autonomous/keep-alive sessions) and the user ("other"), using
    output(+thinking)- and cache-weighted token deltas already split by
    usage_sampler's activity.own/activity.other. Intervals where only one
    side was active are a clean measurement of that side's %-per-token rate;
    those rates are applied to every interval in the current weekly cycle to
    estimate each side's share of this week's weekly % so far.

`resets_at` jitters by a few hundred ms between fetches even when the
underlying reset hasn't moved (see usage_sampler.track_cycle), so windows are
compared after rounding to the minute the same way track_cycle does: add 30s,
then floor -- a reset that's genuinely NNN:59:5x/NNN+1:00:0x rounds to the
same minute either way.

Sparse data is reported honestly: any estimate backed by fewer than
MIN_PAIRS usable pairs comes back as status "insufficient_data" rather than
a number.
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SAMPLES = os.path.join(DATA, "samples.jsonl")

UTC = timezone.utc
MIN_PAIRS = 10            # below this, say "insufficient data" instead of a number
DEFAULT_TRIMMED_DAYS = 14
TRIM_FRAC = 0.1            # drop this fraction of ratios off each end for the trimmed mean

# Rough per-token weighting for "how much of a limit % this interval cost":
# output + thinking tokens are the dominant cost; cache writes and cache
# reads are cheaper (roughly Anthropic's own cost ratios) but not free.
OUT_W = 1.0
THINK_W = 1.0
CACHE_WRITE_W = 0.25
CACHE_READ_W = 0.02


def _parse_ts(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _round_reset(s):
    """Same convention as usage_sampler.track_cycle: absorb ms-level jitter
    around a reset boundary by adding 30s before flooring to the minute."""
    dt = _parse_ts(s)
    if dt is None:
        return None
    return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)


def weighted_tokens(tok_by_model):
    """output(+thinking)- and cache-weighted token count for one side of one
    interval's activity.{own,other}.tokens (usage_sampler's per-model dict)."""
    total = 0.0
    for _model, tk in (tok_by_model or {}).items():
        total += OUT_W * (tk.get("out") or 0)
        total += THINK_W * (tk.get("think") or 0)
        total += CACHE_WRITE_W * (tk.get("cache_w") or 0)
        total += CACHE_READ_W * (tk.get("cache_r") or 0)
    return total


def median(xs):
    return statistics.median(xs) if xs else None


def trimmed_mean(xs, frac=TRIM_FRAC):
    if not xs:
        return None
    xs = sorted(xs)
    k = int(len(xs) * frac)
    core = xs[k:len(xs) - k] if len(xs) - 2 * k > 0 else xs
    return sum(core) / len(core)


# ------------------------------------------------------------------ loading

def load_samples(path=SAMPLES):
    """Parsed rows from samples.jsonl, oldest first. Skips unreadable lines
    (a row still being written by a concurrent cron run)."""
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


# ------------------------------------------------------------------ pairs

def build_pairs(rows):
    """Consecutive-sample deltas within the same session window and weekly
    cycle. One dict per usable pair:
      t0, t1 (datetime), d_session, d_weekly (float, percentage points),
      own_tokens, other_tokens (weighted), weekly_resets (rounded datetime,
      i.e. which weekly cycle this pair belongs to)."""
    pairs = []
    prev = None
    for r in rows:
        u = r.get("usage") or {}
        s = u.get("session") or {}
        w = u.get("weekly") or {}
        t = _parse_ts(r.get("at"))
        if not (t and s.get("resets_at") and w.get("resets_at") and s.get("percent") is not None
                and w.get("percent") is not None):
            prev = None
            continue
        cur = {
            "t": t, "sp": s["percent"], "wp": w["percent"],
            "sr": _round_reset(s["resets_at"]), "wr": _round_reset(w["resets_at"]),
        }
        if prev and prev["sr"] == cur["sr"] and prev["wr"] == cur["wr"]:
            act = r.get("activity") or {}
            pairs.append({
                "t0": prev["t"], "t1": cur["t"],
                "d_session": cur["sp"] - prev["sp"],
                "d_weekly": cur["wp"] - prev["wp"],
                "own_tokens": weighted_tokens((act.get("own") or {}).get("tokens")),
                "other_tokens": weighted_tokens((act.get("other") or {}).get("tokens")),
                "weekly_resets": cur["wr"],
            })
        prev = cur
    return pairs


# ------------------------------------------------------------------ ratio

def estimate_ratio(pairs, now=None, trimmed_days=DEFAULT_TRIMMED_DAYS):
    """weekly-%-per-session-% : median over all usable pairs (delta_session
    > 0), plus a trimmed mean restricted to the last `trimmed_days` days."""
    now = now or datetime.now(UTC)
    candidates = [p for p in pairs if p["d_session"] > 0]
    if len(candidates) < MIN_PAIRS:
        return {"status": "insufficient_data", "n": len(candidates), "min_pairs": MIN_PAIRS}
    ratios = [p["d_weekly"] / p["d_session"] for p in candidates]
    cutoff = now - timedelta(days=trimmed_days)
    recent = [p["d_weekly"] / p["d_session"] for p in candidates if p["t1"] >= cutoff]
    out = {
        "status": "ok", "n": len(candidates),
        "median": median(ratios),
        "n_recent": len(recent), "trimmed_days": trimmed_days,
        "trimmed_mean": trimmed_mean(recent) if len(recent) >= MIN_PAIRS else None,
    }
    if out["trimmed_mean"] is None:
        out["trimmed_mean_note"] = f"insufficient recent data (n={len(recent)} < {MIN_PAIRS})"
    return out


def windows_per_week(ratio):
    """How many full session-limit windows fit into one weekly budget."""
    if not ratio:
        return None
    return 100.0 / (100.0 * ratio)


def windows_left_this_week(ratio, weekly_pct_now):
    """How many more full session windows fit before the weekly budget runs
    out, at the current weekly %."""
    if not ratio or weekly_pct_now is None:
        return None
    return max(0.0, 100.0 - weekly_pct_now) / (100.0 * ratio)


# ------------------------------------------------------------------ attribution

def estimate_side_rates(pairs):
    """%-session-per-weighted-token rate for each side, from intervals where
    only that side was active (a clean single-variable measurement)."""
    out = {}
    for side in ("own", "other"):
        tok_key = f"{side}_tokens"
        other_key = "other_tokens" if side == "own" else "own_tokens"
        clean = [p for p in pairs if p["d_session"] > 0 and p[tok_key] > 0 and p[other_key] == 0]
        n = len(clean)
        rate = median([p["d_session"] / p[tok_key] for p in clean]) if n >= MIN_PAIRS else None
        out[side] = {"rate": rate, "n": n}
        if rate is None:
            out[side]["note"] = f"insufficient clean single-side pairs (n={n} < {MIN_PAIRS})"
    return out


def estimate_weekly_share(pairs, rates, current_weekly_resets):
    """AFClaude ("own") vs user ("other") share of this weekly cycle's
    accumulated weekly %, estimated by applying each side's measured
    %-per-token rate to every interval in the current cycle and normalizing.
    Requires both sides to have a usable clean rate (see estimate_side_rates)."""
    own_rate = rates.get("own", {}).get("rate")
    other_rate = rates.get("other", {}).get("rate")
    if own_rate is None or other_rate is None:
        missing = [s for s in ("own", "other") if rates.get(s, {}).get("rate") is None]
        return {"status": "insufficient_data", "missing_side_rate": missing}
    cycle_pairs = [p for p in pairs if p["weekly_resets"] == current_weekly_resets]
    attributed_own = sum(own_rate * p["own_tokens"] for p in cycle_pairs)
    attributed_other = sum(other_rate * p["other_tokens"] for p in cycle_pairs)
    total = attributed_own + attributed_other
    if total <= 0:
        return {"status": "insufficient_data", "reason": "no attributable activity this cycle", "n_pairs": len(cycle_pairs)}
    observed_delta = None
    if cycle_pairs:
        observed_delta = round(sum(p["d_weekly"] for p in cycle_pairs), 2)
    return {
        "status": "ok", "n_pairs": len(cycle_pairs),
        "own_share": attributed_own / total, "other_share": attributed_other / total,
        "observed_weekly_pct_delta": observed_delta,
        "note": "share of the weekly % change covered by sampled pairs this cycle; "
                "may not cover the full week if sampling started mid-cycle",
    }


# ------------------------------------------------------------------ snapshot

def compute(rows, now=None, trimmed_days=DEFAULT_TRIMMED_DAYS):
    """Full snapshot from a list of already-parsed sample rows (oldest
    first). Pure function, easy to unit test with synthetic rows and to call
    from usage_sampler.py with in-memory rows (no extra file I/O there)."""
    now = now or datetime.now(UTC)
    pairs = build_pairs(rows)
    ratio_est = estimate_ratio(pairs, now=now, trimmed_days=trimmed_days)
    ratio = ratio_est.get("median") if ratio_est["status"] == "ok" else None

    weekly_pct_now = None
    weekly_resets_now = None
    for r in reversed(rows):
        w = (r.get("usage") or {}).get("weekly") or {}
        if w.get("percent") is not None and w.get("resets_at"):
            weekly_pct_now = w["percent"]
            weekly_resets_now = _round_reset(w["resets_at"])
            break

    rates = estimate_side_rates(pairs)
    share = (estimate_weekly_share(pairs, rates, weekly_resets_now)
             if weekly_resets_now is not None else {"status": "insufficient_data", "reason": "no current weekly sample"})

    return {
        "generated_at": now.isoformat(),
        "ratio": ratio_est,
        "windows_per_week": windows_per_week(ratio),
        "windows_left_this_week": windows_left_this_week(ratio, weekly_pct_now),
        "weekly_pct_now": weekly_pct_now,
        "attribution": {"rates": rates, "week_share": share},
    }


def compute_from_file(path=SAMPLES, now=None, trimmed_days=DEFAULT_TRIMMED_DAYS):
    return compute(load_samples(path), now=now, trimmed_days=trimmed_days)


# ------------------------------------------------------------------ CLI

def _fmt_pct(x, digits=1):
    return "n/a" if x is None else f"{x:.{digits}f}%"


def _fmt_ratio(x):
    return "n/a" if x is None else f"{x:.4g}"


def _fmt_share(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def human_summary(snap):
    lines = []
    r = snap["ratio"]
    if r["status"] != "ok":
        lines.append(f"Session->weekly ratio: insufficient data (n={r['n']} < {r['min_pairs']} usable pairs)")
    else:
        lines.append(f"Session->weekly ratio: median {_fmt_ratio(r['median'])} weekly-%/session-% "
                     f"(n={r['n']}), trimmed mean over last {r['trimmed_days']}d: "
                     f"{_fmt_ratio(r['trimmed_mean']) if r['trimmed_mean'] is not None else 'insufficient data (n_recent=' + str(r['n_recent']) + ')'}")
        lines.append(f"  -> about {snap['windows_per_week']:.1f} full session windows fit in one week")
        if snap["weekly_pct_now"] is not None:
            lines.append(f"  -> at {_fmt_pct(snap['weekly_pct_now'], 0)} weekly used now: "
                         f"{snap['windows_left_this_week']:.1f} session windows left this week")
    rates = snap["attribution"]["rates"]
    lines.append(f"AFClaude (own) rate: {_fmt_ratio(rates['own']['rate'])} session-%/weighted-token (n={rates['own']['n']})")
    lines.append(f"User (other) rate: {_fmt_ratio(rates['other']['rate'])} session-%/weighted-token (n={rates['other']['n']})")
    share = snap["attribution"]["week_share"]
    if share["status"] != "ok":
        reason = share.get("missing_side_rate") or share.get("reason") or "?"
        lines.append(f"This week's weekly % share: insufficient data ({reason})")
    else:
        lines.append(f"This week's weekly % share so far: AFClaude {_fmt_share(share['own_share'])} / "
                     f"user {_fmt_share(share['other_share'])} (n_pairs={share['n_pairs']}, "
                     f"observed weekly % delta in sample {share['observed_weekly_pct_delta']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", default=SAMPLES)
    ap.add_argument("--trimmed-days", type=int, default=DEFAULT_TRIMMED_DAYS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    snap = compute_from_file(args.samples, trimmed_days=args.trimmed_days)
    if args.json:
        print(json.dumps(snap, indent=1, default=str))
    else:
        print(human_summary(snap))


if __name__ == "__main__":
    main()
