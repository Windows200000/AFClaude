#!/usr/bin/env python3
"""Offline tests for budget.py (temp files only, never the real data/)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AFCLAUDE_CONFIG"] = os.devnull   # hermetic: the code defaults, not a local data/afclaude.json
import afclaude_config  # noqa: E402
import budget as bm  # noqa: E402

UTC = timezone.utc
WIN = (dtime(23, 0), dtime(9, 0))                 # the default window, Berlin wall clock
GENERIC = dict(bm.GENERIC_ENVELOPE)
LIGHT = bm.floored(bm.effective_envelope({1: 5, 5: 8, 24: 15, 96: 25}, 4)[0])   # a light user, 4 weeks


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# Thu 08.10.2026 19:00 Berlin (CEST): a weekly reset like the live one
R = Z("2026-10-08T17:00:00Z")


def core(w, now, msu=600.0, env=None, ratio=0.125, anchor=None, known=True, P=None, s_pct=None, s_reset=None,
         lm="auto"):
    return bm.decide_core(w, R, now, msu, env or GENERIC, P, s_pct, s_reset, known, ratio, lm, WIN, anchor)


def row(at, w=None, reset=R, own=None, other=None, ratio=None):
    r = {"at": at.isoformat(), "activity": {"own": own or {}, "other": other or {}}}
    if w is not None:
        r["usage"] = {"weekly": {"percent": w, "resets_at": reset.isoformat()}}
    if ratio is not None:
        r["limit_ratio"] = {"ratio": ratio}
    return r


class LastMileFormula(unittest.TestCase):
    """Requirement 3: the last window starts at reset - ceil(sessions_left) x 5 h."""

    def test_hours(self):
        self.assertEqual(bm.last_mile_hours(50, 0.125), 20.0)     # 50 / 12.5 = 4 windows
        self.assertEqual(bm.last_mile_hours(55, 0.125), 20.0)     # 3.6 -> 4
        self.assertEqual(bm.last_mile_hours(50, 0.1), 25.0)       # 5 windows
        self.assertEqual(bm.last_mile_hours(55, 0.1), 25.0)       # 4.5 -> 5
        self.assertEqual(bm.last_mile_hours(90, 0.1), 5.0)
        self.assertEqual(bm.last_mile_hours(91, 0.125), 5.0)
        self.assertEqual(bm.last_mile_hours(100, 0.125), 0.0)
        self.assertEqual(bm.last_mile_hours(50, 0.125, 3), 3.0)   # numeric override
        self.assertEqual(bm.last_mile_hours(50, 0.125, 0), 0.0)   # off

    def test_boundary_in_the_decision(self):
        for ratio, hours in ((0.125, 20), (0.1, 25)):
            start = R - timedelta(hours=hours)
            inside = core(50, start, ratio=ratio, msu=1.0)            # the user is active: no yield in the LM
            self.assertEqual(inside["mode"], "last_mile", inside["reason"])
            self.assertTrue(inside["go"], inside["reason"])
            self.assertEqual(inside["headroom"], 50.0)
            self.assertEqual(inside["session_cap"], 100.0)
            self.assertEqual(inside["last_mile_start"], start)
            before = core(50, start - timedelta(minutes=1), ratio=ratio, msu=1.0)
            self.assertEqual(before["mode"], "night", before["reason"])
            self.assertFalse(before["go"])

    def test_last_mile_holds_only_for_unknown_or_exhausted(self):
        now = R - timedelta(hours=3)
        self.assertTrue(core(90, now, known=False, msu=None)["go"])          # unknown activity: still runs
        self.assertTrue(core(90, now, s_pct=95, s_reset=now + timedelta(hours=1))["go"])   # cap 100
        d = core(90, now, s_pct=100, s_reset=now + timedelta(hours=1))
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], now + timedelta(hours=1))
        d = core(100, now)
        self.assertFalse(d["go"])
        self.assertIn("exhausted", d["reason"])
        self.assertFalse(d["postpone"])
        d = core(None, now)
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])

    def test_last_mile_yield_option(self):
        now = R - timedelta(hours=3)
        d = core(90, now, msu=20.0, P={"last_mile_yield": True})
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], now + timedelta(minutes=40))

    def test_dst_reset(self):
        # reset Sun 25.10.2026 19:00 CET, the day the clocks go back: the period is in real hours
        r = Z("2026-10-25T18:00:00Z")
        d = bm.decide_core(50, r, r - timedelta(hours=20), 600.0, GENERIC, None, None, None, True, 0.125,
                           "auto", WIN, None)
        self.assertEqual(d["last_mile_start"], Z("2026-10-24T22:00:00Z"))
        self.assertEqual(d["mode"], "last_mile")


class Ratio(unittest.TestCase):
    def test_from_sampler_snapshot(self):
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "ok", "median": 0.118, "n": 52})]
        self.assertEqual(bm.ratio_info(rows)[0], 0.118)

    def test_insufficient_falls_back_and_says_so(self):
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "insufficient_data", "n": 3, "min_pairs": 10})]
        ratio, src = bm.ratio_info(rows)
        self.assertEqual(ratio, bm.DEFAULT_RATIO)
        self.assertIn("insufficient data", src)
        self.assertIn("conservative default", src)
        self.assertEqual(bm.ratio_info([])[0], bm.DEFAULT_RATIO)
        rows = [row(R - timedelta(days=1), 10, ratio={"status": "ok", "median": 7.0, "n": 52})]
        self.assertEqual(bm.ratio_info(rows)[0], bm.DEFAULT_RATIO)   # implausible


class Nights(unittest.TestCase):
    """Requirement 4: every nightly window gets a positive budget unless the week is exhausted."""

    def simulate(self, env, user_per_day, af_spends=True):
        w, nights = 0.0, []
        start = R - timedelta(days=7)
        night = bm.next_window_start(start, WIN)
        while night < R:
            d = core(w, night, env=env, anchor=w)
            if d["mode"] == "last_mile":
                nights.append(("LM", d["headroom"], d["go"]))
                break
            nights.append((night, d["night_budget"], d["go"]))
            if af_spends:
                w = min(w + d["night_budget"], 100.0)
            w = min(w + user_per_day, 100.0)
            night = bm.next_window_start(night + timedelta(hours=1), WIN)
        return nights, w

    def test_every_night_heavy_user_generic_envelope(self):
        nights, _ = self.simulate(GENERIC, 12.0)             # 84% per week by the user alone
        self.assertGreaterEqual(len(nights), 5)
        for n, b, go in nights:
            self.assertGreater(b, 0, n)
            self.assertTrue(go, n)

    def test_every_night_light_user_spreads_the_share(self):
        user_env = {1: 5, 5: 8, 24: 15, 96: 25}
        env = bm.floored(bm.effective_envelope(user_env, 2)[0])
        nights, w = self.simulate(env, 2.0)
        budgets = [b for n, b, go in nights if n != "LM"]
        self.assertTrue(all(b > 5.0 for b in budgets), budgets)     # the share, well above the floor
        self.assertEqual(budgets, sorted(budgets))                  # the need shrinks as the week passes
        self.assertTrue(all(go for _, _, go in nights))

    def test_exhausted_week_no_budget(self):
        d = core(100, R - timedelta(days=2))
        self.assertEqual(d["headroom"], 0.0)
        self.assertFalse(d["go"])

    def test_floor_only_out_of_the_free_quota(self):
        user_env = {1: 5, 5: 8, 24: 15, 96: 25}
        env = bm.floored(bm.effective_envelope(user_env, 4)[0])
        now = Z("2026-10-04T21:00:00Z")                     # Sun 23:00 Berlin, 92 h to the reset
        need = 1.25 * bm.interp_env(env, (R - now).total_seconds() / 3600)
        d = core(100 - need - 3, now, env=env, anchor=100 - need - 3)   # free 3%, 3 nights: share 1
        self.assertAlmostEqual(d["share"], 1.0)
        self.assertAlmostEqual(d["night_budget"], 2.0)     # topped up to the floor, out of `free`
        d = core(100 - need - 1, now, env=env, anchor=100 - need - 1)   # free 1%: floor = free
        self.assertAlmostEqual(d["night_budget"], 1.0)
        d = core(60, now, anchor=60)                        # generic: no free quota -> the minimum
        self.assertAlmostEqual(d["night_budget"], bm.DEFAULTS["night_floor_min"])
        self.assertTrue(d["go"])

    def test_share_formula(self):
        user_env = {1: 5, 5: 8, 24: 15, 96: 25}
        env = bm.floored(bm.effective_envelope(user_env, 4)[0])
        now = Z("2026-10-04T21:00:00Z")                     # Sun 23:00 Berlin
        d = core(20, now, env=env, anchor=20)
        T0 = (R - now).total_seconds() / 3600
        need = 1.25 * bm.interp_env(env, T0)
        lm0 = R - timedelta(hours=bm.last_mile_hours(20, 0.125))     # 7 windows = 35 h -> Wed 08:00
        n = 1 + len(bm.window_starts(now, lm0, WIN))
        self.assertEqual(n, 3)                               # Sun, Mon, Tue (Wed 23:00 is in the LM)
        self.assertAlmostEqual(d["share"], (100 - 20 - need) / n)
        self.assertAlmostEqual(d["night_budget"], d["share"])
        self.assertAlmostEqual(d["target"], 20 + d["share"])


class Anchor(unittest.TestCase):
    """The night's budget is fixed at its window start: spending lowers the headroom one for one."""

    def test_headroom_counts_down_through_the_night(self):
        t0 = Z("2026-10-04T21:00:00Z")                       # Sun 23:00 Berlin
        rows = [row(t0 - timedelta(minutes=15 * i), 30) for i in range(4, 0, -1)]
        rows += [row(t0 + timedelta(minutes=15 * i), 30 + i * 0.5) for i in range(0, 5)]
        a = lambda t: bm.weekly_at(rows, t, R, t0 + timedelta(hours=1))   # noqa: E731
        self.assertEqual(a(t0), 30)
        d0 = core(30, t0, anchor=a, env=LIGHT)
        d1 = core(31.5, t0 + timedelta(hours=1), anchor=a, env=LIGHT)
        self.assertGreater(d0["night_budget"], 2)
        self.assertAlmostEqual(d0["night_budget"], d1["night_budget"])
        self.assertAlmostEqual(d1["headroom"], d0["headroom"] - 1.5)

    def test_daytime_uses_last_nights_leftover(self):
        t0 = Z("2026-10-04T21:00:00Z")
        d = core(31, Z("2026-10-05T12:00:00Z"), anchor=30, env=LIGHT)   # Mon 14:00: still Sunday's night
        self.assertEqual(d["t0"], t0)
        self.assertAlmostEqual(d["headroom"], d["night_budget"] - 1)

    def test_anchor_ignores_the_previous_cycle(self):
        t0 = R - timedelta(days=7)                           # the week starts at the reset
        old = R - timedelta(days=7)
        rows = [row(old - timedelta(minutes=10), 99, reset=old), row(old + timedelta(minutes=6), 1)]
        self.assertEqual(bm.weekly_at(rows, t0, R, old + timedelta(hours=1)), 1)
        self.assertIsNone(bm.weekly_at([], t0, R, old))

    def test_used_up_is_a_final_hold(self):
        t0 = Z("2026-10-04T21:00:00Z")
        d = core(32, t0 + timedelta(hours=2), anchor=30)     # 2% floor spent
        self.assertFalse(d["go"])
        self.assertFalse(d["postpone"])
        self.assertIn("used up", d["reason"])
        self.assertEqual(d["recheck_at"], Z("2026-10-05T21:00:00Z"))


