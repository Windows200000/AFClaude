#!/usr/bin/env python3
"""Offline tests for pacing.py (temp files only, never the real data/)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AFCLAUDE_CONFIG"] = os.devnull   # hermetic: the code defaults, not a local data/afclaude.json
import afclaude_config  # noqa: E402
import pacing as pm  # noqa: E402

UTC = timezone.utc
WIN = (dtime(23, 0), dtime(9, 0))                 # the default window, Berlin wall clock


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


R = Z("2026-10-08T17:00:00Z")                     # Thu 08.10.2026 19:00 Berlin, a weekly reset
SUN = Z("2026-10-04T21:00:00Z")                   # Sun 23:00 Berlin, a window start


def flat_fc(rate=0.2):
    return lambda a, b: rate * (b - a).total_seconds() / 3600


def core(w, now, msu=600.0, ratio=0.125, known=True, P=None, s_pct=None, s_reset=None,
         lm="auto", fc=None, thr="auto"):
    return pm.decide_core(w, R, now, msu, P, s_pct, s_reset, known, ratio, lm, WIN, thr, fc)


def row(at, w=None, reset=R, own=None, other=None, ratio=None):
    r = {"at": at.isoformat(), "activity": {"own": own or {}, "other": other or {}}}
    if w is not None:
        r["usage"] = {"weekly": {"percent": w, "resets_at": reset.isoformat()}}
    if ratio is not None:
        r["limit_ratio"] = {"ratio": ratio}
    return r


class LastStretch(unittest.TestCase):
    """The last stretch = the final min(ceil(sessions_left), 2) session windows before the reset."""

    def test_hours(self):
        self.assertEqual(pm.last_mile_hours(95, 0.125), 5.0)     # 0.4 -> 1 window
        self.assertEqual(pm.last_mile_hours(90, 0.1), 5.0)       # 1.0 -> 1
        self.assertEqual(pm.last_mile_hours(88, 0.125), 5.0)     # 0.96 -> 1
        self.assertEqual(pm.last_mile_hours(85, 0.125), 10.0)    # 1.2 -> 2
        self.assertEqual(pm.last_mile_hours(80, 0.1), 10.0)      # 2.0 -> 2
        self.assertEqual(pm.last_mile_hours(50, 0.125), 10.0)    # 4 -> capped at 2
        self.assertEqual(pm.last_mile_hours(50, 0.1), 10.0)      # 5 -> capped at 2
        self.assertEqual(pm.last_mile_hours(100, 0.125), 0.0)
        self.assertEqual(pm.last_mile_hours(50, 0.125, 3), 3.0)   # numeric override
        self.assertEqual(pm.last_mile_hours(50, 0.125, 0), 0.0)   # off

    def test_boundary_in_the_decision(self):
        for w, ratio, hours in ((90, 0.125, 5), (80, 0.1, 10)):
            start = R - timedelta(hours=hours)
            d = core(w, start, ratio=ratio)
            self.assertEqual(d["mode"], "last_mile", d["reason"])
            self.assertTrue(d["go"], d["reason"])
            self.assertEqual(d["headroom"], 100.0 - w)
            self.assertEqual(d["session_cap"], 100.0)
            self.assertEqual(d["last_mile_start"], start)
            before = core(w, start - timedelta(minutes=1), ratio=ratio)
            self.assertNotEqual(before["mode"], "last_mile")

    def test_yield_and_holds(self):
        now = R - timedelta(hours=3)
        d = core(90, now, msu=20.0)                                       # yields by default
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], now + timedelta(minutes=40))
        nyp = {"last_mile_yield": False}
        self.assertTrue(core(90, now, msu=20.0, P=nyp)["go"])
        self.assertTrue(core(90, now, known=False, msu=None, P=nyp)["go"])
        self.assertTrue(core(90, now, s_pct=95, s_reset=now + timedelta(hours=1))["go"])   # cap 100
        d = core(90, now, s_pct=100, s_reset=now + timedelta(hours=1))
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], now + timedelta(hours=1))
        d = core(100, now)
        self.assertFalse(d["go"])
        self.assertIn("exhausted", d["reason"])
        self.assertFalse(d["postpone"])
        self.assertTrue(core(None, now)["postpone"])

    def test_dst_reset(self):
        r = Z("2026-10-25T18:00:00Z")          # Sun 25.10. 19:00 CET, the day the clocks go back
        d = pm.decide_core(85, r, r - timedelta(hours=9), 600.0, None, None, None, True, 0.125, "auto", WIN)
        self.assertEqual(d["last_mile_start"], Z("2026-10-25T08:00:00Z"))   # 10 real hours
        self.assertEqual(d["mode"], "last_mile")


class Ratio(unittest.TestCase):
    def test_limit_ratio_median_and_fallback(self):
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "ok", "median": 0.118, "n": 52})]
        self.assertEqual(pm.ratio_info(rows), (0.118, "limit_ratio median, n=52"))
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "insufficient_data", "n": 3})]
        ratio, src = pm.ratio_info(rows)
        self.assertEqual(ratio, pm.DEFAULT_RATIO)
        self.assertIn("insufficient data", src)
        self.assertEqual(pm.ratio_info([])[0], pm.DEFAULT_RATIO)
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "ok", "median": 7.0, "n": 52})]
        self.assertEqual(pm.ratio_info(rows)[0], pm.DEFAULT_RATIO)        # implausible


class Gate(unittest.TestCase):
    """The night gate (owner decision 04.10.2026): a FULL session window only if the week is then
    predicted to end <= the threshold (w + session cost + forecast user use until the reset);
    no margin; the threshold defaults to one session window left; the straight line without a
    forecast. ratio 0.125: a full session window = 12.5 weekly %, the dynamic threshold 87.5%."""
    FC = 0.2 * (R - SUN).total_seconds() / 3600                     # 92 h x 0.2 = 18.4

    def test_runs_only_if_predicted_end_le_threshold(self):
        d = core(50, SUN, fc=flat_fc())
        self.assertTrue(d["go"], d["reason"])
        self.assertAlmostEqual(d["forecast"], self.FC)              # no margin on the forecast
        self.assertAlmostEqual(d["predicted_end"], 50 + 12.5 + self.FC)
        self.assertEqual((d["threshold"], d["run_cost"], d["headroom"]), (87.5, 12.5, 12.5))
        self.assertAlmostEqual(d["target"], 62.5)
        self.assertIn("predicted end", d["reason"])
        edge = 87.5 - 12.5 - self.FC                                 # exactly at the threshold: runs
        self.assertTrue(core(edge, SUN, fc=flat_fc())["go"])
        d = core(edge + 0.5, SUN, fc=flat_fc())
        self.assertFalse(d["go"])
        self.assertFalse(d["postpone"])
        self.assertEqual(d["headroom"], 0.0)                         # no partial / short run
        self.assertIn("above the threshold", d["reason"])
        self.assertEqual(d["recheck_at"], pm.next_session_start(SUN, WIN))   # D-202: Mon 04:00
        self.assertEqual(d["recheck_at"], SUN + timedelta(hours=5))
        self.assertIn("next check at the session-window start Mon 05.10. 04:00", d["reason"])

    def test_full_session_or_nothing(self):
        for w in range(0, 80, 3):
            d = core(w, SUN, fc=flat_fc())
            self.assertIn(d["headroom"], (0.0, 12.5), w)

    def test_rest_of_a_live_session_window(self):
        d = core(50, SUN, fc=flat_fc(), s_pct=40, s_reset=SUN + timedelta(hours=2))
        self.assertAlmostEqual(d["run_cost"], 0.125 * 60)
        self.assertAlmostEqual(d["predicted_end"], 50 + 7.5 + self.FC)
        self.assertAlmostEqual(d["headroom"], 7.5)
        # the run's own use does not flip the gate mid-session: w and the session rise together
        later = core(50 + 0.125 * 30, SUN + timedelta(hours=1), fc=lambda a, b: 0.0, s_pct=70,
                     s_reset=SUN + timedelta(hours=2))
        self.assertAlmostEqual(later["predicted_end"], 50 + 7.5)

    def test_heavy_forecast_no_run(self):
        d = core(40, SUN, fc=flat_fc(1.0))                            # the user is forecast to fill it
        self.assertFalse(d["go"])
        self.assertEqual(d["headroom"], 0.0)

    def test_dynamic_threshold_and_override(self):
        self.assertAlmostEqual(pm.dynamic_threshold(0.154), 84.6)
        self.assertAlmostEqual(pm.dynamic_threshold(0.2), 80.0)
        self.assertEqual(pm.dynamic_threshold(0.9), pm.THRESHOLD_RANGE[0])    # clamped
        self.assertEqual(pm.threshold_for(0.125, "auto")[0], 87.5)
        self.assertEqual(pm.threshold_for(0.125, 92), (92.0, "override"))
        self.assertEqual(core(60, SUN, fc=flat_fc(), ratio=0.1)["threshold"], 90.0)   # follows the ratio
        w = 57.0                                                     # 87.9 predicted
        self.assertFalse(core(w, SUN, fc=flat_fc())["go"])
        d = core(w, SUN, fc=flat_fc(), thr=95)
        self.assertTrue(d["go"])
        self.assertEqual((d["threshold"], d["threshold_source"]), (95.0, "override"))
        self.assertFalse(core(50, SUN, fc=flat_fc(), thr=80)["go"])  # 80.9 > 80

    def test_straight_line_fallback(self):
        d = core(10, SUN)                                            # no forecast
        end = Z("2026-10-05T07:00:00Z")                               # Mon 09:00, tonight's window end
        line = 87.5 * ((end - (R - pm.WEEK)) / pm.WEEK)
        self.assertTrue(d["go"], d["reason"])
        self.assertEqual(d["headroom"], 12.5)                         # a full session window
        self.assertIn("straight line", d["reason"])
        self.assertIn(f"{line:.1f}%", d["reason"])
        d = core(line - 12, SUN)                                      # the session would cross the line
        self.assertFalse(d["go"])
        self.assertFalse(d["postpone"])
        self.assertEqual(d["headroom"], 0.0)

    def test_recheck_per_session_window(self):
        w, t = 40.0, SUN
        d = core(w, t, fc=flat_fc())                                 # 40 + 12.5 + 18.4 = 70.9
        self.assertTrue(d["go"])
        w += d["headroom"]
        d = core(w, t + timedelta(hours=5), fc=flat_fc())            # 04:00: 52.5 + 12.5 + 17.4 = 82.4
        self.assertTrue(d["go"], d["reason"])
        w += d["headroom"]
        d = core(w, pm.next_window_start(t + timedelta(hours=6), WIN), fc=flat_fc())   # Mon 23:00: 91.1
        self.assertFalse(d["go"], d["reason"])

    def test_simulated_week_ends_at_the_threshold(self):
        """Light user (0.1 %/h): full session windows while the predicted end allows, the week
        reaches the threshold (not more), the last stretch takes the rest."""
        w, t, runs, rate = 0.0, Z("2026-10-01T21:00:00Z"), [], 0.1
        while t < R:
            d = core(w, t, fc=flat_fc(rate))
            if d["mode"] == "last_mile":
                break
            if d["go"]:
                self.assertLessEqual(d["predicted_end"], 87.5 + 1e-9)
                runs.append(d["headroom"])
                w += d["headroom"]
            nxt = t + timedelta(hours=5)
            if not pm.in_window(nxt, WIN):
                nxt = pm.next_window_start(nxt, WIN)
            nxt = min(nxt, R)
            w += rate * (nxt - t).total_seconds() / 3600
            t = nxt
        self.assertTrue(all(x == 12.5 for x in runs), runs)          # only full runs
        self.assertGreaterEqual(len(runs), 5)
        self.assertLessEqual(w + rate * (R - t).total_seconds() / 3600, 87.5 + 1e-6)
        self.assertGreater(w, 87.5 - 12.5 - rate * (R - t).total_seconds() / 3600 - 1e-6)
        d = core(w, R - timedelta(hours=4), fc=flat_fc(rate))
        self.assertEqual(d["mode"], "last_mile")
        self.assertAlmostEqual(d["headroom"], 100.0 - w)

    def test_reset_inside_a_window(self):
        r = Z("2026-10-08T23:30:00Z")                                  # Fri 01:30 Berlin
        d = pm.decide_core(0, r, r - pm.WEEK + timedelta(minutes=30), 600.0, None, None, None, True, 0.125,
                           "auto", WIN, "auto", flat_fc(0.1))
        self.assertEqual(d["mode"], "night")
        self.assertTrue(d["go"])

    def test_dst_windows(self):
        self.assertEqual(pm.latest_window_start(Z("2026-10-26T02:00:00Z"), WIN), Z("2026-10-25T22:00:00Z"))
        self.assertTrue(pm.in_window(Z("2027-03-28T21:30:00Z"), WIN))
        self.assertFalse(pm.in_window(Z("2027-03-28T20:30:00Z"), WIN))
        self.assertEqual(pm.window_end(Z("2026-10-24T21:00:00Z"), WIN), Z("2026-10-25T08:00:00Z"))


class Postpone(unittest.TestCase):
    def test_user_active_postpones(self):
        d = core(30, SUN, msu=20.0, fc=flat_fc(0.1))
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], SUN + timedelta(minutes=40))
        self.assertIn("postponed", d["reason"])
        self.assertTrue(core(30, d["recheck_at"], msu=60.0, fc=flat_fc(0.1))["go"])

    def test_unknown_and_session_guard(self):
        d = core(30, SUN, msu=None, known=False, fc=flat_fc(0.1))
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], SUN + pm.RETRY)
        sr = SUN + timedelta(hours=2)
        d = core(30, SUN, fc=flat_fc(0.1), s_pct=86, s_reset=sr)
        self.assertEqual(d["recheck_at"], sr)
        self.assertTrue(core(30, SUN, fc=flat_fc(0.1), s_pct=84, s_reset=sr)["go"])


class Activity(unittest.TestCase):
    NOW = SUN

    def rows(self, **at15):
        out = []
        for m in range(120, -1, -15):
            kw = at15 if m == 15 else {}
            out.append(row(self.NOW - timedelta(minutes=m), kw.get("w", 30), own=kw.get("own"),
                           other=kw.get("other")))
        return out

    def test_own_session_prompt_is_not_activity(self):
        rows = self.rows(own={"human_prompts": 3, "assistant_turns": 9})
        self.assertAlmostEqual(pm.minutes_since_user(self.NOW, rows), 120)

    def test_other_session_and_device(self):
        self.assertAlmostEqual(pm.minutes_since_user(self.NOW, self.rows(other={"human_prompts": 1})), 15)
        self.assertAlmostEqual(pm.minutes_since_user(self.NOW, self.rows(w=31)), 15)
        self.assertAlmostEqual(pm.minutes_since_user(self.NOW, self.rows(w=31, own={"assistant_turns": 5})), 120)

    def test_unknown_and_malformed(self):
        self.assertIsNone(pm.minutes_since_user(self.NOW, []))
        self.assertIsNone(pm.minutes_since_user(self.NOW, self.rows()[:-3]))
        self.assertAlmostEqual(pm.minutes_since_user(self.NOW, self.rows(other={"human_prompts": "x"})), 15)

    def test_decide_end_to_end(self):
        u = {"weekly": {"percent": 15, "resets_at": R}, "session": {"percent": 10, "resets_at": None}}
        rows = self.rows(own={"human_prompts": 2, "assistant_turns": 4})
        d = pm.decide(u, self.NOW, params=(dict(pm.DEFAULTS), "t"), rows=rows, fires=[])
        self.assertTrue(d["go"], d["reason"])                       # straight line: no closed week yet
        self.assertIn("too little user data", d["reason"])
        d = pm.decide(u, self.NOW, params=(dict(pm.DEFAULTS), "t"), rows=self.rows(other={"human_prompts": 1}),
                      fires=[])
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], self.NOW + timedelta(minutes=45))


class Predictor(unittest.TestCase):
    """The forecast: the user's typical rise per hour of the week from closed cycles."""
    OLD = R - pm.WEEK                                   # the closed cycle reset Thu 01.10. 19:00

    def week(self, tue_rate=2.0, other_rate=0.1, burn=None):
        """A closed cycle (Thu 24.09. 19:00 - Thu 01.10. 19:00) sampled every 15 min."""
        rows, w, t = [], 0.0, self.OLD - pm.WEEK
        while t < self.OLD:
            lt = t.astimezone(pm.BERLIN)
            rate = tue_rate if lt.weekday() == 1 and 9 <= lt.hour < 21 else other_rate
            busy = bool(burn and burn[0] <= t < burn[1])
            if busy:
                rate += 5.0
            w += rate / 4
            rows.append(row(t + timedelta(minutes=15), round(w, 4), reset=self.OLD,
                            own={"assistant_turns": 3} if busy else None))
            t += timedelta(minutes=15)
        return rows

    def test_threshold(self):
        rates, why = pm.fit_profile(self.week()[:200], [], R)        # 50 h < 72 h
        self.assertIsNone(rates)
        self.assertIn("too little user data", why)

    def test_profile_and_forecast(self):
        rates, why = pm.fit_profile(self.week(), [], R)
        self.assertIn("1 closed week", why)
        self.assertAlmostEqual(rates[pm._how(Z("2026-10-06T10:00:00Z"))], 2.0, places=3)   # Tue 12:00
        self.assertAlmostEqual(rates[pm._how(SUN)], 0.1, places=3)
        self.assertAlmostEqual(pm.forecast_user(rates, Z("2026-10-06T07:00:00Z"), Z("2026-10-06T19:00:00Z")),
                               24.0, places=2)
        self.assertAlmostEqual(pm.forecast_user(rates, SUN, SUN + timedelta(hours=10)), 1.0, places=2)  # no margin
        # the night before the busy Tuesday predicts a higher week end than the night after it
        fc = lambda a, b: pm.forecast_user(rates, a, b)   # noqa: E731
        mon = core(10, Z("2026-10-05T21:00:00Z"), fc=fc)
        wed = core(10, Z("2026-10-07T21:00:00Z"), fc=fc)
        self.assertGreater(mon["predicted_end"], wed["predicted_end"])

    def test_autonomous_runs_are_not_user_demand(self):
        fire = self.OLD - timedelta(days=2)
        rows = self.week(tue_rate=0.0, other_rate=0.0, burn=(fire, fire + timedelta(hours=3)))
        with_fire, _ = pm.fit_profile(rows, [fire], R)
        without, _ = pm.fit_profile(rows, [], R)
        self.assertAlmostEqual(sum(with_fire), 0.0)
        self.assertAlmostEqual(sum(without), 15.0, places=3)
        # the user typing into an AFClaude session ends the autonomous span
        for r in rows:
            if pm._ts(r["at"]) == fire + timedelta(hours=1):
                r["activity"]["own"] = {"human_prompts": 1}
        back, _ = pm.fit_profile(rows, [fire], R)
        self.assertAlmostEqual(sum(back), 10.0, places=3)

    def test_autonomous_span_ends_when_the_run_goes_idle_not_at_its_own_prompt(self):
        f = Z("2026-10-01T12:00:32Z")
        rows = [row(f - timedelta(seconds=32) + timedelta(minutes=15 * i)) for i in range(8)]
        for i in (1, 2, 3):                                  # the run's turns 12:00-12:45
            rows[i]["activity"]["own"] = {"assistant_turns": 5}
        rows[1]["activity"]["own"]["human_prompts"] = 1      # the fire's typed-in message (12:15 sample)
        self.assertEqual(pm.autonomous_spans(rows, [f]), [(f, Z("2026-10-01T12:45:00Z"))])   # idle from 12:45
        # a duplicate fire a minute later types a second message into the same interval
        rows[1]["activity"]["own"]["human_prompts"] = 2
        f2 = f + timedelta(minutes=1)
        self.assertEqual(pm.autonomous_spans(rows, [f, f2])[0], (f, Z("2026-10-01T12:45:00Z")))
        # the owner typing into the session during the run still ends the span there
        rows[1]["activity"]["own"]["human_prompts"] = 1
        rows[2]["activity"]["own"]["human_prompts"] = 1
        self.assertEqual(pm.autonomous_spans(rows, [f]), [(f, Z("2026-10-01T12:30:00Z"))])
        # rows without activity data (sampler scan failed) do not end a span
        rows[3]["activity"] = None
        rows[4]["activity"]["own"] = {"assistant_turns": 2}
        rows[2]["activity"]["own"]["human_prompts"] = 0
        self.assertEqual(pm.autonomous_spans(rows, [f]), [(f, Z("2026-10-01T13:00:00Z"))])

    def test_fire_times(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "st.json")
            with open(p, "w") as fh:
                json.dump({"handled": {"a": {"at": "2026-10-01T12:00:00Z", "result": "continued-in-place"},
                                       "b": {"at": "2026-10-01T13:00:00Z", "result": "dry-run"}}}, fh)
            self.assertEqual(pm.fire_times([(p, "handled", "at"), (p + "x", "handled", "at")]),
                             [Z("2026-10-01T12:00:00Z")])

    def test_forecast_log_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "fl.jsonl")
            rows = self.week()
            t0 = self.OLD - timedelta(days=2)
            dd = {"forecast": 30.0, "t0": t0, "w0": 40.0, "run_cost": 12.5, "predicted_end": 82.5,
                  "threshold": 87.5, "go": True, "target": 52.5}
            self.assertTrue(pm.record_forecast(dd, self.OLD, log))
            self.assertFalse(pm.record_forecast({"forecast": None}, self.OLD, log))
            errs = pm.forecast_errors(rows, [], R, log)
            self.assertEqual(len(errs), 1)
            actual = sum(dw for a, b, dw in pm.user_intervals(rows, []) if a >= t0)
            self.assertAlmostEqual(errs[0]["actual"], actual)
            self.assertAlmostEqual(errs[0]["error"], 30.0 - actual)
            self.assertEqual(pm.forecast_errors(rows, [], self.OLD - timedelta(days=1), log), [])  # not closed


