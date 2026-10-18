#!/usr/bin/env python3
"""Offline tests for D-259: the last stretch before the weekly reset runs regardless of the
owner's activity (no yield), but a last-stretch start (a slot, a window start or a fill-up in
it) still never types into a session the owner is typing in or holds: handle_fire's owner check
returns OWNER_BUSY (not handled, no pause) and the caller retries it OWNER_BUSY_RECHECK later.
Normal starts keep the yield and their old preflight handling. The fake tmux / session comes
from test_compaction.Env; the pacing decision itself is in test_pacing (test_yield_and_holds)."""
import os
import sys
import types
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import afclaude_config  # noqa: E402
import keepalive as ka  # noqa: E402
import pacing  # noqa: E402
from test_compaction import NIGHT, SID, Armed, Env  # noqa: E402

TYPING = "a draft in ka-abcdef01's input box (5 characters)"
HELD = f"{ka.RC_HELD} thread (pid [7]); a take-over would fork the user's Remote Control thread"


class Defaults(unittest.TestCase):
    def test_no_last_stretch_yield_by_default(self):
        testenv.clear_settings()
        self.assertFalse(afclaude_config.setting("pacing_last_mile_yield"))
        self.assertFalse(pacing.DEFAULTS["last_mile_yield"])
        self.assertFalse(pacing.load_params()[0]["last_mile_yield"])

    def test_fillup_owner_activity(self):
        """The fill-up's D-018 yield: gone in the last stretch, kept outside it."""
        testenv.clear_settings()
        s = afclaude_config.settings("pacing_idle_min", "pacing_last_mile_yield")
        old = pacing.minutes_since_user
        pacing.minutes_since_user = lambda now, rows=None: 5.0          # the owner was active 5 min ago
        try:
            self.assertEqual(ka._fillup_user(NIGHT, True, s), (False, None))
            self.assertEqual(ka._fillup_user(NIGHT, False, s), (True, NIGHT + timedelta(minutes=55)))
            s["pacing_last_mile_yield"] = True
            self.assertTrue(ka._fillup_user(NIGHT, True, s)[0])
        finally:
            pacing.minutes_since_user = old


class OwnerCheck(Env):
    KEY = "last-mile-2026-10-08T17:00:00+00:00-s1"

    def start(self, owner_check=True, args=Armed()):
        at = NIGHT + timedelta(days=1)
        r = ka.handle_fire(SID, {"uuid": self.KEY, "timestamp": at, "prompt": "last_mile", "budget": "b",
                                 "headroom": 14.0, "start_at": at, "owner_check": owner_check},
                           "last stretch, CONTINUE: test", ka.load_state(), args)
        return r, ka.load_state()["handled"].get(self.KEY)

    def test_typing_waits_then_fires(self):
        self.typing = TYPING
        r, rec = self.start()
        self.assertEqual((r, rec, self.sent), (ka.OWNER_BUSY, None, []))
        self.assertFalse(self.paused()["paused"])
        self.typing = None                                    # the owner stopped: the retry fires
        r, rec = self.start()
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertEqual(len(self.sent), 1)

    def test_owner_holds_waits_instead_of_dropping_the_slot(self):
        ka.preflight = lambda sid: (False, [HELD], None)
        r, rec = self.start()
        self.assertEqual((r, rec, self.sent), (ka.OWNER_BUSY, None, []))
        self.assertFalse(self.paused()["paused"])
        r, rec = self.start(owner_check=False)                # a normal start: skipped (handled) as before
        self.assertIsNone(r)
        self.assertEqual((rec["result"], rec["held"]), ("preflight-failed", True))

    def test_normal_start_unchanged(self):
        calls = []
        ka.owner_typing = lambda sid, now: calls.append(sid) or TYPING
        r, rec = self.start(owner_check=False)
        self.assertEqual((calls, rec["result"]), ([], "continued-in-place"))

    def test_no_tmux_pane_no_typing_check(self):
        """A resume (the session is not live in tmux) has no pane to type into: owner_typing is
        not asked (it would fail on tmux), the start goes ahead."""
        calls = []
        ka.preflight = lambda sid: (True, [], "resume")
        ka.owner_typing = lambda sid, now: calls.append(sid) or TYPING
        r, rec = self.start()
        self.assertEqual((calls, rec["result"]), ([], "continued-in-place"))

    def test_unreadable_pane_aborts_the_start(self):
        """owner_typing failing (tmux, an unknown screen) is an error, not a go (D-235)."""
        def boom(sid, now):
            raise RuntimeError("can't read ka-abcdef01's pane")
        ka.owner_typing = boom
        r, rec = self.start()
        self.assertEqual((r, rec["result"], self.sent), ("failed", "start-failed", []))
        self.assertTrue(self.paused()["paused"])

    def test_dry_run_waits_too(self):
        self.typing = TYPING
        self.assertEqual(self.start(args=types.SimpleNamespace(arm=False, now=False))[0], ka.OWNER_BUSY)


