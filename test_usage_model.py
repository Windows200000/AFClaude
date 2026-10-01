#!/usr/bin/env python3
"""Offline tests for usage_model.py (temp files only, never the real data/)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_model as um  # noqa: E402

UTC = timezone.utc
RESET = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
GEN = um.GENERIC_ENVELOPE
ENV = {"1": 10, "2": 14, "5": 20, "24": 40, "96": 60}     # a made-up user envelope


def usage(w, reset=RESET, s=None, s_reset=None):
    return {"weekly": {"percent": w, "resets_at": reset}, "session": {"percent": s, "resets_at": s_reset}}


def row(at, own=None, other=None, w=50.0, since=None):
    r = {"at": at.isoformat(), "activity": {"own": own or {}, "other": other or {}},
         "usage": {"weekly": {"percent": w, "resets_at": RESET.isoformat()}}}
    if since:
        r["since"] = since.isoformat()
    return r


class Envelope(unittest.TestCase):
    def test_interpolation(self):
        self.assertEqual(um.interp_env(GEN, 0), 0)
        self.assertAlmostEqual(um.interp_env(GEN, 0.5), 6)          # (0,0)-(1,12)
        self.assertEqual(um.interp_env(GEN, 1), 12)
        self.assertAlmostEqual(um.interp_env(GEN, 2), 14)           # (1,12)-(3,16)
        self.assertEqual(um.interp_env(GEN, 4), 16)                 # flat 3..5
        self.assertAlmostEqual(um.interp_env(GEN, 36), 60)          # (24,50)-(48,70)
        self.assertEqual(um.interp_env(GEN, 500), 100)              # flat after the last point
        self.assertEqual(um.interp_env(ENV, 200), 60)
        self.assertEqual(um.interp_env(ENV, -3), 0)

    def test_blending(self):
        env, src = um.effective_envelope(ENV, 0.7)
        self.assertEqual((env, src), (dict(GEN), "generic"))
        self.assertEqual(um.effective_envelope(None, 9)[1], "generic")
        env, src = um.effective_envelope(ENV, 2)
        self.assertEqual(src, "blended")
        for h in (1, 2, 3, 5, 10, 24, 48, 96, 168):
            with self.subTest(h=h):
                self.assertAlmostEqual(um.interp_env(env, h),
                                       max(um.interp_env(ENV, h), 0.5 * um.interp_env(GEN, h)), places=6)
        self.assertEqual(um.interp_env(env, 168), 60)               # user 60 > 0.5 * 100
        self.assertEqual(um.interp_env(env, 1), 10)                 # user 10 > 6
        env, src = um.effective_envelope(ENV, 4)
        self.assertEqual(src, "user")
        self.assertEqual(um.interp_env(env, 168), 60)

    def test_reserve_and_target(self):
        P = dict(um.DEFAULTS)
        self.assertAlmostEqual(um.reserve(36, False, GEN, P), 75)    # 1.25 * 60
        self.assertEqual(um.reserve(500, False, GEN, P), 100)        # capped
        self.assertEqual(um.reserve(0, False, GEN, P), 0)
        # active user: at least the 5 h envelope (never more than env(T) for a non-decreasing
        # envelope; an active user is yielded to anyway)
        self.assertAlmostEqual(um.reserve(0.5, True, GEN, P), 7.5)
        self.assertAlmostEqual(um.reserve(36, True, GEN, P), 75)
        # grace shortens the horizon
        self.assertAlmostEqual(um.reserve(1.5, False, GEN, dict(P, grace_min=30)), 15)


class Decide(unittest.TestCase):
    P = dict(um.DEFAULTS)

    def d(self, w, T_h, msu=999.0, known=True, **kw):
        return um.decide_core(w, RESET, RESET - timedelta(hours=T_h), msu, GEN, self.P,
                              activity_known=known, **kw)

    def test_continue_and_budget(self):
        r = self.d(80, 4)                       # reserve 1.25 * 16 = 20 -> target 80
        self.assertFalse(r["go"], r["reason"])
        r = self.d(70, 4)
        self.assertTrue(r["go"], r["reason"])
        self.assertAlmostEqual(r["target"], 80)
        self.assertAlmostEqual(r["headroom"], 10)
        for word in ("reserve 20.0%", "target 80.0%", "headroom +10.0%"):
            self.assertIn(word, r["reason"])

    def test_min_gap(self):
        self.assertFalse(self.d(79.0, 4)["go"])      # gap 1.0 is not > 1
        self.assertTrue(self.d(78.9, 4)["go"])

    def test_target_rises_towards_reset(self):
        self.assertFalse(self.d(90, 4)["go"])
        self.assertTrue(self.d(90, 0.5)["go"])       # reserve 7.5 -> target 92.5

    def test_yield_to_active_user(self):
        r = self.d(10, 30, msu=20)
        self.assertFalse(r["go"])
        self.assertIn("yield", r["reason"])
        self.assertTrue(r["user_active"])
        self.assertTrue(self.d(10, 0.5, msu=61)["go"])              # idle_min 60
        r = self.d(10, 0.5, msu=60)
        self.assertFalse(r["go"])
        self.assertAlmostEqual(r["reserve"], 1.25 * 6)              # T=0.5: env(min(T,5)) = env(T)

    def test_unknown_activity_is_active(self):
        r = self.d(10, 30, msu=None, known=False)
        self.assertFalse(r["go"])
        self.assertIn("unknown", r["reason"])
        self.assertFalse(self.d(10, 1, msu=None, known=False)["go"])   # also in the last hours

    def test_reset_in_the_past_holds(self):
        r = um.decide_core(10, RESET, RESET + timedelta(minutes=1), 999, GEN, self.P)
        self.assertFalse(r["go"])
        self.assertIn("stale", r["reason"])

    def test_active_floor_ignores_grace(self):
        P = dict(self.P, grace_min=60)
        self.assertAlmostEqual(um.reserve(2, False, GEN, P), 1.25 * 12)   # env(1h)
        self.assertAlmostEqual(um.reserve(2, True, GEN, P), 1.25 * 14)    # env(min(2h, 5h))

    def test_unknown_usage_and_exhausted(self):
        r = um.decide_core(None, RESET, RESET, 999, GEN, self.P)
        self.assertFalse(r["go"])
        self.assertIn("unknown", r["reason"])
        self.assertFalse(um.decide_core(50, None, RESET, 999, GEN, self.P)["go"])
        r = self.d(100, 0.2)
        self.assertFalse(r["go"])
        self.assertIn("exhausted", r["reason"])

    def test_session_guard(self):
        now = RESET - timedelta(hours=30)
        # a window that ends long before the weekly reset: cap 85%
        self.assertFalse(self.d(1, 30, session_pct=85, session_resets_at=now + timedelta(hours=2))["go"])
        self.assertTrue(self.d(1, 30, session_pct=84, session_resets_at=now + timedelta(hours=2))["go"])
        # a window that ends at the weekly reset may be filled to 100%
        r = self.d(50, 3, session_pct=95, session_resets_at=RESET)
        self.assertTrue(r["go"], r["reason"])
        r = self.d(50, 3, session_pct=100, session_resets_at=RESET)
        self.assertFalse(r["go"])
        self.assertIn("session guard", r["reason"])
        # an expired window does not count
        self.assertTrue(self.d(1, 30, session_pct=99, session_resets_at=now - timedelta(minutes=1))["go"])

    def test_session_cap_helper(self):
        self.assertEqual(um.session_cap(RESET, RESET - timedelta(hours=1), self.P), 100)
        self.assertEqual(um.session_cap(RESET, RESET + timedelta(seconds=30), self.P), 100)   # jitter
        self.assertEqual(um.session_cap(RESET, RESET + timedelta(minutes=4), self.P), 85)
        self.assertEqual(um.session_cap(RESET, RESET + timedelta(hours=1), self.P), 85)
        self.assertEqual(um.session_cap(RESET, None, self.P), 85)
        # mid-week (reset far away) every window ends before the reset: still 85%
        self.assertEqual(um.session_cap(RESET, RESET - timedelta(hours=28), self.P, RESET - timedelta(hours=30)), 85)
        self.assertEqual(um.session_cap(RESET, RESET, self.P, RESET - timedelta(hours=5)), 100)


class UserModelFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "user_model.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc):
        with open(self.path, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))

    def test_missing_file_generic(self):
        P, env, src = um.load_params(self.path)
        self.assertEqual(env, dict(GEN))
        self.assertEqual(P, um.DEFAULTS)
        self.assertIn("no user model", src)

    def test_valid_file(self):
        self.write({"schema": um.SCHEMA, "weeks_of_data": 5, "envelope_weekly_pct_by_hours": ENV,
                    "safety": 1.1, "idle_min": 45, "grace_min": 30})
        P, env, src = um.load_params(self.path)
        self.assertEqual(env, um.floored({float(k): float(v) for k, v in ENV.items()}))
        self.assertEqual(um.interp_env(env, 24), 40)                # user above the floor
        self.assertEqual(um.interp_env(env, 168), 60)
        self.assertEqual((P["safety"], P["idle_min"], P["grace_min"]), (1.1, 45, 30))
        self.assertIn("user envelope", src)

    def test_candidate_layout(self):
        # parameters nested as in a fitted candidate (recommended.safety, decider.idle_min)
        self.write({"weeks_of_data": 2, "envelope_weekly_pct_by_hours": ENV,
                    "recommended": {"safety": 1.3, "grace_min": 0}, "decider": {"idle_min": 50, "min_gap": 2}})
        P, env, src = um.load_params(self.path)
        self.assertEqual((P["safety"], P["idle_min"], P["min_gap"]), (1.3, 50, 2))
        self.assertIn("blended", src)

    def test_few_weeks_generic(self):
        self.write({"weeks_of_data": 0.5, "envelope_weekly_pct_by_hours": ENV, "safety": 1.5})
        P, env, src = um.load_params(self.path)
        self.assertEqual(env, dict(GEN))
        self.assertEqual(P["safety"], 1.5)                          # parameters still apply

    def test_floor_with_many_weeks(self):
        self.write({"weeks_of_data": 8, "envelope_weekly_pct_by_hours": {"1": 0, "168": 1}, "safety": 1.0})
        P, env, src = um.load_params(self.path)
        for h in (0.5, 1, 5, 24, 168):
            self.assertGreaterEqual(um.interp_env(env, h), 0.5 * um.interp_env(GEN, h) - 1e-9)

    def test_regression_non_object_sections(self):
        for bad in ({"recommended": "x"}, {"decider": [1, 2]}, {"recommended": 5, "decider": None}):
            with self.subTest(bad=bad):
                self.write(dict({"weeks_of_data": 2, "envelope_weekly_pct_by_hours": ENV}, **bad))
                P, env, src = um.load_params(self.path)              # must not raise
                self.assertEqual((P, env), (um.DEFAULTS, dict(GEN)))

    def test_invalid_files(self):
        bad = ["{not json", "[]", {"weeks_of_data": 2}, {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {}},
               {"weeks_of_data": "x", "envelope_weekly_pct_by_hours": ENV},
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"1": 20, "5": 10}},     # decreasing
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"1": 120}},
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"-1": 5}},
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"h": 5}},
               '{"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"1": NaN}}',
               '{"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"1": 5, "Infinity": 9}}',
               '{"weeks_of_data": Infinity, "envelope_weekly_pct_by_hours": {"1": 5}}',
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": {"1": True}},
               {"weeks_of_data": 2, "envelope_weekly_pct_by_hours": ENV, "safety": 0.5},
               {"schema": "other/9", "weeks_of_data": 2, "envelope_weekly_pct_by_hours": ENV}]
        for doc in bad:
            with self.subTest(doc=doc):
                self.write(doc)
                P, env, src = um.load_params(self.path)
                self.assertEqual(env, dict(GEN))
                self.assertEqual(P, um.DEFAULTS)
                self.assertIn("generic", src)


class Activity(unittest.TestCase):
    NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)

    def rows(self, *specs):
        out = []
        for minutes_ago, kw in specs:
            out.append(row(self.NOW - timedelta(minutes=minutes_ago), **kw))
        return out

    def test_human_prompt(self):
        rows = self.rows((120, {}), (90, {"other": {"human_prompts": 1}}), (15, {}), (0, {}))
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 90)

    def test_other_session_turns(self):
        rows = self.rows((45, {"other": {"assistant_turns": 3}}), (0, {}))
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 45)

    def test_injected_prompt_is_not_the_user(self):
        t = self.NOW - timedelta(minutes=30)
        rows = [row(self.NOW - timedelta(minutes=300)),
                row(t, own={"human_prompts": 1, "assistant_turns": 50}, w=60, since=t - timedelta(minutes=15)),
                row(self.NOW, own={"assistant_turns": 20}, w=62)]
        fire = t - timedelta(minutes=10)
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, [fire]), 300)   # nothing but AFClaude
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 30)        # no fire: a user prompt
        rows[1]["activity"]["own"]["human_prompts"] = 2                               # fire + a real prompt
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, [fire]), 30)

    def test_rise_without_local_turns_is_the_user(self):
        rows = [row(self.NOW - timedelta(minutes=30), w=50), row(self.NOW - timedelta(minutes=15), w=52),
                row(self.NOW, w=52)]
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 15)
        rows[1]["activity"]["own"] = {"assistant_turns": 9}                           # AFClaude's own rise
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 30)

    def test_stale_or_missing_is_unknown(self):
        self.assertIsNone(um.minutes_since_user(self.NOW, [], []))
        rows = self.rows((31, {}))
        self.assertIsNone(um.minutes_since_user(self.NOW, rows, []))
        um_path, um.SAMPLES_FILE = um.SAMPLES_FILE, "/nonexistent/samples.jsonl"
        try:
            self.assertIsNone(um.minutes_since_user(self.NOW))
        finally:
            um.SAMPLES_FILE = um_path

    def test_regression_malformed_counts_mean_active(self):
        rows = self.rows((120, {}), (60, {}), (0, {"other": {"human_prompts": "1"}}))
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 0)
        rows = self.rows((120, {}), (0, {"own": {"assistant_turns": "x", "human_prompts": None}}))
        self.assertIsNotNone(um.minutes_since_user(self.NOW, rows, []))     # must not raise
        rows = self.rows((120, {}), (0, {}))
        rows[-1]["activity"] = "garbage"
        rows.insert(1, {"at": "not a time"})
        rows.insert(1, "not a dict")
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 120)

    def test_regression_malformed_fire_state(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, doc in enumerate([{"handled": [1, 2]}, {"sessions": "x"}, [], {"handled": {"a": 3}},
                                     {"handled": {"a": {"at": "2026-01-05T10:00:00+00:00"}}}]):
                paths.append(os.path.join(d, f"{i}.json"))
                with open(paths[-1], "w") as fh:
                    json.dump(doc, fh)
            self.assertEqual(len(um.fire_times(paths)), 1)
        # an exception anywhere in the activity scan -> unknown (the caller holds)
        self.assertIsNone(um.minutes_since_user(self.NOW, 12345, []))

    def test_future_rows_ignored(self):
        rows = self.rows((10, {}), (-30, {"other": {"human_prompts": 1}}))
        self.assertAlmostEqual(um.minutes_since_user(self.NOW, rows, []), 10)

    def test_tail_and_fire_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "samples.jsonl")
            with open(p, "w") as fh:
                for i in range(200):
                    fh.write(json.dumps(row(self.NOW - timedelta(minutes=15 * (199 - i)))) + "\n")
                fh.write("garbage\n")
            rows = um.tail_rows(p, max_bytes=2000)
            self.assertTrue(0 < len(rows) < 200)
            self.assertEqual(rows[-1]["at"], self.NOW.isoformat())
            ks, ds = os.path.join(d, "ka.json"), os.path.join(d, "dp.json")
            with open(ks, "w") as fh:
                json.dump({"handled": {"a": {"at": "2026-01-05T10:00:00+00:00", "result": "continued-in-place"},
                                       "b": {"at": "2026-01-05T10:05:00+00:00", "result": "dry-run"}}}, fh)
            with open(ds, "w") as fh:
                json.dump({"sessions": {"x": {"sent_at": "2026-01-05T11:00:00+00:00"}}}, fh)
            got = um.fire_times([ks, ds, os.path.join(d, "missing.json")])
            self.assertEqual([t.hour for t in got], [10, 11])


class Interface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (um.USER_MODEL_FILE, um.SAMPLES_FILE, um.FIRE_STATE_FILES)
        um.USER_MODEL_FILE = os.path.join(self.tmp.name, "user_model.json")
        um.SAMPLES_FILE = os.path.join(self.tmp.name, "samples.jsonl")
        um.FIRE_STATE_FILES = []

    def tearDown(self):
        um.USER_MODEL_FILE, um.SAMPLES_FILE, um.FIRE_STATE_FILES = self.saved
        self.tmp.cleanup()

    def samples(self, now, minutes_since_prompt):
        with open(um.SAMPLES_FILE, "w") as fh:
            for m in range(240, -1, -15):
                act = {"other": {"human_prompts": 1}} if m == minutes_since_prompt else {}
                fh.write(json.dumps(row(now - timedelta(minutes=m), **act)) + "\n")

    def test_budget_decision_and_headroom(self):
        now = RESET - timedelta(hours=2)
        self.samples(now, 120)
        go, why = um.budget_decision(usage(70), now)
        self.assertTrue(go, why)
        self.assertIn("reserve model", why)
        extra, text = um.budget_headroom(usage(70), now)
        self.assertAlmostEqual(extra, 100 - 1.25 * 14 - 70)
        self.assertIn("budget for this run", text)
        self.samples(now, 30)
        go, why = um.budget_decision(usage(70), now)
        self.assertFalse(go)
        self.assertIn("yield", why)
        self.assertEqual(um.budget_headroom(None, now), (None, "budget unknown"))
        os.remove(um.SAMPLES_FILE)
        self.assertFalse(um.budget_decision(usage(70), now)[0])        # no sampler data: active

    def test_user_model_used(self):
        now = RESET - timedelta(hours=2)
        self.samples(now, 200)
        with open(um.USER_MODEL_FILE, "w") as fh:
            json.dump({"weeks_of_data": 6, "envelope_weekly_pct_by_hours": {"1": 2, "168": 10}, "safety": 1.0}, fh)
        d = um.decide(usage(90), now)
        self.assertTrue(d["go"], d["reason"])
        self.assertAlmostEqual(d["reserve"], 0.5 * 14)              # floor: half the generic 2 h value
        self.assertIn("user envelope", d["reason"])


if __name__ == "__main__":
    unittest.main()