class Accuracy(unittest.TestCase):
    """The model's inaccuracy back-calculated from the usage data and threshold_info() for the UI."""
    SNAP = {"ratio_windows": {"status": "ok", "weighted": 0.15, "n": 10, "weighted_stdev": 0.02,
                              "intrinsic_stdev": 0.012, "se": 0.005, "rounding_sd": 0.003, "stdev": 0.03,
                              "p10": 0.11, "p90": 0.18, "mean": 0.14, "median": 0.15}}

    def cycles(self, rates, until):
        """Cycles ending R - 7 d, R, R + 7 d ... sampled every 15 min up to `until`; rates[i] =
        the user's %/h in cycle i."""
        rows, start = [], R - 2 * pm.WEEK
        for i, rate in enumerate(rates):
            reset, w, t = start + pm.WEEK, 0.0, start
            while t < min(reset, until):
                w += rate / 4
                rows.append(row(t + timedelta(minutes=15), round(w, 4), reset=reset))
                t += timedelta(minutes=15)
            start = reset
        rows[-1]["limit_ratio"] = self.SNAP
        return rows

    def test_backtest_matches_a_steady_user(self):
        now = R + timedelta(days=1)
        rows = self.cycles([0.2, 0.2, 0.2], now)
        bt = pm.forecast_backtest(rows, [], now, WIN)
        self.assertTrue(bt)
        self.assertTrue(all(abs(x["error"]) < 0.3 for x in bt), [x["error"] for x in bt])
        closed = [x for x in bt if x["closed"]]
        self.assertGreaterEqual(len(closed), 14)                      # 2 session windows x 7 nights of cycle 2
        first = Z(closed[0]["at"])
        self.assertTrue(pm.in_window(first, WIN))
        st = pm.error_stats(bt)
        self.assertEqual(st["status"], "ok")
        self.assertAlmostEqual(st["bias"], 0.0, delta=0.3)

    def test_backtest_sees_an_under_prediction(self):
        now = R + timedelta(hours=1)
        bt = pm.forecast_backtest(self.cycles([0.1, 0.3], now), [], now, WIN)   # the user got busier
        st = pm.error_stats(bt)
        self.assertGreater(st["bias"], 0)                               # actual - predicted > 0
        self.assertEqual(st["n_closed"], st["n"])
        self.assertEqual(pm.error_stats([])["status"], "insufficient_data")
        # no closed week before any decision point: nothing to back-calculate
        self.assertEqual(pm.forecast_backtest(self.cycles([0.2], R - pm.WEEK), [], R, WIN), [])

    def test_ratio_stats(self):
        rs = pm.ratio_stats([dict(row(SUN, 10), limit_ratio=self.SNAP)])
        self.assertEqual((rs["value"], rs["kind"], rs["n"], rs["weighted_stdev"], rs["intrinsic_stdev"]),
                         (0.15, "windows", 10, 0.02, 0.012))
        rs = pm.ratio_stats([])
        self.assertEqual((rs["value"], rs["kind"], rs["weighted_stdev"]), (pm.DEFAULT_RATIO, "default", None))

    def test_threshold_info_shape(self):
        now = R + timedelta(days=1)
        rows = self.cycles([0.2, 0.2, 0.2], now)
        d = pm.decide_core(30, R + pm.WEEK, now, 600.0, None, None, None, True, 0.15, "auto", WIN, "auto",
                           flat_fc(0.2))
        ti = pm.threshold_info(rows, [], now, d)
        self.assertEqual(set(ti), {"ratio", "full_session_cost", "model_error", "predicted_end_sd", "threshold", "now"})
        self.assertEqual((ti["ratio"]["value"], ti["ratio"]["n"], ti["ratio"]["spread"]), (0.15, 10, 0.02))
        self.assertEqual(ti["full_session_cost"]["value"], 15.0)
        self.assertAlmostEqual(ti["full_session_cost"]["sd"], 100 * (0.012 ** 2 + 0.005 ** 2) ** 0.5, places=1)
        self.assertEqual(ti["full_session_cost"]["spread"], 2.0)
        me = ti["model_error"]
        for k in ("n", "n_closed", "bias", "sd", "rmse", "worst", "status", "note", "logged"):
            self.assertIn(k, me)
        self.assertEqual(me["status"], "ok")
        t = ti["threshold"]
        self.assertEqual((t["active"], t["source"], t["dynamic_default"], t["override"]), (85.0, "dynamic", 85.0, None))
        self.assertEqual(t["range"], [50.0, 99.0])
        self.assertIsNotNone(ti["predicted_end_sd"])
        self.assertEqual(ti["now"]["threshold"], 85.0)
        self.assertAlmostEqual(ti["now"]["slack"], 85.0 - d["predicted_end"], places=1)
        self.assertTrue(pm.threshold_lines(ti))
        old = afclaude_config.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            afclaude_config.CONFIG_FILE = os.path.join(tmp, "c.json")
            try:
                with open(afclaude_config.CONFIG_FILE, "w") as fh:
                    json.dump({"reserve_threshold": 90}, fh)
                t = pm.threshold_info(rows, [], now)["threshold"]
            finally:
                afclaude_config.CONFIG_FILE = old
        self.assertEqual((t["active"], t["source"], t["dynamic_default"], t["override"]), (90.0, "override", 85.0, 90.0))
        ti = pm.threshold_info([], [], now)                           # no data at all: still a valid shape
        self.assertEqual(ti["model_error"]["status"], "insufficient_data")
        self.assertEqual(ti["threshold"]["active"], 80.0)             # the conservative default ratio 0.2
        self.assertTrue(pm.threshold_lines(ti))