class Callers(Env):
    """The callers mark a last-stretch start with owner_check and retry an OWNER_BUSY later."""
    NAMES = Env.NAMES + ("handle_fire", "fillup_decision", "stall_status")

    def setUp(self):
        super().setUp()
        self.stalls, self.result = [], ka.OWNER_BUSY

        def hf(sid, stall, reason, st, args):
            self.stalls.append(stall)
            return self.result
        ka.handle_fire = hf
        ka.fresh_usage = lambda now, force=False: self.usage
        ka.stall_status = lambda sid, now: ("NO_STALL", "x", None)

    def test_last_mile_pass(self):
        now = self.now
        self.usage["weekly"] = {"percent": 95.0, "resets_at": now + timedelta(hours=2)}
        ka.budget_eval = lambda u, n: {"go": True, "reason": "CONTINUE: last stretch", "text": "b",
                                       "headroom": 5.0, "last_mile": True}
        st = {"handled": {}, "fires": {}}
        self.assertEqual(ka.last_mile_pass(SID, now, st, Armed()), now + ka.OWNER_BUSY_RECHECK)
        self.assertTrue(self.stalls[-1]["owner_check"])
        self.result = None
        self.assertIsNone(ka.last_mile_pass(SID, now + ka.OWNER_BUSY_RECHECK, st, Armed()))
        self.assertEqual(len(self.stalls), 2)

    def test_window_start_in_the_last_stretch(self):
        decision = {"go": True, "reason": "CONTINUE: last stretch", "text": "b", "headroom": 5.0, "last_mile": True}
        ka.budget_eval = lambda u, n: dict(decision)
        args = types.SimpleNamespace(arm=False, now=False)
        ka.window_start_pass(SID, NIGHT, args)
        self.assertTrue(self.stalls[-1]["owner_check"])
        defs = ka.load_deferred()
        self.assertEqual(list(defs), [ka.schedule.start_key(NIGHT)])
        self.assertEqual(ka.parse_ts(defs[ka.schedule.start_key(NIGHT)]["recheck_at"]),
                         NIGHT + ka.OWNER_BUSY_RECHECK)
        decision["last_mile"] = False                         # a night-gate start: no owner check
        self.result = None
        ka.save_deferred({})
        ka.window_start_pass(SID, NIGHT, args)
        self.assertFalse(self.stalls[-1]["owner_check"])

    def test_fillup_in_the_last_stretch(self):
        now = self.now
        calls = []
        d = {"status": "fire", "reason": "due", "key": "fillup-x", "remaining": 10.0, "cost": 1.0,
             "reset": now + timedelta(minutes=30), "start": now - timedelta(minutes=1), "in_last_stretch": True,
             "rate": 20.0, "rate_src": "test", "time_min": 30.0, "factor": 1.1, "ratio": 0.1}
        ka.fillup_decision = lambda sid, n, u=None: dict(d)
        ka.fresh_usage = lambda n, force=False: calls.append(n) or self.usage
        self.usage["fetched_at"] = now                        # young: no refresh needed
        self.assertEqual(ka.fillup_pass(SID, now, Armed()), now + ka.OWNER_BUSY_RECHECK)
        self.assertTrue(self.stalls[-1]["owner_check"])
        self.assertEqual(ka.fillup_pass(SID, now + timedelta(minutes=1), Armed()), now + ka.OWNER_BUSY_RECHECK)
        self.assertEqual((len(self.stalls), calls), (1, []))  # not retried (no /usage call) before then
        self.result = None
        self.usage["fetched_at"] = now + ka.OWNER_BUSY_RECHECK
        self.assertIsNone(ka.fillup_pass(SID, now + ka.OWNER_BUSY_RECHECK, Armed()))
        self.assertEqual(len(self.stalls), 2)
        d["in_last_stretch"], d["key"] = False, "fillup-y"    # outside the last stretch: no owner check
        ka.fillup_pass(SID, now + ka.OWNER_BUSY_RECHECK, Armed())
        self.assertFalse(self.stalls[-1]["owner_check"])


if __name__ == "__main__":
    unittest.main()
