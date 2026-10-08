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
  - an attribution of this week's weekly % between AFClaude and the user,
    BY TIME (estimate_time_split, the primary `attribution.week_share`): the
    weekly-% rise while an autonomous AFClaude run was going (a keep-alive /
    dispatcher / usage-review fire until that run ended: data/afclaude_runs.jsonl,
    else pacing.autonomous_spans) is AFClaude's; every other rise is the
    user's -- including the owner's interactive chats with the task-manager
    session (D-018: the owner's input to AFClaude is the owner's own use) and
    rises no local transcript explains (other devices, claude.ai; D-138 gap 3).
    Inside a run, a parallel token split against non-AFClaude sessions moves
    that part to the user.
  - the old token split (`attribution.week_share_tokens`, secondary signal):
    output(+thinking)- and cache-weighted token deltas split by
    usage_sampler's activity.own/activity.other. That split is by SESSION
    (AFClaude cwd/name = "own"), not by who drove it, and it cannot see other
    devices, so it calls everything the owner does in the AFClaude session
    "AFClaude". Intervals where only one side was active are a clean
    measurement of that side's %-per-token rate; those rates are applied to
    every interval in the current weekly cycle.

`resets_at` jitters by a few hundred ms between fetches even when the
underlying reset hasn't moved (see usage_sampler.track_cycle), so windows are
compared after rounding to the minute the same way track_cycle does: add 30s,
then floor -- a reset that's genuinely NNN:59:5x/NNN+1:00:0x rounds to the
same minute either way.

Per-session-window ratio (preferred): the 15-min pair median above is
biased low because both meters are integer percent (weekly mostly moves 0-1
point per pair). `ratio_windows` instead measures each completed 5-h window
from its first to its last reading (rounding error of two readings only), see
window_record() for baseline / weekly-reset split / saturation / partial
rules; records are kept in the never-pruned data/session_windows.jsonl
(appended by the sampler, rebuilt with --backfill-windows). `preferred_ratio`
is that estimate once MIN_WINDOWS usable windows exist, else the old median
(flagged). The old `ratio` fields are unchanged.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_stale  # noqa: E402  (stale rows: their meters are ignored)

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
        u = usage_stale.row_usage(r)
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


# ------------------------------------------------------------------ per-session-window

# Why per window: both meters are reported as INTEGER percent. Over one 15-min
# pair the weekly meter mostly moves 0 or 1 point while the session meter moves
# several, so per-pair ratios (and their median) are biased low and very noisy.
# Both meters are cumulative within a window, so the deltas between the first
# and the last reading of a whole 5-h window carry the rounding error of only
# two readings each (+-0.5 point per reading), however many samples lie between.
SESSION_LEN = timedelta(hours=5)
MIN_COVERAGE_FRAC = 0.7     # samples must span >= 70% of the 5 h (3.5 h), else "partial"
MIN_D_SESSION = 5.0         # windows with fewer session points are "low_activity" (rounding dominates)
GAP_FLAG = timedelta(minutes=40)  # a gap this long inside a window is flagged (not excluded: meters are cumulative)
CAP_PCT = 100.0             # a meter at 100% is saturated: the window is cut before it
READ_ERR = 0.5              # +- rounding error of one integer reading
MIN_WINDOWS = 5             # usable windows needed before the per-window ratio is preferred
DIST_MAX = 30               # most recent windows listed in the snapshot's distribution
WINDOW_RECORD_VERSION = 1
SESSION_WINDOWS = os.path.join(DATA, "session_windows.jsonl")  # never pruned


def _points(rows):
    """Usable readings, oldest first: t, sp, wp, sr (rounded session reset or
    None when no window is open), wr, own/other weighted tokens of the interval
    ending at this sample."""
    pts = []
    for r in rows:
        u = usage_stale.row_usage(r)
        s = u.get("session") or {}
        w = u.get("weekly") or {}
        t = _parse_ts(r.get("at"))
        if not t or s.get("percent") is None or w.get("percent") is None or not w.get("resets_at"):
            continue
        act = r.get("activity") or {}
        pts.append({"t": t, "sp": float(s["percent"]), "wp": float(w["percent"]),
                    "sr": _round_reset(s.get("resets_at")), "wr": _round_reset(w["resets_at"]),
                    "own": weighted_tokens((act.get("own") or {}).get("tokens")),
                    "other": weighted_tokens((act.get("other") or {}).get("tokens"))})
    pts.sort(key=lambda p: p["t"])
    return pts