class NextRun(unittest.TestCase):
    """pacing.next_run_core(): when the next run takes place (D-166)."""
    NOW = SUN - timedelta(hours=2)                                   # Sun 21:00 Berlin, before the window

    def nr(self, w, now=None, fc=None, lm="auto", current=None, ratio=0.125, **kw):
        return pm.next_run_core(w, R, now or self.NOW, ratio, fc, "auto", lm, WIN, current, **kw)

    def test_tonight_passes(self):
        d = self.nr(30, fc=flat_fc(0.1))                             # no more usage: 30 + 12.5 + 9.2 = 51.7
        self.assertEqual((d["kind"], d["at"], d["label"]), ("night", SUN, "night window"))
        self.assertIn("≤ 87.5%", d["reason"])
        self.assertAlmostEqual(d["predicted_end"], 30 + 12.5 + 0.1 * 92)
        e = d["expected"]                                            # with the forecast until the start: +0.2
        self.assertEqual((e["kind"], e["at"]), ("night", SUN))
        self.assertAlmostEqual(e["predicted_end"], 30 + 0.1 * 2 + 12.5 + 0.1 * 92)

    def test_no_more_usage_vs_forecast(self):
        """D-200: the primary result keeps the weekly % at each start = now; the gate there still
        adds run_cost + the forecast from the start to the reset. "expected" adds the forecast
        until the start, too."""
        d = self.nr(30, fc=flat_fc(0.5))                 # Sun 23:00 30+12.5+46 > 87.5; Mon 04:00 30+12.5+43.5 <=
        self.assertEqual((d["kind"], d["at"]), ("night", SUN + timedelta(hours=5)))
        self.assertAlmostEqual(d["predicted_end"], 30 + 12.5 + 0.5 * 87)
        e = d["expected"]                                            # 89.5 > 87.5 every night: last stretch
        self.assertEqual((e["kind"], e["at"]), ("last_stretch", R - timedelta(hours=10)))   # w at Thu 09:00 = 72
        self.assertEqual(e["last_stretch_at"], e["at"])
        self.assertIn("> 87.5%", e["reason"])
        self.assertIn("last stretch Thu 09:00", e["reason"])

    def test_no_night_passes_last_stretch(self):
        d = self.nr(80, fc=flat_fc(0.5))                             # 80 + 12.5 > 87.5 every night
        self.assertEqual((d["kind"], d["at"]), ("last_stretch", R - timedelta(hours=10)))   # from w now: 2 windows
        self.assertEqual(d["last_stretch_at"], d["at"])
        self.assertIn("last stretch Thu 09:00", d["reason"])
        self.assertEqual(d["expected"]["kind"], "after_reset")      # forecast to be used up before the stretch

    def test_last_stretch_length(self):
        d = self.nr(90, fc=flat_fc(0.05))                            # from w now = 90: one session window
        self.assertEqual((d["kind"], d["at"]), ("last_stretch", R - timedelta(hours=5)))
        self.assertIn("last stretch Thu 14:00", d["reason"])
        e = self.nr(50, fc=flat_fc(0.5))["expected"]                 # w at the stretch ~ 92-95: one window
        self.assertEqual((e["kind"], e["at"]), ("last_stretch", R - timedelta(hours=5)))
        self.assertIn("last stretch Thu 14:00", e["reason"])

    def test_straight_line_walks_the_nights(self):
        d = self.nr(40)                                              # Sun: 52.5 > 44.8; Mon: <= 57.3
        self.assertEqual((d["kind"], d["at"]), ("night", SUN + timedelta(days=1)))
        self.assertIn("straight line", d["reason"])
        self.assertEqual(d["expected"]["at"], d["at"])               # no forecast: both the same

    def test_session_window_rest(self):
        d = self.nr(30, fc=flat_fc(0.1), session_pct=60, session_resets_at=SUN + timedelta(hours=1))
        self.assertAlmostEqual(d["predicted_end"], 30 + 0.125 * 40 + 0.1 * 92)   # the rest of the live window
        d = self.nr(30, fc=flat_fc(0.1), session_pct=60, session_resets_at=SUN + timedelta(seconds=0.4))
        self.assertAlmostEqual(d["predicted_end"], 30 + 12.5 + 0.1 * 92)         # resets at the start: full

    def test_current_decision(self):
        go = core(30, SUN, fc=flat_fc(0.1))
        self.assertTrue(go["go"])
        d = self.nr(30, now=SUN, fc=flat_fc(0.1), current=go)
        self.assertEqual((d["kind"], d["at"]), ("now", SUN))
        self.assertEqual((d["expected"]["kind"], d["expected"]["at"]), ("now", SUN))
        held = core(30, SUN + timedelta(minutes=5), msu=10.0, fc=flat_fc(0.1))
        self.assertTrue(held["postpone"])
        d = self.nr(30, now=SUN + timedelta(minutes=5), fc=flat_fc(0.1), current=held)
        self.assertEqual((d["kind"], d["at"], d["label"]), ("postponed", held["recheck_at"], "postponed after activity"))
        self.assertIn("user active", d["reason"])
        guard = core(30, SUN, fc=flat_fc(0.1), s_pct=90, s_reset=SUN + timedelta(hours=2))
        d = self.nr(30, now=SUN, fc=flat_fc(0.1), current=guard)
        self.assertEqual((d["kind"], d["label"]), ("postponed", "postponed (session guard)"))
        # a passing gate outside the window is not "now": the run starts at the window start
        day = core(30, SUN - timedelta(hours=8), fc=flat_fc(0.1))
        self.assertEqual(self.nr(30, now=SUN - timedelta(hours=8), fc=flat_fc(0.1), current=day)["kind"], "night")

    def test_in_the_last_stretch(self):
        t = R - timedelta(hours=2)
        self.assertEqual(self.nr(90, now=t)["kind"], "last_stretch")
        self.assertEqual(self.nr(90, now=t)["at"], t)
        self.assertEqual(self.nr(90, now=t, current=core(90, t))["kind"], "now")

    def test_last_stretch_slot_already_ran(self):
        """D-204: a slot whose run already happened (ended at a limit or not) is not "now": the
        next slot start, or after the reset once the final slot ran; a run going is still "now"."""
        t = R - timedelta(hours=7)                                      # slot 2 of a 10 h stretch
        cur = core(70, t)
        self.assertEqual(self.nr(70, now=t, current=cur)["kind"], "now")             # slot start due
        d = self.nr(70, now=t, current=cur, slot_next=R - timedelta(hours=5))
        self.assertEqual((d["kind"], d["at"]), ("last_stretch", R - timedelta(hours=5)))
        self.assertIn("D-204", d["reason"])
        d = self.nr(70, now=R - timedelta(hours=2), current=core(70, R - timedelta(hours=2)), slot_next=False)
        self.assertEqual((d["kind"], d["at"]), ("after_reset", pm.next_window_start(R, WIN)))
        self.assertEqual(self.nr(70, now=t, current=cur, slot_next=R - timedelta(hours=5), active=t)["kind"],
                         "now")

    def test_after_the_reset(self):
        thu = pm.next_window_start(R, WIN)                            # Thu 23:00, the first night after
        d = self.nr(80, fc=flat_fc(0.5), lm=0)                        # last stretch off, no night passes
        self.assertEqual((d["kind"], d["at"]), ("after_reset", thu))
        self.assertIsNone(d["last_stretch_at"])
        self.assertEqual(self.nr(100)["kind"], "after_reset")
        d = self.nr(80, fc=flat_fc(0.5), lm=0)["expected"]
        self.assertEqual((d["kind"], d["at"]), ("after_reset", thu))
        d = self.nr(40, fc=flat_fc(1.0))                              # the user is forecast to use it all up
        self.assertEqual((d["expected"]["kind"], d["expected"]["at"]), ("after_reset", thu))
        self.assertEqual((d["kind"], d["at"]), ("night", SUN + timedelta(days=3)))   # without more use: Wed

    def test_unknown(self):
        self.assertEqual(self.nr(None)["kind"], "unknown")
        self.assertEqual(pm.next_run_core(30, R, R + timedelta(minutes=1), win=WIN)["kind"], "unknown")

    def test_wrapper(self):
        u = {"weekly": {"percent": 10, "resets_at": R}}
        d = pm.next_run(u, SUN - timedelta(hours=10), rows=[], fires=[])   # no data: straight line, ratio 0.2
        self.assertEqual((d["kind"], d["at"]), ("night", SUN))
        self.assertIn("straight line", d["reason"])