class ResetBoundary(unittest.TestCase):
    def test_gap_before_the_first_window_has_no_budget(self):
        # the reset Thu 19:00 Berlin; at 21:00 the latest window start (Wed 23:00) is last week's
        d = core(1, R - timedelta(days=7) + timedelta(hours=2))
        self.assertEqual(d["mode"], "none")
        self.assertEqual(d["headroom"], 0.0)
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], Z("2026-10-01T21:00:00Z"))   # Thu 23:00 Berlin

    def test_reset_inside_the_window_starts_a_night(self):
        r = Z("2026-10-08T23:30:00Z")                        # Fri 01:30 Berlin
        now = r - timedelta(days=7) + timedelta(minutes=30)
        d = bm.decide_core(0, r, now, 600.0, GENERIC, None, None, None, True, 0.125, "auto", WIN, 0)
        self.assertEqual(d["mode"], "night")
        self.assertEqual(d["t0"], r - timedelta(days=7))
        self.assertTrue(d["go"])

    def test_dst_window_starts(self):
        # 24.10. 23:00 CEST = 21:00Z, 25.10. 23:00 CET = 22:00Z (clocks went back on the 25th)
        a, b = Z("2026-10-24T12:00:00Z"), Z("2026-10-26T12:00:00Z")
        self.assertEqual(bm.window_starts(a, b, WIN), [Z("2026-10-24T21:00:00Z"), Z("2026-10-25T22:00:00Z")])
        self.assertEqual(bm.latest_window_start(Z("2026-10-26T02:00:00Z"), WIN), Z("2026-10-25T22:00:00Z"))
        # spring: 27.03.2027 23:00 CET = 22:00Z, 28.03. 23:00 CEST = 21:00Z
        self.assertEqual(bm.window_starts(Z("2027-03-27T12:00:00Z"), Z("2027-03-29T12:00:00Z"), WIN),
                         [Z("2027-03-27T22:00:00Z"), Z("2027-03-28T21:00:00Z")])
        self.assertTrue(bm.in_window(Z("2027-03-28T21:30:00Z"), WIN))
        self.assertFalse(bm.in_window(Z("2027-03-28T20:30:00Z"), WIN))