def _iso(dt):
    return dt.isoformat() if dt else None


def _ratio_interval(dw, ds, ew, es):
    """Worst-case bounds of dw/ds when dw is off by up to ew and ds by up to es."""
    lo = max(0.0, dw - ew) / (ds + es) if ds + es > 0 else None
    hi = (dw + ew) / (ds - es) if ds - es > 0 else None
    return lo, hi


def window_record(key, pts, pre=None):
    """One completed window (key = rounded session resets_at = window end) from
    its in-window points (oldest first) and the point just before them.

    Baseline: when the sample just before the window is idle (no session window
    open, session 0%), in the same weekly cycle, and less than 5 h before the
    window start (so no other, unseen window fits in between), nothing was used
    between it and the window start: it is the baseline with an EXACT session
    reading of 0 (no rounding error on that end). Otherwise the first in-window
    sample is the baseline.

    Weekly reset inside the window: the window is SPLIT into one segment per
    weekly cycle; each segment is measured first-to-last on its own and the
    deltas are summed. Only the interval between the last old-cycle and the
    first new-cycle sample (one sampling step, its weekly delta unknowable) is
    dropped, and the rounding error counts the extra readings. Excluding such
    windows outright would throw away up to 5 h of good data per week.

    Saturation: once a meter reads 100% it stops tracking (the weekly meter sat
    at 100 while the session meter still rose), so the window is cut at the
    last sample before any meter reached 100 (flag "capped")."""
    start = key - SESSION_LEN
    flags = []
    pts = list(pts)
    base_kind = "first_sample"
    if (pre and pre["sr"] is None and pre["sp"] == 0 and pts and pre["wr"] == pts[0]["wr"]
            and pre["t"] > start - SESSION_LEN and pre["t"] <= start + timedelta(minutes=1)):
        pts = [dict(pre, exact_session=True)] + pts
        base_kind = "idle_pre_sample"
    # coverage is judged on what was observed, before any saturation cut
    # (a capped window is cut on purpose, it isn't missing data)
    observed = (pts[-1]["t"] - pts[0]["t"]) if pts else timedelta(0)
    if pts and len({p["wr"] for p in pts}) > 1:   # minus the dropped straddling interval(s)
        observed = sum((pts[i + 1]["t"] - pts[i]["t"] for i in range(len(pts) - 1)
                        if pts[i]["wr"] == pts[i + 1]["wr"]), timedelta(0))
    # cut at saturation
    cut = None
    for i, p in enumerate(pts):
        if p["sp"] >= CAP_PCT or p["wp"] >= CAP_PCT:
            cut = i
            break
    if cut is not None:
        flags.append("capped")
        pts = pts[:cut]
    # segments by weekly cycle
    segs = []
    for p in pts:
        if segs and segs[-1][-1]["wr"] == p["wr"]:
            segs[-1].append(p)
        else:
            segs.append([p])
    if len(segs) > 1:
        flags.append("weekly_reset_split")
    ds = dw = es = ew = 0.0
    var_s = var_w = 0.0
    covered = timedelta(0)
    own = other = 0.0
    seg_out = []
    for seg in segs:
        a, b = seg[0], seg[-1]
        if len(seg) < 2:
            continue
        d_s, d_w = b["sp"] - a["sp"], b["wp"] - a["wp"]
        e_s = READ_ERR * (1 if a.get("exact_session") else 2)
        ds, dw, es, ew = ds + d_s, dw + d_w, es + e_s, ew + 2 * READ_ERR
        var_s += (1 if a.get("exact_session") else 2) / 12.0   # uniform(+-0.5) has variance 1/12
        var_w += 2 / 12.0
        covered += b["t"] - a["t"]
        own += sum(p["own"] for p in seg[1:])
        other += sum(p["other"] for p in seg[1:])
        seg_out.append({"first_at": _iso(a["t"]), "last_at": _iso(b["t"]), "weekly_resets_at": _iso(a["wr"]),
                        "d_session": d_s, "d_weekly": d_w})
    max_gap = max((pts[i + 1]["t"] - pts[i]["t"] for i in range(len(pts) - 1)), default=timedelta(0))
    if max_gap > GAP_FLAG:
        flags.append("gap")
    cov_frac = observed / SESSION_LEN
    rec = {
        "v": WINDOW_RECORD_VERSION,
        "window_end": _iso(key), "window_start": _iso(start),
        "weekly_resets_at": _iso(pts[0]["wr"]) if pts else None,
        "baseline": base_kind,
        "first_at": _iso(pts[0]["t"]) if pts else None, "last_at": _iso(pts[-1]["t"]) if pts else None,
        "n_samples": len(pts), "covered_min": round(covered.total_seconds() / 60, 1),
        "observed_min": round(observed.total_seconds() / 60, 1),
        "coverage_frac": round(cov_frac, 3), "max_gap_min": round(max_gap.total_seconds() / 60, 1),
        "session_start_pct": pts[0]["sp"] if pts else None, "session_end_pct": pts[-1]["sp"] if pts else None,
        "weekly_start_pct": pts[0]["wp"] if pts else None, "weekly_end_pct": pts[-1]["wp"] if pts else None,
        "d_session": ds, "d_weekly": dw, "err_session": es, "err_weekly": ew,
        "rounding_var_session": round(var_s, 4), "rounding_var_weekly": round(var_w, 4),
        "ratio": dw / ds if ds > 0 else None,
        "ratio_lo": None, "ratio_hi": None,
        "own_w_tokens": round(own, 1), "other_w_tokens": round(other, 1),
        "own_share": round(own / (own + other), 4) if own + other > 0 else None,
        "other_share": round(other / (own + other), 4) if own + other > 0 else None,
    }
    if ds > 0:
        rec["ratio_lo"], rec["ratio_hi"] = _ratio_interval(dw, ds, ew, es)
    if len(seg_out) > 1:
        rec["segments"] = seg_out
    reason = None
    if ds < 0 or dw < 0:
        flags.append("inconsistent")
        reason = "negative delta"
    elif cov_frac < MIN_COVERAGE_FRAC:
        flags.append("partial")
        reason = f"partial: samples cover {cov_frac:.0%} of the window (< {MIN_COVERAGE_FRAC:.0%})"
    elif ds < MIN_D_SESSION:
        flags.append("low_activity")
        reason = f"low activity: d_session {ds:g} < {MIN_D_SESSION:g}"
    rec["flags"] = flags
    rec["usable"] = reason is None
    rec["exclude_reason"] = reason
    return rec


