#!/usr/bin/env python3
"""Offline tests for stale usage data (night 03.10 -> 04.10.2026: `claude -p /usage` printed only
the cost summary for 34 h and the cache stayed frozen): stale detection, stale rows ignored by
pacing.py / limit_ratio.py (old rows), the shared refresh retry (subprocess calls mocked), a failed
refresh as an error (D-234/D-235: alert once, automation paused, nothing starts, no sampler row from
the old cache), and the host bridge accepting the retry's request.
No claude calls, no real ~/.claude.json, no live data/ files."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import host  # noqa: E402
import keepalive as ka  # noqa: E402
import limit_ratio  # noqa: E402
import pacing  # noqa: E402
import usage_sampler as us  # noqa: E402
import usage_stale  # noqa: E402
import afclaude_config  # noqa: E402
import failstop  # noqa: E402

UTC = timezone.utc
WR = datetime(2026, 10, 8, 17, 0, 0, 283789, tzinfo=UTC)        # weekly reset
FROZEN = datetime(2026, 10, 3, 6, 30, 3, 373000, tzinfo=UTC)    # the cache that stayed


def row(at, w, s=0.0, fetched=None, sr=None, stale=None, prompts=0):
    u = {"fetched_at": str(fetched or at), "weekly": {"percent": w, "resets_at": str(WR)},
         "session": {"percent": s, "resets_at": str(sr) if sr else None}}
    if stale is not None:
        u["stale"] = stale
    return {"at": at.isoformat(), "usage": u,
            "activity": {"own": {}, "other": {"human_prompts": prompts}}}


class Patch:
    """Set module attributes for one test, restore them afterwards."""
    def __init__(self, tc):
        self.saved = []
        tc.addCleanup(self.restore)

    def __call__(self, mod, name, value):
        self.saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def restore(self):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)
        self.saved = []


class Detection(unittest.TestCase):
    def test_is_stale(self):
        now = FROZEN + timedelta(minutes=30)
        self.assertFalse(usage_stale.is_stale(FROZEN, now))
        self.assertTrue(usage_stale.is_stale(FROZEN, FROZEN + usage_stale.USAGE_STALE_AFTER + timedelta(seconds=1)))
        self.assertTrue(usage_stale.is_stale(None, now))                       # no cache at all
        self.assertFalse(usage_stale.is_stale(str(FROZEN), now))              # the sampler's str() form

    def test_row_stale_flag_and_legacy_age(self):
        t = FROZEN + timedelta(minutes=15)
        self.assertFalse(usage_stale.row_stale(row(t, 29.0, fetched=FROZEN)))
        self.assertTrue(usage_stale.row_stale(row(t, 29.0, fetched=FROZEN, stale=True)))
        # rows from before the flag (the real 34 h): recognised by sample time - fetch time
        self.assertTrue(usage_stale.row_stale(row(FROZEN + timedelta(hours=5), 29.0, fetched=FROZEN)))
        self.assertFalse(usage_stale.row_stale({"at": t.isoformat(), "usage": {"weekly": {"percent": 1}}}))
        self.assertFalse(usage_stale.row_stale({"at": t.isoformat()}))
        self.assertEqual(usage_stale.row_usage(row(t, 29.0, fetched=FROZEN, stale=True)), {})
        self.assertEqual(usage_stale.row_usage(row(t, 29.0))["weekly"]["percent"], 29.0)


def frozen_night():
    """Fresh 29% at 06:30, 8 stale readings of 29%, then a fresh 32% (the 34 h in small)."""
    rows = [row(FROZEN, 29.0)]
    for i in range(1, 9):
        rows.append(row(FROZEN + timedelta(minutes=15 * i), 29.0, fetched=FROZEN,
                        stale=True if i % 2 else None))   # flagged and legacy (unflagged) rows
    rows.append(row(FROZEN + timedelta(minutes=135), 32.0))
    return rows


class Consumers(unittest.TestCase):
    def test_pacing_ignores_stale_meters(self):
        rows = frozen_night()
        self.assertEqual(pacing._weekly(rows[3]), {})
        self.assertEqual(pacing._weekly(rows[0])["percent"], 29.0)
        # no interval spans the stale stretch: the +3% is not attributed to anyone's hour
        # (07:00 is a legacy row only 30 min after the fetch: still a valid reading)
        iv = pacing.user_intervals(rows, [])
        self.assertEqual([(a, b) for a, b, _ in iv], [(FROZEN, FROZEN + timedelta(minutes=30))])
        self.assertEqual(sum(dw for *_, dw in iv), 0.0)

    def test_limit_ratio_ignores_stale_rows(self):
        sr = FROZEN + timedelta(hours=3)
        rows = [row(FROZEN + timedelta(minutes=15 * i), 29.0, s=10.0, sr=sr, fetched=FROZEN) for i in range(5)]
        pairs = limit_ratio.build_pairs(rows)
        self.assertEqual(len(pairs), 3)          # 07:30 is 60 min after the fetch: stale, no pair
        self.assertEqual(len(limit_ratio._points(rows)), 4)
        rows[2]["usage"]["stale"] = True         # a flagged row breaks the chain too
        self.assertEqual(len(limit_ratio.build_pairs(rows)), 1)
        rows.append(row(FROZEN + timedelta(hours=2), 33.0, s=40.0, sr=sr, fetched=FROZEN))
        snap = limit_ratio.compute(rows, now=FROZEN + timedelta(hours=2))
        self.assertEqual(snap["weekly_pct_now"], 29.0)   # the stale 33% is not "now"


class Retry(unittest.TestCase):
    def setUp(self):
        self.p = Patch(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p(ka, "USAGE_STATE_FILE", os.path.join(self.tmp.name, "usage_state.json"))
        cj = os.path.join(self.tmp.name, "claude.json")                  # never the real one
        with open(cj, "w") as fh:
            json.dump({"cachedUsageUtilization": {"utilization": {"limits": []}}}, fh)
        self.p(ka, "CLAUDE_JSON", cj)
        self.p(ka, "log", lambda msg: None)
        self.alerts = []
        self.p(ka, "alert", lambda subject, body="": self.alerts.append(subject))
        self.addCleanup(testenv.unpause)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.cache = {"fetched_at": FROZEN, "weekly": {"percent": 29.0, "resets_at": WR}}
        self.calls = []
        self.fix_on = None          # which call refreshes the cache: "usage1", "usage2" or None
        self.p(ka, "read_usage_cache", lambda: dict(self.cache))

        def refresh(cwd=ka.HERE):
            n = sum(1 for c in self.calls if c == "usage") + 1
            self.calls.append("usage")
            if self.fix_on == f"usage{n}" or (self.fix_on == "poke" and "poke" in self.calls):
                self.cache["fetched_at"] = datetime.now(UTC)
                return 0, "You are currently using your subscription to power your Claude Code usage"
            return 0, "Total cost:            $0.0000\nTotal duration (API):  0s"
        self.p(ka, "refresh_usage", refresh)

        def roh(argv, input=None, cwd=None, env=None, timeout=None):
            self.calls.append("poke")
            self.poke_argv, self.poke_input = argv, input
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": False, "result": "OK"}),
                                         stderr="")
        self.p(host, "run_on_host", roh)

    def test_refresh_ok_no_poke(self):
        self.fix_on = "usage1"
        r = ka.refresh_usage_checked(now=self.now)
        self.assertTrue(r["ok"])
        self.assertFalse(r["retried"])
        self.assertEqual(self.calls, ["usage"])

    def test_young_unchanged_cache_is_ok(self):
        self.cache["fetched_at"] = self.now - timedelta(seconds=30)   # claude skips rewriting < 60 s
        r = ka.refresh_usage_checked(now=self.now)
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls, ["usage"])

    def test_poke_then_usage_again(self):
        self.fix_on = "poke"
        r = ka.refresh_usage_checked(now=self.now)
        self.assertTrue(r["ok"] and r["retried"])
        self.assertEqual(self.calls, ["usage", "poke", "usage"])
        self.assertEqual(self.poke_argv, ka.POKE_ARGV)
        self.assertEqual(self.poke_input, ka.POKE_PROMPT)
        self.assertIn("is_error=False", r["poke"])
        self.assertIsNotNone(ka.fresh_usage(self.now, force=False))   # now fresh: no new call
        self.assertEqual(self.calls, ["usage", "poke", "usage"])

    def test_still_stale_is_an_error_and_poke_is_throttled(self):
        r = ka.refresh_usage_checked(now=self.now)
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, ["usage", "poke", "usage"])
        self.assertIsNone(ka.fresh_usage(self.now + timedelta(minutes=15), force=True))
        self.assertEqual(self.alerts, ["AFClaude STOPPED: " + ka.USAGE_FAILED])   # D-234/D-235
        self.assertTrue(afclaude_config.automation_pause()["paused"])
        self.assertEqual(self.calls.count("poke"), 1)                 # throttled: one per 30 min
        self.assertIn("skipped", ka.LAST_REFRESH["poke"])
        ka.fresh_usage(self.now + ka.POKE_MIN_INTERVAL + timedelta(minutes=1), force=True)
        self.assertEqual(self.calls.count("poke"), 2)
        self.assertEqual(len(self.alerts), 1)                         # once per pause

    def test_sampler_takes_no_row_from_a_stale_cache(self):
        """D-234/D-235: the sampler no longer keeps a stale-marked row; a failed refresh raises."""
        with self.assertRaises(failstop.Failed) as cm:
            us.usage_now(self.now)
        self.assertEqual(cm.exception.why, ka.USAGE_FAILED)
        self.assertIn("did not refresh", cm.exception.detail)
        self.fix_on, self.calls = "usage1", []
        r = us.usage_now(datetime.now(UTC))
        self.assertFalse(r["stale"])
        self.assertNotIn("refresh_retry", r)

    def test_series_line_flags_stale(self):
        t = FROZEN + timedelta(hours=2)
        self.assertTrue(us.series_line(row(t, 29.0, fetched=FROZEN, stale=True))["stale"])
        self.assertNotIn("stale", us.series_line(row(t, 29.0)))


class WindowStart(unittest.TestCase):
    """D-234/D-235: a failed usage reading at a session-window start is an error at once (alert,
    automation paused), not after two failed checks; nothing starts, and the paused watcher does
    not keep calling /usage."""
    def setUp(self):
        self.p = Patch(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(testenv.unpause)
        d = self.tmp.name
        self.p(ka, "USAGE_STATE_FILE", os.path.join(d, "usage_state.json"))
        self.p(ka, "DEFER_FILE", os.path.join(d, "deferred.json"))
        self.p(ka, "STATE_FILE", os.path.join(d, "state.json"))
        self.p(ka, "PROGRESS_FILE", os.path.join(d, "PROGRESS.md"))
        testenv.notes_project(d)
        self.p(ka, "log", lambda msg: None)
        self.alerts = []
        self.p(ka, "alert", lambda subject, body="": self.alerts.append((subject, body)))
        self.p(ka, "read_usage_cache", lambda: {"fetched_at": FROZEN})
        self.p(ka, "refresh_usage_checked", lambda now=None, cwd=None: self.usage_calls.append(now) or {
            "ok": False, "usage": {"fetched_at": FROZEN}, "rc": 0, "out": "Total cost: $0", "poke": "rc=0"})
        self.usage_calls = []

    def test_failed_reading_alerts_at_once_and_holds(self):
        now = datetime(2026, 10, 3, 21, 0, 5, tzinfo=UTC)               # Sat 23:00 Berlin
        fired = []
        self.p(ka, "handle_fire", lambda *a, **k: fired.append(a))
        args = types.SimpleNamespace(now=False, arm=True)
        sid = "00000000-0000-0000-0000-000000000000"
        d = ka.window_start_pass(sid, now, args)
        self.assertFalse(d["go"])
        self.assertIn("usage reading failed", d["reason"])
        self.assertEqual([a[0] for a in self.alerts], ["AFClaude STOPPED: " + ka.USAGE_FAILED])
        self.assertIn("cache from 2026-10-03 08:30:03 CEST", self.alerts[0][1])
        self.assertIn("PAUSED until you clear it", self.alerts[0][1])
        self.assertTrue(afclaude_config.automation_pause()["paused"])
        self.assertIn("not resumed", testenv.notes(self.tmp.name))
        # the next start (and a postponed one) while paused: no /usage call, no alert, no fire
        n = len(self.usage_calls)
        ka.window_start_pass(sid, now + timedelta(hours=5), args)
        ka.defer_window_start(schedule_key(now + timedelta(hours=5, minutes=20)), sid,
                              now + timedelta(hours=5, minutes=20), "test")
        st = {"handled": {}, "fires": {}}
        ka.deferred_window_start_pass(sid, now + timedelta(hours=5, minutes=21), st, args)
        self.assertEqual(len(self.usage_calls), n)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(fired, [])
        self.assertEqual(ka.load_deferred(), {})                      # dropped, not retried
        self.assertIn("automation is paused", testenv.notes(self.tmp.name))


def schedule_key(t):
    import schedule
    return schedule.start_key(t)


class BridgeWhitelist(unittest.TestCase):
    """The retry's request must pass docker/host_exec.py unchanged (no whitelist change, so no
    reinstall of the host bridge). Checked in-process with subprocess.run stubbed: nothing runs."""
    def test_poke_argv_is_whitelisted(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("host_exec_t", os.path.join(HERE, "docker", "host_exec.py"))
        he = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(he)
        ran = []
        p = Patch(self)
        p(he, "log", lambda msg: None)
        p(he.subprocess, "run", lambda cmd, **kw: ran.append((cmd, kw)) or types.SimpleNamespace(returncode=0))
        p(he.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(ka.POKE_PROMPT.encode())))
        import shlex
        old = os.environ.get("SSH_ORIGINAL_COMMAND")
        os.environ["SSH_ORIGINAL_COMMAND"] = shlex.join(host.bridge_argv(ka.POKE_ARGV))
        try:
            self.assertEqual(he.main(), 0)
        finally:
            if old is None:
                os.environ.pop("SSH_ORIGINAL_COMMAND", None)
            else:
                os.environ["SSH_ORIGINAL_COMMAND"] = old
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][0][1:], ka.POKE_ARGV[1:])
        self.assertEqual(ran[0][1]["input"], ka.POKE_PROMPT.encode())


if __name__ == "__main__":
    unittest.main()
