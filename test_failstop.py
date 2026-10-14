#!/usr/bin/env python3
"""Offline tests for failstop.py (D-234, D-235): a failure is an ERROR line, automation_paused with
the reason (audited, actor runner:<component>) and an alert; a repeat of the same failure while
paused is only logged; a new one is added to the reason; a DB that can't record the pause still
alerts; the strict read of the switch (unreadable = paused); the owner's resume clears it
(tasks.py automation)."""
import io
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import afclaude_config  # noqa: E402
import failstop  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402


class Fail(unittest.TestCase):
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        self.logs, self.alerts = [], []
        self.audit0 = self.max_audit()

    def max_audit(self):
        conn = store.connect()
        try:
            return conn.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()[0]
        finally:
            conn.close()

    def fail(self, subject, detail="", **kw):
        return failstop.fail("keepalive", subject, detail, log=self.logs.append,
                             alert=lambda s, b="": self.alerts.append((s, b)), **kw)

    def audit(self):
        conn = store.connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT actor, via, action FROM audit_log WHERE action='automation.set' AND id > ? "
                "AND actor LIKE 'runner:%' ORDER BY id", (self.audit0,))]
        finally:
            conn.close()

    def test_error_line_pause_and_alert(self):
        r = self.fail("save timeout before the compaction", "no hand-off within 10 min")
        self.assertEqual(r, {"paused": True, "alerted": True, "already": False})
        self.assertEqual(self.logs, ["ERROR: keepalive: save timeout before the compaction: no hand-off within 10 min"])
        p = afclaude_config.automation_pause()
        self.assertTrue(p["paused"])
        self.assertRegex(p["reason"], r"^\d\d\.\d\d\. \d\d:\d\d keepalive: save timeout before the compaction$")
        self.assertEqual(p["by"], "runner:keepalive")
        self.assertEqual(self.audit(), [{"actor": "runner:keepalive", "via": "runner", "action": "automation.set"}])
        (subject, body), = self.alerts
        self.assertEqual(subject, "AFClaude STOPPED: save timeout before the compaction")
        self.assertIn("no hand-off within 10 min", body)
        self.assertIn("PAUSED until you clear it (`python3 tasks.py automation resume`)", body)

    def test_repeat_is_logged_only_and_a_new_failure_is_added(self):
        self.fail("usage reading failed", "x")
        r = self.fail("usage reading failed", "y")                  # e.g. the sampler 15 min later
        self.assertEqual(r, {"paused": True, "alerted": False, "already": True})
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("not alerted again", self.logs[-1])
        self.fail("budget model failed")
        p = afclaude_config.automation_pause()
        self.assertIn("keepalive: usage reading failed | also: ", p["reason"])
        self.assertTrue(p["reason"].endswith("keepalive: budget model failed"))
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(len(self.audit()), 2)

    def test_a_manual_pause_keeps_its_reason_first(self):
        conn = store.connect()
        try:
            import actions
            actions.perform(conn, "automation.set", {"paused": True, "reason": "owner: maintenance"},
                            actor="cli", via="cli")
        finally:
            conn.close()
        self.fail("dispatcher config unreadable")
        self.assertTrue(afclaude_config.automation_pause()["reason"].startswith("owner: maintenance | also: "))

    def test_pause_not_recorded_still_alerts(self):
        with mock.patch.object(store, "connect", side_effect=store.DBUnavailable("disk full", "disk_full")):
            r = self.fail("hand-off not marked consumed")
        self.assertEqual((r["paused"], r["alerted"]), (False, True))
        self.assertIn("automation_paused could not be set", self.logs[-1])
        self.assertIn("could NOT record the pause", self.alerts[0][1])

    def test_no_pause(self):
        r = self.fail("something to report", pause=False)
        self.assertEqual((r["paused"], self.alerts[0][0]), (False, "something to report"))
        self.assertFalse(afclaude_config.automation_pause()["paused"])

    def test_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        r = failstop.fail("sampler", "s", log=boom, alert=boom)
        self.assertEqual((r["paused"], r["alerted"]), (True, False))


class StrictRead(unittest.TestCase):
    def test_unreadable_counts_as_paused(self):
        testenv.unpause()
        self.assertFalse(failstop.paused()["paused"])
        with mock.patch.object(afclaude_config, "_read_conn", side_effect=sqlite3.OperationalError("locked")):
            p = failstop.paused()
        self.assertTrue(p["paused"])
        self.assertIn("could not be read (OperationalError: locked)", p["reason"])
        with mock.patch.object(afclaude_config, "_read_conn", return_value=None):
            self.assertTrue(failstop.paused()["paused"])            # no DB at all
        testenv.put_raw("automation_paused", "yes")                    # written around actions.py
        self.addCleanup(testenv.clear_settings)
        p = failstop.paused()
        self.assertTrue(p["paused"])
        self.assertIn("invalid", p["reason"])


class Cli(unittest.TestCase):
    """The owner clears the pause (D-234/D-235): tasks.py automation [status|resume|pause]."""
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tasks.main(list(argv))
        return rc, out.getvalue()

    def test_status_resume_pause(self):
        self.addCleanup(testenv.unpause)
        failstop.fail("keepalive", "start of abcdef01 aborted: compaction failed", log=lambda m: None,
                      alert=lambda s, b="": None)
        rc, out = self.run_cli("automation")
        self.assertEqual(rc, 0)
        self.assertIn("automation PAUSED since", out)
        self.assertIn("(by runner:keepalive): ", out)
        self.assertIn("keepalive: start of abcdef01 aborted: compaction failed", out)
        self.assertIn("python3 tasks.py automation resume", out)
        rc, out = self.run_cli("automation", "resume")
        self.assertEqual((rc, out.strip()), (0, "automation running (not paused)"))
        p = afclaude_config.automation_pause()
        self.assertEqual((p["paused"], p["reason"], p["by"]), (False, None, "cli"))
        rc, out = self.run_cli("automation", "pause", "by hand")
        self.assertIn("by hand", out)
        rc, out = self.run_cli("automation", "status", "--json")
        self.assertIn('"paused": true', out)


if __name__ == "__main__":
    unittest.main()
