#!/usr/bin/env python3
"""Offline tests for the status snapshot (phase 3a S9(b)): the status_snapshot table and
actions.snapshot_put (a runner write, no audit row, replaced not appended, the size cap, a DB error
is raised), status_snapshot.build / run (sections, an error row instead of a stale value) and the
runner job that runs it once a minute, also while paused (the cadence tests are in test_runner)."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import keepalive as ka  # noqa: E402
import runner  # noqa: E402
import runner_jobs  # noqa: E402
import status_snapshot  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
W = dict(actor="runner:status_snapshot", via="runner", builder=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-snap-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = store.connect(os.path.join(self.tmp, "t.db"), create=True)
        self.addCleanup(self.conn.close)


class Table(Base):
    def test_rows_are_replaced_not_appended(self):
        actions.snapshot_put(self.conn, {"usage": {"payload": {"a": 1}}, "runner": {"payload": {"b": 2}}},
                             computed_at="2026-10-29T12:00:00.000Z", **W)
        actions.snapshot_put(self.conn, {"usage": {"payload": {"a": 3}}}, computed_at="2026-10-29T12:01:00.000Z", **W)
        rows = store.snapshot_rows(self.conn)
        self.assertEqual(sorted(rows), ["runner", "usage"])                          # still one row per section
        self.assertEqual(json.loads(rows["usage"]["payload"]), {"a": 3})
        self.assertEqual((rows["usage"]["computed_at"], rows["usage"]["version"]), ("2026-10-29T12:01:00.000Z", 1))
        self.assertEqual((rows["runner"]["state"], rows["runner"]["version"]), ("ok", 0))

    def test_an_error_section_replaces_the_old_value(self):
        actions.snapshot_put(self.conn, {"usage": {"payload": {"a": 1}}}, **W)
        actions.snapshot_put(self.conn, {"usage": {"error": "boom"}}, **W)
        r = store.snapshot_rows(self.conn)["usage"]
        self.assertEqual((r["state"], r["payload"], r["error"]), ("error", None, "boom"))   # no stale value kept

    def test_no_audit_row(self):
        """D-154 / D-256: a runner write with actor and via on the call, no audit_log row of its own."""
        before = len(store.audit_log(self.conn))
        actions.snapshot_put(self.conn, {"usage": {"payload": {"a": 1}}}, **W)
        self.assertEqual(len(store.audit_log(self.conn)), before)

    def test_only_runner_actors_and_valid_input(self):
        for bad in ({"actor": "mcp", "via": "mcp", "builder": 1}, {"actor": "cli", "via": "cli", "builder": 1}):
            with self.assertRaises(PermissionError):
                actions.snapshot_put(self.conn, {"usage": {"payload": {}}}, **bad)
        for secs in ({"Bad": {"payload": {}}}, {"x": {"payload": []}}, {"x": {"payload": {}, "error": "e"}},
                     {"x": {}}):
            with self.assertRaises(ValueError):
                actions.snapshot_put(self.conn, secs, **W)
        with self.assertRaises(ValueError):
            actions.snapshot_put(self.conn, {"x": {"payload": {}}}, **dict(W, builder=0))
        self.assertEqual(store.snapshot_rows(self.conn), {})                         # nothing half written

    def test_size_cap_and_one_transaction(self):
        big = {"x": "y" * actions.SNAPSHOT_MAX}
        with self.assertRaises(ValueError):
            actions.snapshot_put(self.conn, {"usage": {"payload": {"a": 1}}, "next_run": {"payload": big}}, **W)
        self.assertEqual(store.snapshot_rows(self.conn), {})                         # the good section was not written

    def test_the_state_check_and_the_account_default(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO status_snapshot(section, computed_at, state, builder) "
                              "VALUES ('x', 'now', 'maybe', 1)")
        actions.snapshot_put(self.conn, {"usage": {"payload": {}}}, **W)
        self.assertEqual(store.snapshot_rows(self.conn)["usage"]["account_id"], "default")

    def test_a_db_error_is_raised_and_reported(self):
        """D-234/D-235: no swallowed write failure: the §7.8 DB error path (runner=True), then the error."""
        err = store.DBUnavailable("database locked", "locked")
        with mock.patch.object(store, "put_snapshot", side_effect=sqlite3.OperationalError("disk I/O error")), \
                mock.patch.object(store, "retrying", side_effect=err), \
                mock.patch.object(store, "report_db_error") as rep:
            with self.assertRaises(store.DBError):
                actions.snapshot_put(self.conn, {"usage": {"payload": {}}}, **W)
        self.assertEqual(rep.call_args.kwargs["runner"], True)
        self.assertEqual(rep.call_args.kwargs["action"], "snapshot.put")


USAGE = {"fetched_at": NOW - timedelta(minutes=2),
         "session": {"percent": 10.0, "resets_at": NOW + timedelta(hours=3)},
         "weekly": {"percent": 40.0, "resets_at": NOW + timedelta(days=3)}}
DECISION = {"go": True, "reason": "ok", "headroom": 20.0, "text": "budget", "postpone": False, "mode": "night"}


class Build(Base):
    def build(self, usage=USAGE, **blocks):
        import export_quickview as qv
        patches = [mock.patch.object(ka, "read_usage_cache", return_value=usage),
                   mock.patch.object(ka, "budget_eval", return_value=DECISION),
                   mock.patch.object(qv, "next_run_block", blocks.get("next_run", lambda u, n, d=None: {"kind": "x"})),
                   mock.patch.object(qv, "threshold_block", blocks.get("threshold", lambda n, d=None: {"ratio": 1})),
                   mock.patch.object(qv, "automation_block", lambda: {"paused": False})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return status_snapshot.build(self.conn, NOW)

    def test_the_sections(self):
        actions.run_begin(self.conn, boot_id="a" * 32, job="sampler", key=None, timeout_s=60, actor="runner:sampler",
                          via="runner")
        actions.run_begin(self.conn, boot_id="a" * 32, job="status_snapshot", key=None, timeout_s=60,
                          actor="runner:status_snapshot", via="runner")
        s = self.build()
        self.assertEqual(sorted(s), sorted(status_snapshot.SECTIONS))
        u = s["usage"]["payload"]
        self.assertEqual((u["weekly"]["percent"], u["session"]["percent"], u["age_minutes"]), (40.0, 10.0, 2.0))
        self.assertEqual((u["budget"]["mode"], u["budget"]["go"]), ("night", True))
        self.assertEqual(s["next_run"], {"payload": {"kind": "x"}})
        run = s["runner"]["payload"]
        self.assertEqual(run["automation"], {"paused": False})
        self.assertEqual([r["job"] for r in run["running"]], ["sampler"])           # not its own running row

    def test_no_usage_cache_is_an_error_row_not_a_value(self):
        s = self.build(usage=None)
        self.assertIn("no usage cache", s["usage"]["error"])
        self.assertIn("payload", s["next_run"])               # the others are still built (as the quickview does)

    def test_a_failing_section_is_its_error_row(self):
        def boom(u, n, d=None):
            raise RuntimeError("model broke")
        s = self.build(next_run=boom, threshold=lambda n, d=None: {"error": "ValueError: bad"})
        self.assertEqual(s["next_run"], {"error": "RuntimeError: model broke"})
        self.assertEqual(s["threshold"], {"error": "ValueError: bad"})
        self.assertIn("payload", s["usage"])

    def test_an_oversized_section_is_an_error_row(self):
        s = self.build(next_run=lambda u, n, d=None: {"x": "y" * actions.SNAPSHOT_MAX})
        self.assertIn("over the", s["next_run"]["error"])

    def test_run_writes_the_rows_and_says_which_failed(self):
        note = status_snapshot.run(self.conn, NOW, builder=lambda c, n: {"usage": {"payload": {"a": 1}},
                                                                         "runner": {"error": "x"}})
        self.assertEqual(note, "status snapshot: 2 section(s), error in runner")
        rows = store.snapshot_rows(self.conn)
        self.assertEqual((rows["usage"]["state"], rows["runner"]["state"]), ("ok", "error"))
        self.assertEqual(rows["usage"]["computed_at"], "2026-10-29T12:00:00.000Z")

    def test_run_raises_on_a_write_failure(self):
        with mock.patch.object(actions, "snapshot_put", side_effect=store.DBUnavailable("locked", "locked")):
            with self.assertRaises(store.DBError):
                status_snapshot.run(self.conn, NOW, builder=lambda c, n: {"usage": {"payload": {}}})


class Job(Base):
    def test_the_runner_job(self):
        j = runner.JOBS["status_snapshot"]
        self.assertEqual((j.every, j.paused, j.pause_on_failure), (runner.MIN, "runs", True))   # also while paused
        self.assertIn("status_snapshot", runner.RUNNER_OWNS)
        self.assertEqual(runner_jobs.FUNCTIONS["status_snapshot"][1], runner_jobs.status_snapshot_tick)
        self.assertTrue(runner_jobs.log_path("status_snapshot").endswith("status_snapshot.log"))
        self.assertIn(actions.RUNNER_TIMEOUT + "status_snapshot", actions.SETTINGS)

    def test_the_tick_runs_the_snapshot(self):
        with mock.patch.object(status_snapshot, "run", return_value="status snapshot: 4 section(s)") as run:
            self.assertEqual(runner_jobs.status_snapshot_tick(), "status snapshot: 4 section(s)")
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