def build_windows(rows, now=None, include_open=False):
    """Per-session-window records from sample rows (oldest first). A window is
    completed once `now` (default: the latest sample) has reached its end; the
    open window is left out unless include_open (then flagged "open")."""
    pts = _points(rows)
    if not pts:
        return []
    now = now or pts[-1]["t"]
    groups, order, pre_of = {}, [], {}
    prev = None
    for p in pts:
        k = p["sr"]
        if k is not None:
            if k not in groups:
                groups[k] = []
                order.append(k)
                pre_of[k] = prev
            groups[k].append(p)
        prev = p
    out = []
    for k in order:
        is_open = now < k
        if is_open and not include_open:
            continue
        rec = window_record(k, groups[k], pre_of[k])
        if is_open:
            rec["flags"].append("open")
            rec["usable"] = False
            rec["exclude_reason"] = rec["exclude_reason"] or "open: window not finished"
        out.append(rec)
    return out


def load_windows(path=SESSION_WINDOWS):
    return load_samples(path)


def merge_windows(stored, derived):
    """Stored records (never pruned) win; derived ones fill in the rest. Sorted by window end."""
    by = {}
    for w in derived or []:
        if w.get("window_end"):
            by[w["window_end"]] = w
    for w in stored or []:
        if w.get("window_end"):
            by[w["window_end"]] = w
    return [by[k] for k in sorted(by)]


def append_new_windows(rows, path=SESSION_WINDOWS, now=None, source="sampler"):
    """Append every completed window found in `rows` that the file doesn't
    have yet (called by the sampler on each run, so a window is recorded on
    the first run after it ends). Returns (all stored records, newly added)."""
    stored = load_windows(path)
    have = {w.get("window_end") for w in stored}
    computed_at = (now or datetime.now(UTC)).isoformat()
    new = []
    for w in build_windows(rows, now=now):
        if w["window_end"] in have:
            continue
        w = dict(w, source=source, computed_at=computed_at)
        new.append(w)
    if new:
        with open(path, "a") as fh:
            for w in new:
                fh.write(json.dumps(w) + "\n")
    return stored + new, new


