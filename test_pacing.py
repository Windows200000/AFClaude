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


def core(w, now, msu=600.0, ratio=0.125, anchor=None, known=True, P=None, s_pct=None, s_reset=None,
         lm="auto", fc=None, target=90.0):
    return pm.decide_core(w, R, now, msu, P, s_pct, s_reset, known, ratio, lm, WIN, target, fc, anchor)


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
        self.assertAlmostEqual(pm.night_sessions(Z("2026-10-24T12:00:00Z"), r, WIN), 11 / 5)  # an 11-h night


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


class Plan(unittest.TestCase):
    """The nights fill the week to week_target minus the forecast user use, re-planned every
    night, as full session windows; the original straight line when there is no forecast."""

    def test_straight_line_fallback(self):
        d = core(30, SUN)                                            # no forecast
        end = Z("2026-10-05T07:00:00Z")                               # Mon 09:00, tonight's window end
        self.assertAlmostEqual(d["target"], 90 * ((end - (R - pm.WEEK)) / pm.WEEK))
        self.assertAlmostEqual(d["headroom"], d["target"] - 30)
        self.assertTrue(d["go"], d["reason"])
        self.assertIn("straight line", d["reason"])
        d = core(70, SUN)                                            # ahead of the line: no run tonight
        self.assertFalse(d["go"])
        self.assertFalse(d["postpone"])
        self.assertIn("ahead of the plan", d["reason"])

    def test_forecast_share(self):
        d = core(20, SUN, fc=flat_fc(), anchor=20)
        nights = pm.night_sessions(SUN, R - timedelta(hours=10), WIN)   # 80% left: a 2-window stretch
        self.assertAlmostEqual(nights, 8.0)            # Sun, Mon, Tue, Wed nights (the stretch starts Thu 09:00)
        fc = 0.2 * (R - SUN).total_seconds() / 3600
        allow = 90 - 20 - fc
        self.assertAlmostEqual(d["forecast"], fc)
        self.assertAlmostEqual(d["allow"], allow)
        self.assertAlmostEqual(d["target"], 20 + allow * 2 / nights)
        self.assertAlmostEqual(d["headroom"], d["target"] - 20)
        self.assertIn("session windows", d["reason"])

    def test_heavy_forecast_no_run(self):
        d = core(40, SUN, fc=flat_fc(1.0))                            # the user is forecast to fill it
        self.assertLess(d["allow"], 0)
        self.assertFalse(d["go"])
        self.assertEqual(d["headroom"], 0.0)

    def test_simulated_week_fills_to_the_target(self):
        """Light user (0.1 %/h): every night runs a full catch-up, the week reaches about
        week_target before the last stretch, which takes the rest."""
        w, t, runs = 0.0, Z("2026-10-01T21:00:00Z"), []
        while t < R:
            d = core(w, t, fc=flat_fc(0.1), anchor=w)
            if d["mode"] == "last_mile":
                break
            self.assertTrue(d["go"], d["reason"])
            runs.append(d["headroom"])
            w += d["headroom"] + 0.1 * 24                              # AFClaude to the target, the user's day
            t = pm.next_window_start(t + timedelta(hours=1), WIN)
        self.assertEqual(len(runs), 7)
        self.assertGreater(min(runs), 5.0)                             # full runs, not a trickle
        self.assertGreater(w, 85.0)
        self.assertLessEqual(w, 95.0)
        d = core(w, R - timedelta(hours=4), fc=flat_fc(0.1))
        self.assertEqual(d["mode"], "last_mile")
        self.assertAlmostEqual(d["headroom"], 100.0 - w)

    def test_headroom_counts_down_through_the_night(self):
        d0 = core(30, SUN, fc=flat_fc(0.1), anchor=30)
        d1 = core(33, SUN + timedelta(hours=2), fc=flat_fc(0.1), anchor=30)
        self.assertAlmostEqual(d1["target"], d0["target"])
        self.assertAlmostEqual(d1["headroom"], d0["headroom"] - 3)
        day = core(31, Z("2026-10-05T12:00:00Z"), fc=flat_fc(0.1), anchor=30)   # Mon 14:00: still Sunday's
        self.assertEqual(day["t0"], SUN)
        self.assertAlmostEqual(day["target"], d0["target"])

    def test_week_target_setting(self):
        a = core(20, SUN, fc=flat_fc(0.1), anchor=20, target=80)
        b = core(20, SUN, fc=flat_fc(0.1), anchor=20, target=95)
        self.assertLess(a["target"], b["target"])

    def test_gap_and_reset_inside_a_window(self):
        d = core(1, R - pm.WEEK + timedelta(hours=2))                  # Thu 21:00, before the first window
        self.assertEqual(d["mode"], "none")
        self.assertEqual(d["headroom"], 0.0)
        self.assertEqual(d["recheck_at"], Z("2026-10-01T21:00:00Z"))
        r = Z("2026-10-08T23:30:00Z")                                  # Fri 01:30 Berlin
        d = pm.decide_core(0, r, r - pm.WEEK + timedelta(minutes=30), 600.0, None, None, None, True, 0.125,
                           "auto", WIN, 90.0, flat_fc(0.1), 0)
        self.assertEqual(d["mode"], "night")
        self.assertEqual(d["t0"], r - pm.WEEK)
        self.assertTrue(d["go"])

    def test_dst_windows(self):
        self.assertEqual(pm.latest_window_start(Z("2026-10-26T02:00:00Z"), WIN), Z("2026-10-25T22:00:00Z"))
        self.assertTrue(pm.in_window(Z("2027-03-28T21:30:00Z"), WIN))
        self.assertFalse(pm.in_window(Z("2027-03-28T20:30:00Z"), WIN))
        self.assertEqual(pm.window_end(Z("2026-10-24T21:00:00Z"), WIN), Z("2026-10-25T08:00:00Z"))


