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


class TimeSplit(unittest.TestCase):
    """AFClaude vs user by time: weekly % risen during autonomous runs is AFClaude's, all else the user's."""
    WR = lr._round_reset(WEEK_RESET2)            # cycle 01.10. 17:00 .. 08.10. 17:00 UTC
    START = WR - timedelta(days=7)

    def r(self, h, wp, own=0, other=0, stale=False):
        x = row(self.START + timedelta(hours=h), 0, wp, week_reset=WEEK_RESET2,
                own=own_tok(out=own) if own else None, other=own_tok(out=other) if other else None)
        if stale:
            x["usage"]["stale"] = True
        return x

    def split(self, rows, spans, now_h=200):
        return lr.estimate_time_split(rows, spans, self.WR, now=self.START + timedelta(hours=now_h))

    def span(self, h0, h1):
        return (self.START + timedelta(hours=h0), self.START + timedelta(hours=h1))

    def test_owner_chat_in_afclaude_session_without_a_run_is_user_usage(self):
        # all tokens in the AFClaude session (activity.own), no autonomous run: 100% user (D-018)
        rows = [self.r(1, 5, own=1000), self.r(2, 12, own=2000), self.r(3, 20, own=500)]
        s = self.split(rows, [])
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["method"], "time")
        self.assertAlmostEqual(s["own_share"], 0.0)
        self.assertAlmostEqual(s["user_pct"], 20.0)       # incl. the rise from 0 at the reset
        # the token split by session would have called it 100% AFClaude
        self.assertEqual(s["user_unseen_pct"], 0.0)

    def test_rise_during_a_run_is_afclaude(self):
        rows = [self.r(1, 10, other=100), self.r(2, 10), self.r(3, 25, own=3000), self.r(4, 30, other=100)]
        s = self.split(rows, [self.span(2, 3)])
        self.assertAlmostEqual(s["own_pct"], 15.0)
        self.assertAlmostEqual(s["user_pct"], 15.0)
        self.assertAlmostEqual(s["own_share"], 0.5)
        self.assertEqual(s["runs_in_cycle"], 1)

    def test_partial_overlap_is_proportional(self):
        rows = [self.r(1, 10), self.r(2, 20, own=1000)]
        # run covers the first half of (1h, 2h], minus the end grace: 1:00..1:20 + 10 min grace = 1:30
        s = self.split(rows, [self.span(1, 1 + 20 / 60)])
        self.assertAlmostEqual(s["own_pct"], 5.0)
        self.assertAlmostEqual(s["user_pct"], 15.0)

    def test_parallel_user_tokens_during_a_run_go_to_the_user(self):
        rows = [self.r(1, 10), self.r(2, 20, own=3000, other=1000)]
        s = self.split(rows, [self.span(1, 3)])
        self.assertAlmostEqual(s["own_pct"], 7.5)
        self.assertAlmostEqual(s["user_pct"], 12.5)

    def test_rise_across_stale_readings_and_without_tokens_is_user(self):
        rows = [self.r(1, 10, other=50), self.r(5, 10, stale=True), self.r(30, 99, stale=True),
                self.r(40, 18), self.r(41, 19)]
        s = self.split(rows, [])
        self.assertAlmostEqual(s["user_pct"], 19.0)       # the stale gap's +8 still counted
        self.assertAlmostEqual(s["user_unseen_pct"], 9.0)  # 10->18 and 18->19: no tokens on this host
        self.assertEqual(s["weekly_pct"], 19.0)

    def test_stale_row_tokens_still_count_for_the_interval(self):
        rows = [self.r(1, 10, other=10), self.r(2, 10, other=500, stale=True), self.r(3, 14)]
        s = self.split(rows, [])
        self.assertEqual(s["user_unseen_pct"], 0.0)

    def test_previous_cycle_rows_and_future_rows_ignored(self):
        old = row(self.START - timedelta(hours=1), 0, 95, week_reset=WEEK_RESET)
        rows = [old, self.r(1, 4), self.r(300, 50)]
        s = self.split(rows, [], now_h=10)
        self.assertAlmostEqual(s["user_pct"], 4.0)

    def test_unknown_spans_or_no_rise(self):
        self.assertEqual(self.split([self.r(1, 5)], None)["status"], "insufficient_data")
        self.assertEqual(self.split([self.r(1, 0)], [])["status"], "insufficient_data")
        self.assertEqual(lr.estimate_time_split([], [], None)["status"], "insufficient_data")