def backfill_windows(samples_path=SAMPLES, out_path=SESSION_WINDOWS, now=None):
    """Rebuild out_path from samples: every window derivable from the samples
    is recomputed; stored windows the samples no longer cover (pruned) are kept,
    since the file is never pruned. Atomic replace. Returns the records."""
    now_dt = now or datetime.now(UTC)
    derived = [dict(w, source="backfill", computed_at=now_dt.isoformat())
               for w in build_windows(load_samples(samples_path), now=now)]
    stored = load_windows(out_path)
    derived_keys = {w["window_end"] for w in derived}
    keep = [w for w in stored if w.get("window_end") not in derived_keys]
    allw = merge_windows(keep, derived)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        for w in allw:
            fh.write(json.dumps(w) + "\n")
    os.replace(tmp, out_path)
    return allw


def _percentile(sorted_xs, q):
    """Linear-interpolated percentile (q in 0..1) of an already sorted list."""
    if not sorted_xs:
        return None
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def estimate_window_ratio(windows, pairs=None, now=None, recent_days=DEFAULT_TRIMMED_DAYS):
    """Weekly-%-per-session-% from completed, usable windows.

    weighted = sum(d_weekly) / sum(d_session): each window's ratio weighted by
    its d_session, so long/busy windows count more (and small windows, where
    rounding dominates, count little). Dispersion across windows (real
    variation: model mix, context size, ...) is reported as stdev, weighted
    stdev, IQR and percentiles; `se` is the ratio estimator's standard error.
    The rounding error of the pooled estimate: `rounding_sd` (readings
    independent, uniform +-0.5) and `rounding_worst` (every reading off by 0.5
    in the unfavourable direction). `vs_15min` compares with the old estimate."""
    now = now or datetime.now(UTC)
    allw = [w for w in windows or [] if "open" not in (w.get("flags") or [])]
    use = [w for w in allw if w.get("usable") and w.get("d_session")]
    excluded = {}
    for w in allw:
        if not w.get("usable"):
            r = (w.get("exclude_reason") or "?").split(":")[0]
            excluded[r] = excluded.get(r, 0) + 1
    out = {"n": len(use), "n_total": len(allw), "excluded": excluded, "min_windows": MIN_WINDOWS,
           "min_d_session": MIN_D_SESSION, "min_coverage_frac": MIN_COVERAGE_FRAC}
    if pairs is not None:
        out["vs_15min"] = _vs_15min(pairs, None)
    if not use:
        out["status"] = "insufficient_data"
        return out
    S = sum(w["d_session"] for w in use)
    W = sum(w["d_weekly"] for w in use)
    R = W / S
    ratios = sorted(w["ratio"] for w in use)
    n = len(use)
    out.update({
        "status": "ok" if n >= MIN_WINDOWS else "low_n",
        "weighted": R, "sum_d_session": S, "sum_d_weekly": W,
        "mean": sum(ratios) / n, "median": _percentile(ratios, 0.5),
        "stdev": statistics.stdev(ratios) if n >= 2 else None,
        "weighted_stdev": (sum(w["d_session"] * (w["ratio"] - R) ** 2 for w in use) / S) ** 0.5,
        "p10": _percentile(ratios, 0.1), "p25": _percentile(ratios, 0.25),
        "p75": _percentile(ratios, 0.75), "p90": _percentile(ratios, 0.9),
        "min": ratios[0], "max": ratios[-1],
    })
    out["iqr"] = out["p75"] - out["p25"]
    # how much of the window-to-window spread rounding alone explains, and the rest
    rv = sum(w["d_session"] * (w.get("rounding_var_weekly", 2 / 12.0) + w["ratio"] ** 2
                               * w.get("rounding_var_session", 2 / 12.0)) / w["d_session"] ** 2 for w in use) / S
    out["rounding_spread"] = rv ** 0.5
    out["intrinsic_stdev"] = max(0.0, out["weighted_stdev"] ** 2 - rv) ** 0.5
    if n >= 2:
        mean_s = S / n
        out["se"] = (sum((w["d_weekly"] - R * w["d_session"]) ** 2 for w in use) / (n * (n - 1))) ** 0.5 / mean_s
    else:
        out["se"] = None
    var_w = sum(w.get("rounding_var_weekly", 2 / 12.0) for w in use)
    var_s = sum(w.get("rounding_var_session", 2 / 12.0) for w in use)
    out["rounding_sd"] = (var_w + R * R * var_s) ** 0.5 / S   # delta method for W/S
    out["rounding_worst"] = list(_ratio_interval(W, S, sum(w["err_weekly"] for w in use),
                                                 sum(w["err_session"] for w in use)))
    cutoff = now - timedelta(days=recent_days)
    rec = [w for w in use if (_parse_ts(w.get("window_end")) or now) >= cutoff]
    rs = sum(w["d_session"] for w in rec)
    out["recent_days"] = recent_days
    out["n_recent"] = len(rec)
    out["weighted_recent"] = sum(w["d_weekly"] for w in rec) / rs if rs > 0 else None
    out["distribution"] = [[(w.get("window_end") or "")[5:16], round(w["ratio"], 4), w["d_session"]]
                           for w in sorted(use, key=lambda w: w.get("window_end") or "")[-DIST_MAX:]]
    if pairs is not None:
        out["vs_15min"] = _vs_15min(pairs, R)
    return out