class Postpone(unittest.TestCase):
    def test_user_active_postpones(self):
        d = core(30, SUN, msu=20.0, fc=flat_fc(0.1), anchor=30)
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], SUN + timedelta(minutes=40))
        self.assertIn("postponed", d["reason"])
        self.assertTrue(core(30, d["recheck_at"], msu=60.0, fc=flat_fc(0.1), anchor=30)["go"])

    def test_unknown_and_session_guard(self):
        d = core(30, SUN, msu=None, known=False, fc=flat_fc(0.1), anchor=30)
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], SUN + pm.RETRY)
        sr = SUN + timedelta(hours=2)
        d = core(30, SUN, fc=flat_fc(0.1), anchor=30, s_pct=86, s_reset=sr)
        self.assertEqual(d["recheck_at"], sr)
        self.assertTrue(core(30, SUN, fc=flat_fc(0.1), anchor=30, s_pct=84, s_reset=sr)["go"])


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
        u = {"weekly": {"percent": 30, "resets_at": R}, "session": {"percent": 10, "resets_at": None}}
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
            if burn and burn[0] <= t < burn[1]:
                rate += 5.0
            w += rate / 4
            rows.append(row(t + timedelta(minutes=15), round(w, 4), reset=self.OLD))
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
        self.assertAlmostEqual(pm.forecast_user(rates, SUN, SUN + timedelta(hours=10), 1.25), 1.25, places=2)
        # the night before the busy Tuesday plans with less room than the night after it
        fc = lambda a, b: pm.forecast_user(rates, a, b)   # noqa: E731
        mon = core(10, Z("2026-10-05T21:00:00Z"), fc=fc, anchor=10)
        wed = core(10, Z("2026-10-07T21:00:00Z"), fc=fc, anchor=10)
        self.assertGreater(wed["allow"], mon["allow"])

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
            dd = {"forecast": 30.0, "t0": t0, "w0": 40.0, "allow": 20.0, "target": 45.0}
            self.assertTrue(pm.record_forecast(dd, self.OLD, log))
            self.assertFalse(pm.record_forecast({"forecast": None}, self.OLD, log))
            errs = pm.forecast_errors(rows, [], R, log)
            self.assertEqual(len(errs), 1)
            actual = sum(dw for a, b, dw in pm.user_intervals(rows, []) if a >= t0)
            self.assertAlmostEqual(errs[0]["actual"], actual)
            self.assertAlmostEqual(errs[0]["error"], 30.0 - actual)
            self.assertEqual(pm.forecast_errors(rows, [], self.OLD - timedelta(days=1), log), [])  # not closed


class OneNumber(unittest.TestCase):
    def test_reason_and_text(self):
        for w, t, fc in ((30, SUN, flat_fc(0.1)), (90, R - timedelta(hours=2), None)):
            d = core(w, t, fc=fc, anchor=w)
            self.assertIn(f"budget for this run +{d['headroom']:.1f}%", d["reason"])
            self.assertIn(f"+{d['headroom']:.1f} weekly %", pm.budget_text(d, w))


class Params(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(pm.parse_params({"pacing": {"forecast_margin": 1.5, "last_mile_yield": False}}),
                         {"forecast_margin": 1.5, "last_mile_yield": False})
        self.assertEqual(pm.parse_params({"envelope_weekly_pct_by_hours": {"1": 2}, "idle_min": 30}),
                         {"idle_min": 30.0})
        for bad in ({"forecast_margin": 9}, {"last_mile_yield": "no"}):
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

    def test_week_target(self):
        self.assertEqual(afclaude_config.week_target(), 90.0)
        self.setcfg(week_target=95)
        self.assertEqual(afclaude_config.week_target(), 95.0)
        for bad in (99, 50, "90", True):
            self.setcfg(week_target=bad)
            self.assertEqual(afclaude_config.week_target(), 90.0, bad)

    def test_usage_model_switch(self):
        self.assertEqual(afclaude_config.usage_model(), "pacing")
        self.setcfg(usage_model="linear")
        self.assertEqual(afclaude_config.usage_model(), "linear")
        for old in ("reserve", "budget", "nonsense"):
            self.setcfg(usage_model=old)
            self.assertEqual(afclaude_config.usage_model(), "pacing")


if __name__ == "__main__":
    unittest.main()