class CombineSpans(unittest.TestCase):
    def test_runs_fallback_and_merge(self):
        now = T0 + timedelta(hours=10)
        runs = [{"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat(), "ongoing": False},
                {"start": (T0 + timedelta(hours=8)).isoformat(), "end": (T0 + timedelta(hours=8)).isoformat(),
                 "ongoing": True}]
        fallback = [(T0 + timedelta(minutes=1), T0 + timedelta(hours=10)),      # the 2nd fire of run 1: covered
                    (T0 + timedelta(hours=3), T0 + timedelta(hours=3, minutes=30)),   # a usage-review run
                    (T0 + timedelta(hours=11), T0 + timedelta(hours=12))]     # after now: ignored
        got = lr.combine_spans(runs, fallback, now)
        self.assertEqual(got, [(T0, T0 + timedelta(hours=1)),
                               (T0 + timedelta(hours=3), T0 + timedelta(hours=3, minutes=30)),
                               (T0 + timedelta(hours=8), now)])          # ongoing: until now

    def test_overlapping_spans_merge(self):
        got = lr.combine_spans([], [(T0, T0 + timedelta(hours=2)), (T0 + timedelta(hours=1), T0 + timedelta(hours=3))])
        self.assertEqual(got, [(T0, T0 + timedelta(hours=3))])


class ComputeSnapshot(unittest.TestCase):
    def test_compute_reports_time_split_and_keeps_token_split(self):
        rows = make_series(lr.MIN_PAIRS + 5, step_session=2.0, step_weekly=0.2, own_out=1000)
        snap = lr.compute(rows, now=T0 + timedelta(days=1), spans=[])
        att = snap["attribution"]
        self.assertEqual(att["week_share"]["method"], "time")
        self.assertEqual(att["week_share"]["status"], "ok")
        self.assertAlmostEqual(att["week_share"]["other_share"], 1.0)   # no runs: all user
        self.assertIn("week_share_tokens", att)
        self.assertIn("AFClaude runs vs user", lr.human_summary(snap))
        snap = lr.compute(rows, now=T0 + timedelta(days=1))              # spans unknown
        self.assertEqual(snap["attribution"]["week_share"]["status"], "insufficient_data")

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


# ------------------------------------------------------------------ per-session-window

W_END = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)      # window 12:00..17:00
W_START = W_END - timedelta(hours=5)


def srow(t, sp, wp, session_reset=None, week_reset=WEEK_RESET, own=None, other=None):
    """Like row(), but session_reset=None means idle (no window open, 0%)."""
    r = row(t, sp, wp, session_reset=session_reset or SESSION_RESET, week_reset=week_reset, own=own, other=other)
    if session_reset is None:
        r["usage"]["session"] = {"percent": 0.0, "resets_at": None}
    return r


def window_rows(seq, start=W_START, end=W_END, step=15, week_reset=WEEK_RESET, idle_before=True, own=None):
    """Samples every `step` min from start+step..end-step, readings from seq(i)."""
    rows = [srow(start - timedelta(minutes=10), 0, seq(0)[1], week_reset=week_reset)] if idle_before else []
    i = 1
    t = start + timedelta(minutes=step)
    while t < end:
        sp, wp = seq(i)
        rows.append(srow(t, sp, wp, session_reset=end.isoformat(), week_reset=week_reset, own=own))
        i += 1
        t += timedelta(minutes=step)
    return rows


def linear(ds_total, w0, dw_total, n=19):
    """session rises to ds_total and weekly from w0 by dw_total, integer readings."""
    return lambda i: (float(round(ds_total * min(i, n) / n)), float(w0 + round(dw_total * min(i, n) / n)))