class Postpone(unittest.TestCase):
    """Requirement 2: a HOLD for the user postpones to last activity + idle_min."""
    NOW = Z("2026-10-04T21:00:00Z")

    def test_user_active_postpones(self):
        d = core(30, self.NOW, msu=20.0, anchor=30)
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], self.NOW + timedelta(minutes=40))
        self.assertIn("postponed", d["reason"])
        later = core(30, d["recheck_at"], msu=60.0 + 0.01, anchor=30)
        self.assertTrue(later["go"], later["reason"])

    def test_unknown_activity_and_usage_retry(self):
        d = core(30, self.NOW, msu=None, known=False, anchor=30)
        self.assertTrue(d["postpone"])
        self.assertEqual(d["recheck_at"], self.NOW + bm.RETRY)
        d = core(None, self.NOW)
        self.assertTrue(d["postpone"])

    def test_session_guard_postpones_to_the_session_reset(self):
        sr = self.NOW + timedelta(hours=2)
        d = core(30, self.NOW, anchor=30, s_pct=86, s_reset=sr)
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], sr)
        self.assertTrue(core(30, self.NOW, anchor=30, s_pct=84, s_reset=sr)["go"])


class Activity(unittest.TestCase):
    """Requirement 1: only non-AFClaude sessions and other devices count as the user."""
    NOW = Z("2026-10-04T21:00:00Z")

    def rows(self, **at15):
        out = []
        for m in range(120, -1, -15):
            kw = at15 if m == 15 else {}
            out.append(row(self.NOW - timedelta(minutes=m), kw.get("w", 30), own=kw.get("own"),
                           other=kw.get("other")))
        return out

    def test_own_session_prompt_is_not_activity(self):
        rows = self.rows(own={"human_prompts": 3, "assistant_turns": 9})
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, rows), 120)

    def test_other_session_is_activity(self):
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, self.rows(other={"human_prompts": 1})), 15)
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, self.rows(other={"subagent_turns": 2})), 15)

    def test_other_device_rise(self):
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, self.rows(w=31)), 15)     # no local turns
        rows = self.rows(w=31, own={"assistant_turns": 5})                               # AFClaude's own
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, rows), 120)

    def test_unknown_and_malformed(self):
        self.assertIsNone(bm.minutes_since_user(self.NOW, []))
        self.assertIsNone(bm.minutes_since_user(self.NOW, self.rows()[:-3]))             # stale (> 30 min)
        rows = self.rows(other={"human_prompts": "x"})
        self.assertAlmostEqual(bm.minutes_since_user(self.NOW, rows), 15)                # counts as the user

    def test_decide_end_to_end(self):
        rows = self.rows(own={"human_prompts": 2, "assistant_turns": 4})
        u = {"weekly": {"percent": 30, "resets_at": R}, "session": {"percent": 10, "resets_at": None}}
        d = bm.decide(u, self.NOW, params=(dict(bm.DEFAULTS), GENERIC, "generic"), rows=rows)
        self.assertTrue(d["go"], d["reason"])
        rows = self.rows(other={"human_prompts": 1})
        d = bm.decide(u, self.NOW, params=(dict(bm.DEFAULTS), GENERIC, "generic"), rows=rows)
        self.assertFalse(d["go"])
        self.assertEqual(d["recheck_at"], self.NOW + timedelta(minutes=45))