class SessionStarts(unittest.TestCase):
    """D-202: the gate is checked at each session-window start of the night (23:00, 04:00)."""

    def test_starts_and_dst(self):
        for night, starts in [
            ("2026-10-04T21:00:00Z", ["2026-10-04T21:00:00Z", "2026-10-05T02:00:00Z"]),   # CEST
            ("2026-10-24T21:00:00Z", ["2026-10-24T21:00:00Z", "2026-10-25T03:00:00Z"]),   # DST end (6 h)
            ("2027-03-27T22:00:00Z", ["2027-03-27T22:00:00Z", "2027-03-28T02:00:00Z"]),   # DST start (4 h)
        ]:
            ws = pm.latest_window_start(Z(night), WIN)
            self.assertEqual(pm.session_starts(ws, WIN), [Z(s) for s in starts])
            self.assertEqual(pm.session_starts(ws, WIN)[-1] + timedelta(hours=5), pm.window_end(ws, WIN))
        self.assertEqual(len(pm.session_starts(SUN, (dtime(23, 0), dtime(4, 0)))), 1)      # 5 h: one
        self.assertEqual(len(pm.session_starts(SUN, (dtime(22, 0), dtime(5, 0)))), 1)      # 7 h: one ends by 05:00
        self.assertEqual(len(pm.session_starts(SUN, (dtime(21, 0), dtime(12, 0)))), 3)     # 15 h: three

    def test_latest_next_and_deadline(self):
        s2 = SUN + timedelta(hours=5)
        self.assertEqual(pm.latest_session_start(SUN + timedelta(hours=2), WIN), SUN)
        self.assertEqual(pm.latest_session_start(s2 + timedelta(hours=4), WIN), s2)       # 08:00
        self.assertIsNone(pm.latest_session_start(SUN - timedelta(hours=1), WIN))         # 22:00: outside
        self.assertEqual(pm.next_session_start(SUN - timedelta(hours=1), WIN), SUN)
        self.assertEqual(pm.next_session_start(SUN, WIN), s2)                             # strictly after
        self.assertEqual(pm.next_session_start(s2 + timedelta(hours=4, minutes=18), WIN),
                         SUN + timedelta(days=1))                                         # 08:18 -> 23:00
        self.assertEqual(pm.postpone_deadline(SUN, WIN), s2)
        self.assertEqual(pm.postpone_deadline(s2, WIN), s2)                               # the last: none
        fall = Z("2026-10-24T21:00:00Z")
        self.assertEqual(pm.next_session_start(fall, WIN), Z("2026-10-25T03:00:00Z"))     # 04:00 CET
        self.assertEqual(pm.next_session_start(Z("2026-10-25T03:00:00Z"), WIN), Z("2026-10-25T22:00:00Z"))