def _vs_15min(pairs, weighted):
    """The old consecutive-pair estimate next to the per-window one. The
    sum-ratio over pairs telescopes to window endpoints (nearly unbiased); the
    median of per-pair ratios is what integer rounding drags down."""
    cand = [p for p in pairs if p["d_session"] > 0]
    rs = sorted(p["d_weekly"] / p["d_session"] for p in cand)
    S = sum(p["d_session"] for p in cand)
    med = median(rs)
    return {"n_pairs": len(cand), "median_15min": med,
            "sum_ratio_15min": sum(p["d_weekly"] for p in cand) / S if S > 0 else None,
            "share_zero_weekly": round(sum(1 for p in cand if p["d_weekly"] == 0) / len(cand), 3) if cand else None,
            "factor_vs_median": (weighted / med) if weighted is not None and med else None}


def preferred_ratio(snap):
    """The ratio consumers should use: the per-window weighted estimate once
    MIN_WINDOWS usable windows exist, else the old 15-min median (flagged).
    Accepts a compute() snapshot; returns {value, source, n, flagged, spread, note}."""
    rw = (snap or {}).get("ratio_windows") or {}
    old = (snap or {}).get("ratio") or {}
    if rw.get("status") == "ok":
        return {"value": rw["weighted"], "source": "windows", "n": rw["n"], "flagged": False,
                "spread": rw.get("weighted_stdev"), "iqr": rw.get("iqr"), "se": rw.get("se"),
                "rounding_sd": rw.get("rounding_sd")}
    if old.get("status") == "ok":
        return {"value": old.get("median"), "source": "15min_median", "n": old.get("n"), "flagged": True,
                "spread": None, "note": f"only {rw.get('n', 0)} usable session windows (< {MIN_WINDOWS}); "
                                        "15-min median is biased low by integer rounding"}
    return {"value": None, "source": None, "n": 0, "flagged": True, "note": "insufficient data"}


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
    """AFClaude-SESSION ("own") vs other-session ("other") share of this weekly
    cycle's accumulated weekly %, estimated by applying each side's measured
    %-per-token rate to every interval in the current cycle and normalizing.
    Requires both sides to have a usable clean rate (see estimate_side_rates).
    Secondary signal only (`week_share_tokens`): "own" includes the owner's
    interactive turns in AFClaude sessions, and usage on other devices is
    invisible to it; the AFClaude vs user split is estimate_time_split()."""
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
        "status": "ok", "method": "tokens", "n_pairs": len(cycle_pairs),
        "own_share": attributed_own / total, "other_share": attributed_other / total,
        "observed_weekly_pct_delta": observed_delta,
        "note": "token split by session (AFClaude sessions incl. the owner's own turns in them vs other "
                "sessions on this host; other devices invisible); covers sampled pairs only",
    }


# ------------------------------------------------------------------ time-based split (primary)

WEEK = timedelta(days=7)
RUN_END_GRACE = timedelta(minutes=10)   # the meter lags the run's last turn a little
FIRE_COVER = timedelta(minutes=2)       # a fire this close to a run row's span belongs to that run


