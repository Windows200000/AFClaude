#!/usr/bin/env python3
"""Offline tests for limit_ratio.py: synthetic sample rows only, no real data."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limit_ratio as lr  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
SESSION_RESET = "2026-09-26T17:00:00.000000+00:00"
WEEK_RESET = "2026-10-01T17:00:00.000000+00:00"
WEEK_RESET2 = "2026-10-08T17:00:00.000000+00:00"


def row(t, sp, wp, session_reset=SESSION_RESET, week_reset=WEEK_RESET, own=None, other=None, jitter=0):
    """One synthetic sample row. `jitter` (seconds) simulates the ms/second
    wobble real resets_at values have between fetches."""
    def wob(iso):
        d = datetime.fromisoformat(iso) + timedelta(seconds=jitter)
        return d.isoformat()
    activity = {"own": {"tokens": own or {}}, "other": {"tokens": other or {}}}
    return {
        "at": t.isoformat(),
        "usage": {
            "session": {"percent": sp, "resets_at": wob(session_reset)},
            "weekly": {"percent": wp, "resets_at": wob(week_reset)},
        },
        "activity": activity,
    }


def own_tok(out=0, think=0, cache_w=0, cache_r=0):
    return {"model-x": {"out": out, "think": think, "cache_w": cache_w, "cache_r": cache_r}}


class RoundReset(unittest.TestCase):
    def test_jitter_within_a_minute_rounds_the_same(self):
        a = lr._round_reset("2026-10-01T16:59:59.780471+00:00")
        b = lr._round_reset("2026-10-01T17:00:00.102489+00:00")
        self.assertEqual(a, b)

    def test_none_on_missing(self):
        self.assertIsNone(lr._round_reset(None))


class WeightedTokens(unittest.TestCase):
    def test_weights_out_think_cache(self):
        tk = {"m": {"out": 100, "think": 50, "cache_w": 40, "cache_r": 1000}}
        got = lr.weighted_tokens(tk)
        expect = 100 * lr.OUT_W + 50 * lr.THINK_W + 40 * lr.CACHE_WRITE_W + 1000 * lr.CACHE_READ_W
        self.assertAlmostEqual(got, expect)

    def test_empty(self):
        self.assertEqual(lr.weighted_tokens(None), 0)
        self.assertEqual(lr.weighted_tokens({}), 0)


class BuildPairs(unittest.TestCase):
    def test_simple_pair(self):
        rows = [row(T0, 10, 40), row(T0 + timedelta(minutes=15), 15, 41)]
        pairs = lr.build_pairs(rows)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertAlmostEqual(p["d_session"], 5)
        self.assertAlmostEqual(p["d_weekly"], 1)

    def test_reset_boundary_is_skipped(self):
        """A session reset between two samples (resets_at jumps to a new,
        materially different window) must not produce a pair."""
        r1 = row(T0, 95, 40)
        r2 = row(T0 + timedelta(minutes=15), 3, 40,
                 session_reset="2026-09-26T22:00:00.000000+00:00")  # new session window
        pairs = lr.build_pairs([r1, r2])
        self.assertEqual(pairs, [])

    def test_weekly_reset_boundary_is_skipped(self):
        r1 = row(T0, 10, 99, week_reset=WEEK_RESET)
        r2 = row(T0 + timedelta(minutes=15), 12, 2, week_reset=WEEK_RESET2)
        pairs = lr.build_pairs([r1, r2])
        self.assertEqual(pairs, [])

    def test_ms_jitter_on_resets_at_does_not_break_pairing(self):
        r1 = row(T0, 10, 40, jitter=0)
        r2 = row(T0 + timedelta(minutes=15), 15, 41, jitter=0.6)
        pairs = lr.build_pairs([r1, r2])
        self.assertEqual(len(pairs), 1)

    def test_missing_usage_breaks_the_chain(self):
        r1 = row(T0, 10, 40)
        bad = {"at": (T0 + timedelta(minutes=15)).isoformat(), "usage": {}}
        r2 = row(T0 + timedelta(minutes=30), 20, 42)
        pairs = lr.build_pairs([r1, bad, r2])
        self.assertEqual(pairs, [])  # r1->bad skipped (bad has no usage); bad->r2 skipped (prev reset to None)


def make_series(n, step_session=2.0, step_weekly=0.2, own_out=0, other_out=0,
                 start=T0, week_reset=WEEK_RESET, session_reset=SESSION_RESET):
    rows = []
    for i in range(n + 1):
        t = start + timedelta(minutes=15 * i)
        sp = min(100, i * step_session)
        wp = min(100, i * step_weekly * step_session)  # keep weekly moving with session for a clean ratio
        own = own_tok(out=own_out) if (own_out and i > 0) else {}
        other = own_tok(out=other_out) if (other_out and i > 0) else {}
        rows.append(row(t, sp, wp, session_reset=session_reset, week_reset=week_reset, own=own, other=other))
    return rows


class EstimateRatio(unittest.TestCase):
    def test_insufficient_data_below_min_pairs(self):
        rows = make_series(lr.MIN_PAIRS - 2)
        pairs = lr.build_pairs(rows)
        est = lr.estimate_ratio(pairs, now=T0 + timedelta(days=1))
        self.assertEqual(est["status"], "insufficient_data")

    def test_ok_above_min_pairs_and_ratio_value(self):
        # step_session=2, step_weekly=0.2 -> weekly moves 0.2*2=0.4 per step
        # -> ratio = d_weekly/d_session = 0.4/2 = 0.2 for every pair
        rows = make_series(lr.MIN_PAIRS + 5, step_session=2.0, step_weekly=0.2)
        pairs = lr.build_pairs(rows)
        est = lr.estimate_ratio(pairs, now=T0 + timedelta(days=1))
        self.assertEqual(est["status"], "ok")
        self.assertGreaterEqual(est["n"], lr.MIN_PAIRS)
        self.assertAlmostEqual(est["median"], 0.2, places=6)
        self.assertAlmostEqual(est["trimmed_mean"], 0.2, places=6)

    def test_trimmed_mean_insufficient_recent_data(self):
        rows = make_series(lr.MIN_PAIRS + 5, step_session=2.0, step_weekly=0.2)
        pairs = lr.build_pairs(rows)
        # "now" far in the future -> nothing falls inside the trimmed window
        est = lr.estimate_ratio(pairs, now=T0 + timedelta(days=365), trimmed_days=1)
        self.assertEqual(est["status"], "ok")   # median still fine
        self.assertIsNotNone(est["median"])
        self.assertIsNone(est["trimmed_mean"])

    def test_windows_per_week_and_left(self):
        self.assertAlmostEqual(lr.windows_per_week(0.1), 10.0)
        self.assertAlmostEqual(lr.windows_left_this_week(0.1, 50.0), 5.0)
        self.assertAlmostEqual(lr.windows_left_this_week(0.1, 100.0), 0.0)
        self.assertIsNone(lr.windows_per_week(None))
        self.assertIsNone(lr.windows_left_this_week(None, 50.0))


class Attribution(unittest.TestCase):
    def test_single_side_intervals_give_a_clean_rate(self):
        # own-only activity, session% rises 2 per step -> rate = 2/tokens
        rows = make_series(lr.MIN_PAIRS + 3, step_session=2.0, step_weekly=0.2, own_out=1000)
        pairs = lr.build_pairs(rows)
        rates = lr.estimate_side_rates(pairs)
        self.assertIsNotNone(rates["own"]["rate"])
        self.assertAlmostEqual(rates["own"]["rate"], 2.0 / 1000, places=9)
        self.assertIsNone(rates["other"]["rate"])  # never active -> no clean pairs at all

    def test_insufficient_clean_pairs_below_threshold(self):
        rows = make_series(lr.MIN_PAIRS - 3, step_session=2.0, step_weekly=0.2, own_out=1000)
        pairs = lr.build_pairs(rows)
        rates = lr.estimate_side_rates(pairs)
        self.assertIsNone(rates["own"]["rate"])
        self.assertIn("note", rates["own"])

    def test_mixed_activity_pair_is_not_a_clean_measurement(self):
        rows = [row(T0, 0, 0)]
        for i in range(1, lr.MIN_PAIRS + 2):
            t = T0 + timedelta(minutes=15 * i)
            rows.append(row(t, i * 2.0, i * 0.2, own=own_tok(out=500), other=own_tok(out=500)))
        pairs = lr.build_pairs(rows)
        rates = lr.estimate_side_rates(pairs)
        # both sides active every interval -> zero clean single-side pairs for either side
        self.assertEqual(rates["own"]["n"], 0)
        self.assertEqual(rates["other"]["n"], 0)

    def test_weekly_share_ok_when_both_rates_known(self):
        n = lr.MIN_PAIRS + 3
        own_rows = make_series(n, step_session=2.0, step_weekly=0.2, own_out=1000)
        pairs = lr.build_pairs(own_rows)
        # synthesize an "other" rate directly rather than a second full series
        rates = {"own": {"rate": 0.002, "n": lr.MIN_PAIRS}, "other": {"rate": 0.004, "n": lr.MIN_PAIRS}}
        wr = pairs[-1]["weekly_resets"]
        share = lr.estimate_weekly_share(pairs, rates, wr)
        self.assertEqual(share["status"], "ok")
        self.assertAlmostEqual(share["own_share"] + share["other_share"], 1.0, places=9)
        self.assertGreater(share["own_share"], 0)

    def test_weekly_share_insufficient_when_one_rate_missing(self):
        rows = make_series(lr.MIN_PAIRS + 3, step_session=2.0, step_weekly=0.2, own_out=1000)
        pairs = lr.build_pairs(rows)
        rates = lr.estimate_side_rates(pairs)  # other rate will be None (never active)
        wr = pairs[-1]["weekly_resets"]
        share = lr.estimate_weekly_share(pairs, rates, wr)
        self.assertEqual(share["status"], "insufficient_data")
        self.assertIn("other", share["missing_side_rate"])


class ComputeSnapshot(unittest.TestCase):
    def test_compute_end_to_end_insufficient(self):
        rows = make_series(3)
        snap = lr.compute(rows, now=T0 + timedelta(days=1))
        self.assertEqual(snap["ratio"]["status"], "insufficient_data")
        self.assertIsNone(snap["windows_per_week"])
        self.assertEqual(snap["attribution"]["week_share"]["status"], "insufficient_data")

    def test_compute_end_to_end_ok(self):
        rows = make_series(lr.MIN_PAIRS + 5, step_session=2.0, step_weekly=0.2, own_out=1000)
        snap = lr.compute(rows, now=T0 + timedelta(days=1))
        self.assertEqual(snap["ratio"]["status"], "ok")
        self.assertAlmostEqual(snap["windows_per_week"], 5.0, places=3)  # 1/0.2
        self.assertIsNotNone(snap["weekly_pct_now"])

    def test_empty_rows(self):
        snap = lr.compute([], now=T0)
        self.assertEqual(snap["ratio"]["status"], "insufficient_data")
        self.assertIsNone(snap["weekly_pct_now"])


class HumanSummary(unittest.TestCase):
    def test_runs_on_insufficient_and_ok(self):
        snap = lr.compute(make_series(3), now=T0 + timedelta(days=1))
        text = lr.human_summary(snap)
        self.assertIn("insufficient data", text)
        snap2 = lr.compute(make_series(lr.MIN_PAIRS + 5, own_out=1000), now=T0 + timedelta(days=1))
        text2 = lr.human_summary(snap2)
        self.assertIn("full session windows fit", text2)


if __name__ == "__main__":
    unittest.main()