class NextRunSessionStarts(unittest.TestCase):
    """D-202: next_run reports only what the runner does: 'now' only while a run is going, in the
    last stretch, or at a due session-window start that passes; else the next session-window start."""
    S2 = SUN + timedelta(hours=5)                                    # Mon 04:00

    def nr(self, w, now, current=None, fc=None, **kw):
        return pm.next_run_core(w, R, now, 0.125, fc or flat_fc(0.1), "auto", "auto", WIN, current, **kw)

    def test_no_now_between_starts(self):
        """The 08:18 case: the gate passes, but the runner only checks at 23:00 / 04:00."""
        now = self.S2 + timedelta(hours=4, minutes=18)
        cur = core(30, now, fc=flat_fc(0.1))
        self.assertTrue(cur["go"], cur["reason"])
        d = self.nr(30, now, cur)
        self.assertEqual((d["kind"], d["at"]), ("night", SUN + timedelta(days=1)))
        self.assertEqual((d["expected"]["kind"], d["expected"]["at"]), ("night", SUN + timedelta(days=1)))
        mid = SUN + timedelta(hours=2, minutes=30)                   # 01:30, nothing running
        d = self.nr(30, mid, core(30, mid, fc=flat_fc(0.1)))
        self.assertEqual((d["kind"], d["at"]), ("night", self.S2))   # the 04:00 start

    def test_now_at_a_due_start_or_while_running(self):
        for t in (SUN + timedelta(minutes=5), self.S2 + timedelta(minutes=5)):
            d = self.nr(30, t, core(30, t, fc=flat_fc(0.1)))
            self.assertEqual((d["kind"], d["at"]), ("now", t))
            self.assertIn("session-window start", d["reason"])
        later = self.S2 + timedelta(hours=1, minutes=30)               # 05:30
        d = self.nr(30, later, core(30, later, fc=flat_fc(0.1)), active=self.S2)
        self.assertEqual(d["kind"], "now")
        self.assertIn("a run is going (started Mon 04:00)", d["reason"])
        # a failing gate at the start: hold until the start where it passes (not "now")
        held = core(80, SUN + timedelta(minutes=5), fc=flat_fc(0.1))
        self.assertFalse(held["go"])
        self.assertNotEqual(self.nr(80, SUN + timedelta(minutes=5), held)["kind"], "now")

    def test_postponed_start_and_the_deadline(self):
        t = SUN + timedelta(minutes=5)
        cur = core(30, t, msu=20.0, fc=flat_fc(0.1))                 # postponed to 23:45
        d = self.nr(30, t, cur)
        self.assertEqual((d["kind"], d["at"]), ("postponed", SUN + timedelta(minutes=45)))
        self.assertEqual(self.nr(30, t, cur, deferred=False)["kind"], "night")   # the runner has none
        self.assertEqual(self.nr(30, t, cur, deferred=False)["at"], self.S2)
        # a recheck that lands after the next start (04:10) is skipped: the 04:00 start decides
        late = self.S2 - timedelta(minutes=50)                       # 03:10, user active 0 min ago
        cur = core(30, late, msu=0.0, P={"idle_min": 60.0}, fc=flat_fc(0.1))
        self.assertGreaterEqual(cur["recheck_at"], self.S2)
        self.assertEqual((self.nr(30, late, cur)["kind"], self.nr(30, late, cur)["at"]), ("night", self.S2))
        # at the last start (04:00) a postponement skips to the next night (the run would pass 09:00)
        t2 = self.S2 + timedelta(minutes=5)
        cur = core(30, t2, msu=20.0, fc=flat_fc(0.1))
        self.assertTrue(cur["postpone"])
        d = self.nr(30, t2, cur)
        self.assertEqual((d["kind"], d["at"]), ("night", SUN + timedelta(days=1)))
        # the runner's pending postponed start whose recheck has come: it fires now
        t3 = SUN + timedelta(hours=1)
        d = self.nr(30, t3, core(30, t3, fc=flat_fc(0.1)), deferred=SUN + timedelta(minutes=45))
        self.assertEqual(d["kind"], "now")
        self.assertIn("postponed session-window start", d["reason"])

    def test_walk_over_a_dst_night(self):
        """Sat 24.10. 23:00 CEST fails, Sun 25.10. 04:00 CET (6 h later) passes."""
        reset = Z("2026-10-29T17:00:00Z")
        now = Z("2026-10-24T18:00:00Z")
        d = pm.next_run_core(19, reset, now, 0.125, flat_fc(0.5), "auto", "auto", WIN, None)
        self.assertEqual((d["kind"], d["at"]), ("night", Z("2026-10-25T03:00:00Z")))
        self.assertAlmostEqual(d["predicted_end"], 19 + 12.5 + 0.5 * 110)


