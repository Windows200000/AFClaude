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
        self.assertEqual(r, {"paused": True, "alerted": True, "already": False, "project": None})
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
        self.assertEqual(r, {"paused": True, "alerted": False, "already": True, "project": None})
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


SID_A = "aaaaaaaa-1111-4222-8333-444444444444"     # the task-manager of the project PA
SID_B = "bbbbbbbb-1111-4222-8333-444444444444"     # ... of a second (fake) project PB
SID_X = "cccccccc-1111-4222-8333-444444444444"     # manages no project


class ProjectPause(unittest.TestCase):
    """D-244: a project's pause holds that project's runner checks only; the install-wide one
    still holds all; an inbox error pauses the project its message was for."""
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        self.addCleanup(testenv.managed_project("PA", SID_A))
        self.addCleanup(testenv.managed_project("PB", SID_B))
        self.logs, self.alerts = [], []

    def fail(self, subject, detail="", **kw):
        return failstop.fail("inbox", subject, detail, log=self.logs.append,
                             alert=lambda s, b="": self.alerts.append((s, b)), **kw)

    def test_project_pause_holds_only_that_project(self):
        r = self.fail("inbox message triage failed", "message #1: timed out", session=SID_A)
        self.assertEqual(r, {"paused": True, "alerted": True, "already": False, "project": "PA"})
        a = failstop.paused(session=SID_A)
        self.assertEqual((a["paused"], a["scope"], a["project"], a["by"]), (True, "project", "PA", "runner:inbox"))
        self.assertRegex(a["reason"], r"^\d\d\.\d\d\. \d\d:\d\d inbox: inbox message triage failed$")
        self.assertEqual(failstop.paused(project="PA")["paused"], True)
        self.assertFalse(failstop.paused(session=SID_B)["paused"])          # the other project goes on
        self.assertFalse(failstop.paused(session=SID_X)["paused"])          # so does a session with no project
        self.assertFalse(failstop.paused()["paused"])                       # and every install-wide check
        (subject, body), = self.alerts
        self.assertEqual(subject, "AFClaude STOPPED (PA): inbox message triage failed")
        self.assertIn("project PA is PAUSED until you clear it (`python3 tasks.py automation resume --project PA`)", body)
        ps = failstop.pauses()
        self.assertFalse(ps["install"]["paused"])
        self.assertEqual([p["name"] for p in ps["projects"]], ["PA"])
        r = self.fail("inbox message triage failed", "message #2", session=SID_A)    # a repeat: logged only
        self.assertEqual((r["already"], len(self.alerts)), (True, 1))
        self.fail("inbox message not delivered", "message #3", session=SID_A)       # a new one is added
        self.assertIn("triage failed | also: ", failstop.paused(session=SID_A)["reason"])
        self.assertFalse(failstop.paused(session=SID_B)["paused"])

    def test_install_wide_pause_holds_every_project(self):
        self.fail("usage reading failed")
        for sid in (SID_A, SID_B, SID_X, None):
            p = failstop.paused(session=sid)
            self.assertEqual((p["paused"], p["scope"]), (True, "install"), sid)
        self.fail("inbox message triage failed", session=SID_B)                     # both: both reasons
        p = failstop.paused(session=SID_B)
        self.assertEqual((p["scope"], p["project"]), ("install", "PB"))
        self.assertIn("inbox: usage reading failed | project PB: ", p["reason"])
        testenv.unpause()
        self.assertFalse(failstop.paused(session=SID_B)["paused"])

    def test_no_project_means_install_wide(self):
        r = self.fail("inbox message triage failed", session=SID_X)
        self.assertEqual((r["paused"], r["project"]), (True, None))
        self.assertTrue(failstop.paused()["paused"])
        self.assertIn("the install-wide pause instead of a project's: the session cccccccc is the task-manager "
                      "of no project", "\n".join(self.logs))
        self.assertTrue(self.alerts[0][0].startswith("AFClaude STOPPED: "))

    def test_unknown_project_counts_as_paused(self):
        p = failstop.paused(project="no such project")
        self.assertTrue(p["paused"])
        self.assertIn("no project", p["reason"])

    def test_runner_checks(self):
        """keepalive's checks ask for their session's project: the start, the window start and the
        fill-up of PA hold, PB's go on; the dispatcher / usage reviews (install-wide) go on."""
        from datetime import datetime, timezone
        import types
        import fillup
        import keepalive as ka
        self.fail("inbox message triage failed", session=SID_A)
        self.assertTrue(ka.automation_paused("x", SID_A))
        self.assertFalse(ka.automation_paused("x", SID_B))
        self.assertFalse(ka.automation_paused("x"))                                 # --work-on: install-wide
        self.assertEqual(ka.resume_hint(SID_A), "python3 tasks.py automation resume --project PA")
        self.assertTrue(ka.paused_reason(SID_A).startswith("project PA: "))
        now = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)
        args = types.SimpleNamespace(now=False, arm=False)
        with mock.patch.object(ka, "progress_note") as note, \
                mock.patch.object(ka, "fresh_usage", return_value=None) as usage:
            d = ka.window_start_pass(SID_A, now, args)
            self.assertTrue(d["reason"].startswith("PAUSED: automation is paused (project PA: "), d)
            self.assertIn("automation resume --project PA", note.call_args[0][0])
            usage.assert_not_called()
            d = ka.window_start_pass(SID_B, now, args)                              # past the pause check
            self.assertIn("usage reading failed", d["reason"])
        st = {"handled": {}, "fires": {}}
        with mock.patch.object(ka, "db_paused", return_value=True):
            self.assertEqual(ka.handle_fire(SID_A, {"uuid": "k1", "timestamp": now}, "r", st, args), "paused")
            self.assertEqual(ka.handle_fire(SID_B, {"uuid": "k2", "timestamp": now}, "r", st, args), "db_paused")
        seen = {}
        with mock.patch.object(fillup, "decide", side_effect=lambda **kw: seen.setdefault(kw["paused"], kw)), \
                mock.patch.object(ka, "run_active", return_value=None):
            ka.fillup_decision(SID_A, now, usage={})
            ka.fillup_decision(SID_B, now, usage={})
        self.assertEqual(set(seen), {True, False})
        self.assertFalse(failstop.paused()["paused"])            # what the dispatcher and usage_review read

    def test_resume_per_project(self):
        self.fail("inbox message triage failed", session=SID_A)
        self.fail("inbox message triage failed", session=SID_B)
        conn = store.connect()
        try:
            import actions
            actions.perform(conn, "automation.set_project", {"project": "PA", "paused": False}, actor="cli", via="cli")
        finally:
            conn.close()
        self.assertFalse(failstop.paused(session=SID_A)["paused"])
        self.assertTrue(failstop.paused(session=SID_B)["paused"])