def combine_spans(run_rows=(), fallback=(), now=None):
    """Autonomous AFClaude periods [(start, end)], merged and sorted.
    run_rows: run_metrics rows (data/afclaude_runs.jsonl): start..end, an ongoing run until `now`.
    fallback: [(fire, end)] from pacing.autonomous_spans; used only for fires no run row covers
    (a fire run_metrics hasn't written yet, a usage-review or dispatcher run)."""
    spans = []
    for r in run_rows or []:
        if not isinstance(r, dict):
            continue
        a, b = _parse_ts(r.get("start")), _parse_ts(r.get("end"))
        if a is None:
            continue
        if r.get("ongoing") or b is None:
            b = max(b or a, now or a)
        spans.append((a, max(a, b)))
    rows_spans = list(spans)
    for a, b in fallback or []:
        a, b = _parse_ts(a), _parse_ts(b)
        if a is None or b is None or (now is not None and a > now):
            continue
        if any(x - FIRE_COVER <= a <= y + FIRE_COVER for x, y in rows_spans):
            continue
        spans.append((a, max(a, b)))
    spans.sort()
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _overlap(t0, t1, spans):
    """Seconds of (t0, t1] inside any span (spans merged, non-overlapping)."""
    tot = 0.0
    for a, b in spans:
        lo, hi = max(t0, a), min(t1, b)
        if hi > lo:
            tot += (hi - lo).total_seconds()
    return tot


def estimate_time_split(rows, spans, current_weekly_resets, now=None):
    """AFClaude vs user share of this weekly cycle's weekly %, by TIME.

    Walks the cycle's usable (non-stale) weekly readings from the reset (the meter is 0 there):
    each rise between two readings is AFClaude's for the part of the interval that lies inside
    an autonomous run (`spans`, extended by RUN_END_GRACE for the meter lag), the user's for the
    rest. Inside a run, the interval's token split moves the share of non-AFClaude-session tokens
    to the user (the owner working in parallel elsewhere). Stale readings are skipped, so a rise
    across a stale gap is still counted (split by the gap's overlap with runs). `user_unseen_pct`
    is the user's rise in intervals with no transcript tokens at all on this host (other devices,
    claude.ai, or a meter lagging an earlier interval)."""
    if current_weekly_resets is None:
        return {"status": "insufficient_data", "method": "time", "reason": "no current weekly sample"}
    if spans is None:
        return {"status": "insufficient_data", "method": "time", "reason": "AFClaude run spans unavailable"}
    now = now or datetime.now(UTC)
    start = current_weekly_resets - WEEK
    grace = [(a, b + RUN_END_GRACE) for a, b in spans]
    merged = combine_spans(fallback=grace)
    prev_t, prev_w = start, 0.0
    tok_o = tok_u = 0.0
    own = user = unseen = 0.0
    n = 0
    seq = sorted(((_parse_ts(r.get("at")), r) for r in rows if isinstance(r, dict) and r.get("at")),
                 key=lambda x: x[0])
    for t, r in seq:
        if t <= start or t > now + timedelta(minutes=1):
            continue
        act = r.get("activity") or {}
        tok_o += weighted_tokens((act.get("own") or {}).get("tokens"))
        tok_u += weighted_tokens((act.get("other") or {}).get("tokens"))
        w = usage_stale.row_usage(r).get("weekly") or {}
        if w.get("percent") is None or _round_reset(w.get("resets_at")) != current_weekly_resets:
            continue
        wp = float(w["percent"])
        dw = max(wp - prev_w, 0.0)
        if dw > 0 and t > prev_t:
            frac = min(1.0, _overlap(prev_t, t, merged) / (t - prev_t).total_seconds())
            auto, man = dw * frac, dw * (1 - frac)
            uf = tok_u / (tok_o + tok_u) if tok_o + tok_u > 0 else 0.0
            own += auto * (1 - uf)
            user += auto * uf + man
            if tok_o + tok_u == 0:
                unseen += man
        prev_w, prev_t = max(prev_w, wp), t
        tok_o = tok_u = 0.0
        n += 1
    total = own + user
    in_cycle = [(max(a, start), min(b, now)) for a, b in spans if b > start and a < now]
    out = {"method": "time", "n_readings": n, "weekly_pct": round(prev_w, 1),
           "own_pct": round(own, 2), "user_pct": round(user, 2), "user_unseen_pct": round(unseen, 2),
           "runs_in_cycle": len(in_cycle),
           "run_hours": round(sum((b - a).total_seconds() for a, b in in_cycle) / 3600, 2),
           "note": "weekly % risen during AFClaude's autonomous runs vs everything else (the owner's "
                   "chats with the task-manager and other devices count as user usage)"}
    if total <= 0:
        out.update(status="insufficient_data", reason="no weekly % rise this cycle yet")
        return out
    out.update(status="ok", own_share=own / total, other_share=user / total)
    return out