class PerWindow(unittest.TestCase):
    def one(self, rows, now=None):
        ws = lr.build_windows(rows, now=now or W_END + timedelta(minutes=5))
        self.assertEqual(len(ws), 1)
        return ws[0]

    def test_whole_window_deltas_idle_baseline(self):
        w = self.one(window_rows(linear(40, 30, 6), own=own_tok(out=100)))
        self.assertEqual(w["window_end"], W_END.isoformat())
        self.assertEqual(w["window_start"], W_START.isoformat())
        self.assertEqual(w["baseline"], "idle_pre_sample")
        self.assertEqual((w["d_session"], w["d_weekly"]), (40.0, 6.0))
        self.assertEqual(w["session_end_pct"], 40.0)
        self.assertAlmostEqual(w["ratio"], 0.15)
        # exact 0% session at the idle baseline: only the end reading is rounded
        self.assertEqual((w["err_session"], w["err_weekly"]), (0.5, 1.0))
        self.assertTrue(w["usable"])
        self.assertEqual(w["flags"], [])
        self.assertEqual(w["own_share"], 1.0)
        self.assertIsNone(w["exclude_reason"])

    def test_rounding_interval(self):
        # baseline is the first in-window sample (no idle sample before): +-1 on both deltas
        w = self.one(window_rows(lambda i: (float(i), 40.0 + (3 if i >= 19 else 0)), idle_before=False))
        self.assertEqual(w["baseline"], "first_sample")
        self.assertEqual((w["d_session"], w["d_weekly"]), (18.0, 3.0))
        self.assertEqual((w["err_session"], w["err_weekly"]), (1.0, 1.0))
        self.assertAlmostEqual(w["ratio_lo"], 2 / 19)
        self.assertAlmostEqual(w["ratio_hi"], 4 / 17)
        self.assertLessEqual(w["ratio_lo"], w["ratio"])
        self.assertGreaterEqual(w["ratio_hi"], w["ratio"])
        self.assertEqual(lr._ratio_interval(0, 10, 1, 1), (0.0, 1 / 9))
        self.assertEqual(lr._ratio_interval(1, 0.5, 1, 1)[1], None)   # ds within its error: unbounded

    def test_idle_baseline_requires_same_cycle_and_recency(self):
        rows = window_rows(linear(40, 30, 6))
        rows[0] = srow(W_START - timedelta(hours=6), 0, 30)          # too old: a window could hide in between
        self.assertEqual(self.one(rows)["baseline"], "first_sample")
        rows[0] = srow(W_START - timedelta(minutes=10), 0, 30, week_reset=WEEK_RESET2)  # other weekly cycle
        self.assertEqual(self.one(rows)["baseline"], "first_sample")

    def test_back_to_back_windows_use_first_sample(self):
        prev_end = W_START
        rows = [srow(prev_end - timedelta(minutes=15), 50, 30, session_reset=prev_end.isoformat())]
        rows += window_rows(linear(40, 31, 6), idle_before=False)
        ws = lr.build_windows(rows, now=W_END + timedelta(minutes=5))
        w = [x for x in ws if x["window_end"] == W_END.isoformat()][0]
        self.assertEqual(w["baseline"], "first_sample")

    def test_partial_window_flagged_and_excluded(self):
        rows = [r for r in window_rows(linear(40, 30, 6)) if lr._parse_ts(r["at"]) >= W_END - timedelta(hours=2)]
        w = self.one(rows)
        self.assertIn("partial", w["flags"])
        self.assertFalse(w["usable"])
        est = lr.estimate_window_ratio([w])
        self.assertEqual(est["status"], "insufficient_data")
        self.assertEqual(est["excluded"], {"partial": 1})

    def test_gap_is_flagged_but_deltas_still_valid(self):
        rows = window_rows(linear(40, 30, 6))
        gap0, gap1 = W_START + timedelta(hours=1), W_START + timedelta(hours=2, minutes=30)
        rows = [r for r in rows if not (gap0 < lr._parse_ts(r["at"]) < gap1)]
        w = self.one(rows)
        self.assertIn("gap", w["flags"])
        self.assertTrue(w["usable"])                 # cumulative meters: endpoints still valid
        self.assertEqual((w["d_session"], w["d_weekly"]), (40.0, 6.0))
        self.assertGreaterEqual(w["max_gap_min"], 90)

    def test_low_activity_excluded(self):
        w = self.one(window_rows(linear(3, 30, 0)))
        self.assertIn("low_activity", w["flags"])
        self.assertFalse(w["usable"])

    def test_open_window_left_out(self):
        rows = window_rows(linear(40, 30, 6))
        self.assertEqual(lr.build_windows(rows, now=W_END - timedelta(minutes=30)), [])
        ws = lr.build_windows(rows, now=W_END - timedelta(minutes=30), include_open=True)
        self.assertIn("open", ws[0]["flags"])
        self.assertFalse(ws[0]["usable"])
        # default "now" = the latest sample, which is inside the window
        self.assertEqual(lr.build_windows(rows), [])

    def test_capped_window_cut_before_saturation(self):
        # weekly hits 100 at sample 10 and stays there while session keeps rising
        seq = lambda i: (float(5 * i), float(min(100, 90 + i)))   # noqa: E731
        w = self.one(window_rows(seq))
        self.assertIn("capped", w["flags"])
        self.assertEqual(w["weekly_end_pct"], 99.0)
        self.assertEqual((w["d_session"], w["d_weekly"]), (45.0, 9.0))
        self.assertTrue(w["usable"])                 # cut on purpose, not partial

    def test_cross_weekly_reset_split(self):
        # weekly cycle resets at 14:30 inside the 12:00..17:00 window
        reset = datetime(2026, 9, 26, 14, 30, tzinfo=UTC)
        rows = [srow(W_START - timedelta(minutes=10), 0, 90, week_reset=reset.isoformat())]
        t, i = W_START + timedelta(minutes=15), 1
        while t < W_END:
            if t < reset:
                rows.append(srow(t, 4.0 * i, 90 + (4 * i) // 6, session_reset=W_END.isoformat(), week_reset=reset.isoformat()))
            else:
                rows.append(srow(t, 4.0 * i, float((4 * i - 40) // 6), session_reset=W_END.isoformat(),
                                 week_reset=WEEK_RESET2))
            t += timedelta(minutes=15)
            i += 1
        w = self.one(rows)
        self.assertIn("weekly_reset_split", w["flags"])
        self.assertEqual(len(w["segments"]), 2)
        s1, s2 = w["segments"]
        # segment 1: idle 0 -> 9 samples*4 = 36% session; segment 2 from 40% to 76%
        self.assertEqual(s1["d_session"], 36.0)
        self.assertEqual(s2["d_session"], 36.0)
        self.assertEqual(w["d_session"], s1["d_session"] + s2["d_session"])
        self.assertEqual(w["d_weekly"], s1["d_weekly"] + s2["d_weekly"])
        # the straddling 15 min is dropped, rounding counts both segments' readings
        self.assertEqual(w["err_weekly"], 2.0)
        self.assertEqual(w["err_session"], 0.5 + 1.0)
        self.assertLess(w["covered_min"], 300 - 15 + 1)
        self.assertTrue(w["usable"])

    def test_samples_without_meters_are_skipped(self):
        rows = window_rows(linear(40, 30, 6))
        rows.insert(5, {"at": rows[4]["at"], "usage": {}})
        self.assertEqual(self.one(rows)["d_session"], 40.0)


def wrec(ds, dw, end="2026-09-26T17:00:00+00:00", usable=True):
    return {"window_end": end, "d_session": float(ds), "d_weekly": float(dw), "ratio": dw / ds if ds else None,
            "err_session": 1.0, "err_weekly": 1.0, "rounding_var_session": 2 / 12, "rounding_var_weekly": 2 / 12,
            "usable": usable, "flags": [] if usable else ["low_activity"],
            "exclude_reason": None if usable else "low activity: x"}


class WindowEstimate(unittest.TestCase):
    NOW = datetime(2026, 9, 30, tzinfo=UTC)

    def test_weighted_by_d_session(self):
        ws = [wrec(10, 1), wrec(90, 15)]
        est = lr.estimate_window_ratio(ws, now=self.NOW)
        self.assertEqual(est["status"], "low_n")          # < MIN_WINDOWS
        self.assertAlmostEqual(est["weighted"], 16 / 100)   # not the plain mean (0.1333)
        self.assertAlmostEqual(est["mean"], (0.1 + 15 / 90) / 2)
        self.assertEqual(est["n"], 2)

    def test_dispersion_and_rounding(self):
        ws = [wrec(20, 2), wrec(40, 6), wrec(50, 8), wrec(60, 9), wrec(30, 6), wrec(3, 2, usable=False)]
        est = lr.estimate_window_ratio(ws, now=self.NOW)
        self.assertEqual(est["status"], "ok")
        self.assertEqual((est["n"], est["n_total"]), (5, 6))
        self.assertEqual(est["excluded"], {"low activity": 1})
        ratios = sorted([0.1, 0.15, 0.16, 0.15, 0.2])
        self.assertAlmostEqual(est["weighted"], 31 / 200)
        self.assertAlmostEqual(est["median"], 0.15)
        self.assertAlmostEqual(est["p25"], 0.15)
        self.assertAlmostEqual(est["p75"], 0.16)
        self.assertAlmostEqual(est["iqr"], 0.01)
        self.assertAlmostEqual(est["stdev"], statistics_stdev(ratios))
        self.assertEqual((est["min"], est["max"]), (0.1, 0.2))
        self.assertGreater(est["se"], 0)
        lo, hi = est["rounding_worst"]
        self.assertAlmostEqual(lo, 26 / 205)
        self.assertAlmostEqual(hi, 36 / 195)
        self.assertGreater(est["rounding_sd"], 0)
        self.assertLess(est["rounding_sd"], hi - lo)
        self.assertGreaterEqual(est["intrinsic_stdev"], 0)
        self.assertEqual(len(est["distribution"]), 5)

    def test_vs_15min_and_preferred(self):
        rows = make_series(lr.MIN_PAIRS + 5)
        pairs = lr.build_pairs(rows)
        ws = [wrec(40, 6)] * 5
        est = lr.estimate_window_ratio(ws, pairs=pairs, now=self.NOW)
        self.assertAlmostEqual(est["vs_15min"]["median_15min"], 0.2)
        self.assertAlmostEqual(est["vs_15min"]["factor_vs_median"], 0.15 / 0.2)
        old = lr.estimate_ratio(pairs, now=T0 + timedelta(days=1))
        p = lr.preferred_ratio({"ratio": old, "ratio_windows": est})
        self.assertEqual((p["source"], p["flagged"]), ("windows", False))
        self.assertAlmostEqual(p["value"], 0.15)
        p = lr.preferred_ratio({"ratio": old, "ratio_windows": lr.estimate_window_ratio(ws[:2], now=self.NOW)})
        self.assertEqual((p["source"], p["flagged"]), ("15min_median", True))
        self.assertAlmostEqual(p["value"], 0.2)
        p = lr.preferred_ratio({"ratio": {"status": "insufficient_data"}, "ratio_windows": {}})
        self.assertIsNone(p["value"])

    def test_compute_snapshot_keeps_old_api_and_adds_windows(self):
        rows = window_rows(linear(40, 30, 6))
        snap = lr.compute(rows, now=W_END + timedelta(minutes=5))
        for k in ("status", "n"):
            self.assertIn(k, snap["ratio"])
        for k in ("generated_at", "windows_per_week", "windows_left_this_week", "weekly_pct_now", "attribution"):
            self.assertIn(k, snap)
        self.assertEqual(snap["ratio_windows"]["n"], 1)
        self.assertAlmostEqual(snap["ratio_windows"]["weighted"], 0.15)
        self.assertIn("preferred_ratio", snap)
        self.assertIn("Per-session-window ratio", lr.human_summary(snap))
        # stored windows win over derived ones, unknown stored ones are kept
        stored = [dict(wrec(50, 10, end=W_END.isoformat())), wrec(50, 5, end="2026-09-20T10:00:00+00:00")]
        snap = lr.compute(rows, now=W_END + timedelta(minutes=5), windows=stored)
        self.assertEqual(snap["ratio_windows"]["n"], 2)
        self.assertAlmostEqual(snap["ratio_windows"]["weighted"], 15 / 100)


class WindowFile(unittest.TestCase):
    def test_append_dedupes_and_backfill_keeps_pruned(self):
        import json
        import tempfile
        rows = window_rows(linear(40, 30, 6))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session_windows.jsonl")
            now = W_END + timedelta(minutes=5)
            allw, new = lr.append_new_windows(rows, path=path, now=now)
            self.assertEqual((len(allw), len(new)), (1, 1))
            self.assertEqual(new[0]["source"], "sampler")
            allw, new = lr.append_new_windows(rows, path=path, now=now)
            self.assertEqual((len(allw), len(new)), (1, 0))
            # a window from long ago that samples.jsonl no longer has
            with open(path, "a") as fh:
                fh.write(json.dumps(wrec(50, 5, end="2026-09-01T10:00:00+00:00")) + "\n")
            sp = os.path.join(d, "samples.jsonl")
            with open(sp, "w") as fh:
                for r in rows + [srow(W_END + timedelta(minutes=5), 0, 36)]:
                    fh.write(json.dumps(r) + "\n")
            out = lr.backfill_windows(sp, path)
            self.assertEqual([w["window_end"][:10] for w in out], ["2026-09-01", "2026-09-26"])
            self.assertEqual(out[1]["source"], "backfill")
            self.assertEqual(len(lr.load_windows(path)), 2)


def statistics_stdev(xs):
    import statistics
    return statistics.stdev(xs)


if __name__ == "__main__":
    unittest.main()