class Migration(unittest.TestCase):
    """project_pauses is additive (D-244): an older database gains it on its next connect(), keeps
    its schema_version and data; until then the readers see no project pause (not an error)."""
    def test_older_database_gains_the_table(self):
        import tempfile
        d = tempfile.mkdtemp(dir=testenv.DIR)
        path = os.path.join(d, "old.db")
        conn = store.connect(path, create=True)
        try:
            import actions
            actions.perform(conn, "automation.set", {"paused": True, "reason": "kept"}, actor="cli", via="cli")
            store.add_project(conn, "Old")
            with store.transaction(conn):            # what the code before D-244's project pause left
                conn.execute("DROP TRIGGER project_pauses_version")
                conn.execute("DROP TABLE project_pauses")
        finally:
            conn.close()
        old = sqlite3.connect(path)
        old.row_factory = sqlite3.Row
        try:
            self.assertEqual(afclaude_config._project_pauses(old), [])
        finally:
            old.close()
        with mock.patch.object(store, "DB_PATH", path):
            ps = failstop.pauses()                          # the strict read before any connect()
            self.assertEqual((ps["install"]["paused"], ps["install"]["reason"], ps["projects"]), (True, "kept", []))
            conn = store.connect(path)
            try:
                self.assertEqual(store.get_meta(conn, "schema_version"), str(store.SCHEMA_VERSION))
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
                self.assertTrue({"project_pauses", "project_pauses_version"} <= names)
                actions.perform(conn, "automation.set_project", {"project": "Old", "paused": True, "reason": "r"},
                                actor="cli", via="cli")
                actions.perform(conn, "automation.set_project", {"project": "Old", "paused": True, "reason": "r2"},
                                actor="cli", via="cli")
                self.assertEqual(store.get_project_pause(conn, store.get_project(conn, "Old")["id"])["version"], 2)
            finally:
                conn.close()
            self.assertEqual([p["name"] for p in failstop.pauses()["projects"]], ["Old"])
            self.assertTrue(failstop.paused(project="Old")["paused"])


class ProjectCli(unittest.TestCase):
    """tasks.py automation status | resume [--project P | --all] | pause [REASON] [--project P]."""
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        self.addCleanup(testenv.managed_project("PA", SID_A))
        self.addCleanup(testenv.managed_project("PB", SID_B))

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tasks.main(list(argv))
        return rc, out.getvalue()

    def test_status_resume_pause(self):
        failstop.fail("inbox", "inbox message triage failed", session=SID_A, log=lambda m: None,
                      alert=lambda s, b="": None)
        rc, out = self.run_cli("automation", "pause", "maintenance", "--project", "PB")
        self.assertEqual(rc, 0)
        self.assertTrue(failstop.paused(session=SID_B)["paused"])
        self.assertFalse(failstop.paused()["paused"])
        rc, out = self.run_cli("automation")
        self.assertIn("automation running install-wide (not paused); paused project(s) below", out)
        self.assertIn("project PA PAUSED since ", out)
        self.assertIn("(by runner:inbox): ", out)
        self.assertIn("project PB PAUSED since ", out)
        self.assertIn("python3 tasks.py automation resume --project PA; python3 tasks.py automation resume "
                      "--project PB (or all at once: python3 tasks.py automation resume --all)", out)
        rc, out = self.run_cli("automation", "resume")                  # the install-wide one only
        self.assertTrue(failstop.paused(session=SID_A)["paused"])
        self.assertIn("project PA PAUSED", out)
        rc, out = self.run_cli("automation", "resume", "--project", "PA")
        self.assertFalse(failstop.paused(session=SID_A)["paused"])
        self.assertTrue(failstop.paused(session=SID_B)["paused"])
        self.assertNotIn("project PA", out)
        rc, out = self.run_cli("automation", "pause", "by hand")
        rc, out = self.run_cli("automation", "status", "--json")
        st = __import__("json").loads(out)
        self.assertEqual((st["paused"], st["reason"], [p["name"] for p in st["projects"]]), (True, "by hand", ["PB"]))
        rc, out = self.run_cli("automation", "resume", "--all")
        self.assertEqual(out.strip(), "automation running (not paused)")
        self.assertEqual(failstop.pauses()["projects"], [])
        rc, out = self.run_cli("automation", "resume", "--project", "PA", "--all")
        self.assertNotEqual(rc, 0)
        pid = failstop.paused(project="PB")                              # by id too
        conn = store.connect()
        try:
            pb = store.get_project(conn, "PB")["id"]
        finally:
            conn.close()
        self.run_cli("automation", "pause", "--project", str(pb))
        self.assertTrue(failstop.paused(project="PB")["paused"])
        self.assertFalse(pid["paused"])


if __name__ == "__main__":
    unittest.main()