def load_autonomous_spans(rows, now=None, runs_path=None):
    """The autonomous AFClaude periods from the live files: run_metrics rows plus pacing's fire
    spans for fires without a row. None if neither source can be read (the caller then reports
    the time split as unavailable rather than calling everything user usage)."""
    now = now or datetime.now(UTC)
    run_rows = fallback = None
    try:
        import run_metrics
        run_rows = run_metrics.read_rows(runs_path)
    except Exception:   # noqa: BLE001 - advisory
        run_rows = None
    try:
        import pacing
        srt = sorted((r for r in rows if isinstance(r, dict) and _parse_ts(r.get("at"))),
                     key=lambda r: _parse_ts(r["at"]))
        fallback = pacing.autonomous_spans(srt, pacing.fire_times())
    except Exception:   # noqa: BLE001 - advisory
        fallback = None
    if run_rows is None and fallback is None:
        return None
    return combine_spans(run_rows or [], fallback or [], now)


# ------------------------------------------------------------------ snapshot

def compute(rows, now=None, trimmed_days=DEFAULT_TRIMMED_DAYS, windows=None, spans=None):
    """Full snapshot from a list of already-parsed sample rows (oldest
    first). Pure function, easy to unit test with synthetic rows and to call
    from usage_sampler.py with in-memory rows (no extra file I/O there).
    `windows`: stored per-session-window records (data/session_windows.jsonl);
    merged with the windows derivable from `rows` (stored ones win).
    `spans`: autonomous AFClaude run periods [(start, end)] (load_autonomous_spans);
    None = unknown, the AFClaude vs user split is then reported as unavailable."""
    now = now or datetime.now(UTC)
    pairs = build_pairs(rows)
    ratio_est = estimate_ratio(pairs, now=now, trimmed_days=trimmed_days)
    ratio = ratio_est.get("median") if ratio_est["status"] == "ok" else None

    weekly_pct_now = None
    weekly_resets_now = None
    for r in reversed(rows):
        w = usage_stale.row_usage(r).get("weekly") or {}
        if w.get("percent") is not None and w.get("resets_at"):
            weekly_pct_now = w["percent"]
            weekly_resets_now = _round_reset(w["resets_at"])
            break

    rates = estimate_side_rates(pairs)
    share_tok = (estimate_weekly_share(pairs, rates, weekly_resets_now)
                 if weekly_resets_now is not None else {"status": "insufficient_data", "reason": "no current weekly sample"})
    share = estimate_time_split(rows, spans, weekly_resets_now, now=now)

    wins = merge_windows(windows, build_windows(rows, now=now))
    snap = {
        "generated_at": now.isoformat(),
        "ratio": ratio_est,
        "windows_per_week": windows_per_week(ratio),
        "windows_left_this_week": windows_left_this_week(ratio, weekly_pct_now),
        "weekly_pct_now": weekly_pct_now,
        "attribution": {"rates": rates, "week_share": share, "week_share_tokens": share_tok},
        "ratio_windows": estimate_window_ratio(wins, pairs=pairs, now=now, recent_days=trimmed_days),
    }
    pref = preferred_ratio(snap)
    pref["windows_per_week"] = windows_per_week(pref["value"])
    pref["windows_left_this_week"] = windows_left_this_week(pref["value"], weekly_pct_now)
    snap["preferred_ratio"] = pref
    return snap