class OneNumber(unittest.TestCase):
    def test_reason_and_text(self):
        for w, t, fc in ((30, SUN, flat_fc(0.1)), (90, R - timedelta(hours=2), None)):
            d = core(w, t, fc=fc)
            self.assertIn(f"budget for this run +{d['headroom']:.1f}%", d["reason"])
            self.assertIn(f"+{d['headroom']:.1f} weekly %", pm.budget_text(d, w))


class Params(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(pm.parse_params({"pacing": {"forecast_margin": 1.5, "last_mile_yield": False}}),
                         {"last_mile_yield": False})                # the retired margin is ignored
        self.assertNotIn("forecast_margin", pm.DEFAULTS)
        self.assertEqual(pm.parse_params({"envelope_weekly_pct_by_hours": {"1": 2}, "idle_min": 30}),
                         {"idle_min": 30.0})
        for bad in ({"idle_min": -1}, {"last_mile_yield": "no"}):
            with self.assertRaises(ValueError):
                pm.parse_params(bad)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "um.json")
            self.assertEqual(pm.load_params(p)[1], "defaults")
            with open(p, "w") as fh:
                fh.write("{")
            self.assertIn("ignored", pm.load_params(p)[1])


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = afclaude_config.CONFIG_FILE
        afclaude_config.CONFIG_FILE = os.path.join(self.tmp.name, "afclaude.json")

    def tearDown(self):
        afclaude_config.CONFIG_FILE = self.old
        self.tmp.cleanup()

    def setcfg(self, **kw):
        with open(afclaude_config.CONFIG_FILE, "w") as fh:
            json.dump(kw, fh)

    def test_last_mile_setting(self):
        self.assertEqual(afclaude_config.last_mile_setting(), "auto")
        self.setcfg(last_mile_hours=3)
        self.assertEqual(afclaude_config.last_mile_setting(), 3.0)
        self.setcfg(last_mile_hours=0)
        self.assertEqual(afclaude_config.last_mile_setting(), 0.0)
        for bad in ("soon", True, None):
            self.setcfg(last_mile_hours=bad)
            self.assertEqual(afclaude_config.last_mile_setting(), "auto", bad)

    def test_reserve_threshold_setting(self):
        rt = afclaude_config.reserve_threshold_setting
        self.assertEqual(rt(), ("auto", "dynamic"))
        self.setcfg(reserve_threshold=88)
        self.assertEqual(rt(), (88.0, "override"))
        self.setcfg(reserve_threshold="auto", week_target=92)            # explicit auto wins
        self.assertEqual(rt(), ("auto", "dynamic"))
        self.setcfg(week_target=92)                                       # backward compatibility
        self.assertEqual(rt(), (92.0, "override (legacy week_target)"))
        for bad in (40, 100, "90", True):
            self.setcfg(reserve_threshold=bad)
            self.assertEqual(rt(), ("auto", "dynamic"), bad)
            self.setcfg(week_target=bad)
            self.assertEqual(rt(), ("auto", "dynamic"), bad)

    def test_decide_honours_the_override(self):
        u = {"weekly": {"percent": 15, "resets_at": R}, "session": {"percent": 0, "resets_at": None}}
        args = dict(params=(dict(pm.DEFAULTS), "t"), rows=[row(SUN, 15)], fires=[], msu=600.0,
                    activity_known=True)
        d = pm.decide(u, SUN, **args)
        self.assertEqual((d["threshold"], d["threshold_source"]), (80.0, d["threshold_source"]))   # ratio 0.2
        self.assertIn("dynamic", d["threshold_source"])
        self.setcfg(reserve_threshold=95)
        d = pm.decide(u, SUN, **args)
        self.assertEqual((d["threshold"], d["threshold_source"]), (95.0, "override"))
        self.setcfg(week_target=85)
        d = pm.decide(u, SUN, **args)
        self.assertEqual((d["threshold"], d["threshold_source"]), (85.0, "override (legacy week_target)"))
        self.assertIn("legacy week_target", d["reason"])

    def test_usage_model_switch(self):
        self.assertEqual(afclaude_config.usage_model(), "pacing")
        self.setcfg(usage_model="linear")
        self.assertEqual(afclaude_config.usage_model(), "linear")
        for old in ("reserve", "budget", "nonsense"):
            self.setcfg(usage_model=old)
            self.assertEqual(afclaude_config.usage_model(), "pacing")


if __name__ == "__main__":
    unittest.main()