class OneNumber(unittest.TestCase):
    """Requirement 6: the reason and the budget text carry the same headroom number."""

    def test_reason_and_text(self):
        now = Z("2026-10-04T21:00:00Z")
        for w, t in ((30, now), (80, R - timedelta(hours=2))):
            d = core(w, t, anchor=w)
            self.assertIn(f"budget for this run +{d['headroom']:.1f}%", d["reason"])
            self.assertIn(f"+{d['headroom']:.1f} weekly %", bm.budget_text(d, w))


class UserModel(unittest.TestCase):
    def test_parse_new_params(self):
        env, weeks, over = bm.parse_user_model({"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"5": 10},
                                                "decider": {"night_floor": 3, "night_floor_min": 1,
                                                            "last_mile_yield": True}})
        self.assertEqual(over, {"night_floor": 3.0, "night_floor_min": 1.0, "last_mile_yield": True})
        with self.assertRaises(ValueError):
            bm.parse_user_model({"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"5": 10},
                                 "last_mile_yield": "yes"})

    def test_load_params_fallbacks(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "um.json")
            self.assertIn("no user model", bm.load_params(p)[2])
            with open(p, "w") as fh:
                fh.write("{")
            self.assertIn("unreadable", bm.load_params(p)[2])
            with open(p, "w") as fh:
                json.dump({"weeks_of_data": 5, "envelope_weekly_pct_by_hours": {"1": 1, "168": 2}}, fh)
            P, env, src = bm.load_params(p)
            self.assertIn("user envelope", src)
            self.assertGreaterEqual(bm.interp_env(env, 168), 50.0)      # floored at 0.5 x generic


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
        for bad in ("soon", True, None, -2):
            self.setcfg(last_mile_hours=bad)
            self.assertEqual(afclaude_config.last_mile_setting(), "auto" if bad != -2 else 0.0, bad)
        self.setcfg(last_mile_hours="AUTO")
        self.assertEqual(afclaude_config.last_mile_setting(), "auto")

    def test_usage_model_switch(self):
        self.assertEqual(afclaude_config.usage_model(), "budget")
        self.setcfg(usage_model="linear")
        self.assertEqual(afclaude_config.usage_model(), "linear")
        for old in ("reserve", "nonsense"):
            self.setcfg(usage_model=old)
            self.assertEqual(afclaude_config.usage_model(), "budget")


if __name__ == "__main__":
    unittest.main()