def compute_from_file(path=SAMPLES, now=None, trimmed_days=DEFAULT_TRIMMED_DAYS, windows_path=SESSION_WINDOWS,
                      runs_path=None):
    rows = load_samples(path)
    return compute(rows, now=now, trimmed_days=trimmed_days,
                   windows=load_windows(windows_path) if windows_path else None,
                   spans=load_autonomous_spans(rows, now=now, runs_path=runs_path))


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
        if snap["windows_per_week"] is not None:
            lines.append(f"  -> about {snap['windows_per_week']:.1f} full session windows fit in one week")
        if snap["weekly_pct_now"] is not None and snap["windows_left_this_week"] is not None:
            lines.append(f"  -> at {_fmt_pct(snap['weekly_pct_now'], 0)} weekly used now: "
                         f"{snap['windows_left_this_week']:.1f} session windows left this week")
    rw = snap.get("ratio_windows") or {}
    if rw.get("weighted") is not None:
        vs = rw.get("vs_15min") or {}
        lines.append(f"Per-session-window ratio ({rw['status']}): weighted {_fmt_ratio(rw['weighted'])} "
                     f"(n={rw['n']} of {rw['n_total']} windows; stdev {_fmt_ratio(rw.get('stdev'))}, "
                     f"IQR {_fmt_ratio(rw.get('p25'))}..{_fmt_ratio(rw.get('p75'))}, se {_fmt_ratio(rw.get('se'))}, "
                     f"rounding sd {_fmt_ratio(rw.get('rounding_sd'))}); old 15-min median "
                     f"{_fmt_ratio(vs.get('median_15min'))}")
    else:
        lines.append(f"Per-session-window ratio: insufficient data (0 usable of {rw.get('n_total', 0)} windows)")
    pref = snap.get("preferred_ratio") or {}
    lines.append(f"  -> preferred: {_fmt_ratio(pref.get('value'))} (source {pref.get('source')}"
                 f"{', flagged' if pref.get('flagged') else ''})")
    rates = snap["attribution"]["rates"]
    lines.append(f"AFClaude (own) rate: {_fmt_ratio(rates['own']['rate'])} session-%/weighted-token (n={rates['own']['n']})")
    lines.append(f"User (other) rate: {_fmt_ratio(rates['other']['rate'])} session-%/weighted-token (n={rates['other']['n']})")
    share = snap["attribution"]["week_share"]
    if share["status"] != "ok":
        lines.append(f"This week's weekly %, AFClaude runs vs user: insufficient data ({share.get('reason') or '?'})")
    else:
        lines.append(f"This week's weekly %, AFClaude runs vs user (by time): AFClaude {_fmt_share(share['own_share'])} "
                     f"({share['own_pct']:g} pts in {share['runs_in_cycle']} run(s), {share['run_hours']:g} h) / "
                     f"user {_fmt_share(share['other_share'])} ({share['user_pct']:g} pts, of which "
                     f"{share['user_unseen_pct']:g} with no local tokens: other devices / claude.ai)")
    tok = snap["attribution"].get("week_share_tokens") or {}
    if tok.get("status") != "ok":
        reason = tok.get("missing_side_rate") or tok.get("reason") or "?"
        lines.append(f"  token split by session: insufficient data ({reason})")
    else:
        lines.append(f"  token split by session (secondary): AFClaude sessions {_fmt_share(tok['own_share'])} / "
                     f"other sessions {_fmt_share(tok['other_share'])} (n_pairs={tok['n_pairs']}; the owner's turns "
                     f"in AFClaude sessions count as AFClaude here)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", default=SAMPLES)
    ap.add_argument("--trimmed-days", type=int, default=DEFAULT_TRIMMED_DAYS)
    ap.add_argument("--windows", default=SESSION_WINDOWS, help="per-session-window file (never pruned)")
    ap.add_argument("--backfill-windows", action="store_true",
                    help="rebuild --windows from --samples (keeps stored windows the samples no longer cover)")
    ap.add_argument("--runs", default=None, help="AFClaude runs file (default: run_metrics.RUNS_FILE)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.backfill_windows:
        allw = backfill_windows(args.samples, args.windows)
        use = [w for w in allw if w.get("usable")]
        print(f"{args.windows}: {len(allw)} windows, {len(use)} usable")
        for w in allw:
            r = w.get("ratio")
            print(f"  {w['window_end'][:16]}  ds={w['d_session']:>5g} dw={w['d_weekly']:>4g}  "
                  f"ratio={'n/a' if r is None else f'{r:.4f}'}"
                  f" [{_fmt_ratio(w.get('ratio_lo'))}..{_fmt_ratio(w.get('ratio_hi'))}]"
                  f"  cov={w['coverage_frac']:.0%} {','.join(w['flags'])}")
        return
    snap = compute_from_file(args.samples, trimmed_days=args.trimmed_days, windows_path=args.windows,
                             runs_path=args.runs)
    if args.json:
        print(json.dumps(snap, indent=1, default=str))
    else:
        print(human_summary(snap))


if __name__ == "__main__":
    main()
