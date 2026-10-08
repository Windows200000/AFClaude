#!/usr/bin/env python3
"""The DB error path (docs/dashboard_design.md §7.8, owner D-171): fault injection for each
error class (a locked DB, a read-only file, a corrupted file, a newer schema), the hand-back
to the CLI, the fallback alert file with its once-per-episode alert, and the runners pausing
on a broken DB and resuming on their own. (The MCP hand-back: test_mcp_server.py, DBErrors.)
"""
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB and data dir; before the AFClaude imports)
import store  # noqa: E402
import actions  # noqa: E402
import tasks  # noqa: E402


def busy(msg="database is locked"):
    return sqlite3.OperationalError(msg)


class Base(unittest.TestCase):
    """A temp data dir with a DB holding one task; alerts recorded (never pushed); no real sleeps."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "afclaude.db")
        c = store.connect(self.db, create=True)   # the explicit act a real install's setup performs
        store.add_task(c, "one")
        c.close()
        self.alerts, self.sleeps = [], []
        for name, value in (("NOTIFY", lambda s, b="": self.alerts.append((s, b)) or "sent"),
                            ("_sleep", self.sleeps.append), ("BUSY_TIMEOUT", 0.05),
                            ("DB_PATH", self.db)):
            p = mock.patch.object(store, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._cleanup_spare)

    def _cleanup_spare(self):
        os.chmod(self.dir, 0o755)
        for p in (store._spare_path(self.fallback),):
            if os.path.exists(p):
                os.unlink(p)

    @property
    def fallback(self):
        return os.path.join(self.dir, "ALERTS.fallback.md")

    def events(self, kind=None):
        return [e for e in store.fallback_events(self.fallback) if kind is None or e["event"] == kind]

    def read_only(self):
        os.chmod(self.db, 0o444)
        self.addCleanup(os.chmod, self.db, 0o644)

    def corrupt(self):
        with open(self.db, "wb") as fh:
            fh.write(b"this is not an SQLite file" * 200)
        for ext in ("-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.unlink(self.db + ext)

    def add(self, conn, title="x", actor="cli", via="cli"):
        return actions.perform(conn, "task.add", {"title": title}, actor=actor, via=via)


class Classify(Base):
    def test_classes(self):
        cases = [(busy(), ("transient", "locked")), (busy("database table is locked"), ("transient", "locked")),
                 (sqlite3.OperationalError("disk I/O error"), ("transient", "io")),
                 (sqlite3.OperationalError("attempt to write a readonly database"), ("persistent", "read_only")),
                 (sqlite3.OperationalError("database or disk is full"), ("persistent", "disk_full")),
                 (sqlite3.DatabaseError("database disk image is malformed"), ("persistent", "corrupt")),
                 (sqlite3.DatabaseError("file is not a database"), ("persistent", "corrupt")),
                 (sqlite3.OperationalError("unable to open database file"), ("persistent", "missing")),
                 (sqlite3.IntegrityError("UNIQUE constraint failed: projects.name"), ("caller", "constraint")),
                 (ValueError("bad"), ("caller", "invalid")), (store.NotFound("no task"), ("caller", "invalid")),
                 (actions.Conflict("stale"), ("caller", "invalid")),
                 (store.SchemaTooNew("newer", 9, 4), ("persistent", "schema")),
                 (RuntimeError("a bug"), None)]
        for exc, want in cases:
            self.assertEqual(store.classify(exc), want, exc)


class Transient(Base):
    def test_retry_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise busy()
            return "ok"
        self.assertEqual(store.retrying(fn), "ok")
        self.assertEqual(self.sleeps, list(store.RETRY_DELAYS[:2]))      # backoff

    def test_retries_exhaust_to_persistent(self):
        def fn():
            raise busy()
        with self.assertRaises(store.DBUnavailable) as cm:
            store.retrying(fn)
        e = cm.exception
        self.assertEqual((e.error_class, e.kind), ("persistent", "locked"))
        self.assertIn("still failing after 5 tries", str(e))
        self.assertEqual(self.sleeps, list(store.RETRY_DELAYS))
        self.assertLessEqual(sum(store.RETRY_DELAYS), 10)

    def test_caller_and_persistent_errors_are_not_retried(self):
        def integrity():
            raise sqlite3.IntegrityError("UNIQUE constraint failed: x")
        with self.assertRaises(ValueError):
            store.retrying(integrity)

        def readonly():
            raise sqlite3.OperationalError("attempt to write a readonly database")
        with self.assertRaises(store.DBUnavailable):
            store.retrying(readonly)
        self.assertEqual(self.sleeps, [])

    def test_a_real_lock_on_the_write_path(self):
        """Another process holds the write lock: perform retries; released during the backoff,
        the write goes through; held throughout, it ends as a recorded, persistent error."""
        conn = store.connect(self.db)
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            def release(delay):
                self.sleeps.append(delay)
                if len(self.sleeps) == 2 and holder.in_transaction:
                    holder.execute("ROLLBACK")
            with mock.patch.object(store, "_sleep", release):
                self.assertEqual(self.add(conn, "after the lock")["title"], "after the lock")
            self.assertEqual(len(self.sleeps), 2)
            self.assertEqual(self.events(), [])                          # fixed by retrying: nothing to report
            holder.execute("BEGIN IMMEDIATE")
            self.sleeps.clear()
            with self.assertRaises(store.DBUnavailable) as cm:
                self.add(conn, "never")
            self.assertEqual(cm.exception.kind, "locked")
            self.assertEqual(len(self.sleeps), 4)
            [rec] = self.events("failed")
            self.assertEqual((rec["action"], rec["params"], rec["kind"]), ("task.add", {"title": "never"}, "locked"))
        finally:
            holder.close()
            conn.close()
        self.assertEqual(self.alerts, [])                                # one failure: no alert yet


class _FakeCursor:
    def fetchone(self):
        return None            # no "meta" table: schema_problem() reads this as a fresh/healthy DB

    def fetchall(self):
        return []


class _FakeConn:
    """Enough of sqlite3.Connection's surface for store.transaction(): a real Connection's
    commit/rollback can't be monkeypatched (read-only C attributes), so this fakes them."""
    def __init__(self):
        self.in_transaction = False
        self.log = []
        self.fail_commits = 0

    def execute(self, sql, params=()):
        self.log.append(sql)
        if sql == "BEGIN IMMEDIATE":
            self.in_transaction = True
        return _FakeCursor()

    def commit(self):
        if self.fail_commits > 0:
            self.fail_commits -= 1
            raise sqlite3.OperationalError("simulated commit failure")
        self.in_transaction = False

    def rollback(self):
        self.in_transaction = False


class TransactionCommitFailure(unittest.TestCase):
    """store.py ~814: commit() used to run outside transaction()'s try/except, so a commit that
    raised left the BEGIN IMMEDIATE transaction open (never rolled back); retrying()'s next
    attempt on the same connection would then see conn.in_transaction=True and run as a
    SAVEPOINT nested inside that still-open, uncommitted transaction instead of a fresh
    BEGIN IMMEDIATE, so a later successful retry's RELEASE never actually committed anything.
    The commit now runs inside the try, so a failed commit is rolled back and a retry starts
    clean."""
    def test_a_failed_commit_is_rolled_back_so_a_retry_starts_a_fresh_transaction(self):
        conn = _FakeConn()
        conn.fail_commits = 1
        with self.assertRaises(sqlite3.OperationalError):
            with store.transaction(conn):
                pass
        self.assertFalse(conn.in_transaction)      # rolled back, not left open (store.py ~814)
        with store.transaction(conn):              # a retry: its own fresh BEGIN IMMEDIATE
            pass
        self.assertNotIn("SAVEPOINT afclaude_tx", conn.log)    # never nested in the stale transaction
        self.assertEqual(conn.log.count("BEGIN IMMEDIATE"), 2)


class Persistent(Base):
    def test_read_only_file(self):
        self.read_only()
        conn = store.connect(self.db)                                   # opens read-only
        try:
            self.assertEqual([t["title"] for t in store.list_tasks(conn)], ["one"])   # reads go on
            with self.assertRaises(store.DBError) as cm:
                self.add(conn, actor="mcp:abc", via="mcp")
            e = cm.exception
            self.assertEqual((e.error_class, e.kind), ("persistent", "read_only"))
            self.assertEqual(self.sleeps, [])                           # not retried
            [rec] = self.events("failed")
            self.assertEqual((rec["actor"], rec["action"], rec["params"], rec["write"]),
                             ("mcp:abc", "task.add", {"title": "x"}, True))
            self.assertEqual(len(rec["payload_sha256"]), 64)
            self.assertTrue(rec["time"].endswith("Z"))
            self.assertIn("recorded in " + self.fallback, e.not_saved)
            self.assertIn("not escalated yet", e.escalation)
            self.assertEqual(self.alerts, [])
            # the session retries once and it fails again: escalate (D-171), once per episode
            with self.assertRaises(store.DBError) as cm:
                self.add(conn, actor="mcp:abc", via="mcp")
            self.assertIn("has been alerted", cm.exception.escalation)
            self.assertEqual(len(self.alerts), 1)
            self.assertIn("read_only", self.alerts[0][0])
            for title in ("x", "y"):                                     # more failures: recorded, no new alert
                with self.assertRaises(store.DBError) as cm:
                    self.add(conn, title, actor="mcp:abc", via="mcp")
                self.assertIn("already alerted", cm.exception.escalation)
            self.assertEqual(len(self.alerts), 1)
            self.assertEqual(len(self.events("failed")), 4)
            self.assertEqual(len(self.events("alert")), 1)
        finally:
            conn.close()
        self.assertEqual(store.health(self.db).kind, "read_only")

    def test_corrupted_file(self):
        self.corrupt()
        with self.assertRaises(store.DBError) as cm:
            store.connect(self.db)
        self.assertEqual(cm.exception.kind, "corrupt")
        self.assertEqual(store.health(self.db).kind, "corrupt")

    def test_corrupted_pages_fail_quick_check(self):
        c = store.connect(self.db)
        for i in range(200):
            store.add_task(c, "t" * 300)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        with open(self.db, "r+b") as fh:
            fh.seek(4096 * 3)
            fh.write(b"\xff" * 4096 * 4)
        p = store.health(self.db)
        self.assertEqual(p.kind, "corrupt")
        self.assertIn("corrupt", str(p))

    def test_newer_schema(self):
        c = sqlite3.connect(self.db)
        c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(store.SCHEMA_VERSION + 1),))
        c.commit()
        c.close()
        conn = store.connect(self.db)
        try:
            with self.assertRaises(store.SchemaTooNew) as cm:          # step 1's SchemaMismatch, now a DBError
                self.add(conn)
            self.assertEqual(store.handback(cm.exception)["kind"], "schema")
            self.assertEqual(len(self.events("failed")), 1)
        finally:
            conn.close()
        self.assertIsInstance(store.health(self.db), store.SchemaTooNew)

    def test_missing_and_full(self):
        self.assertEqual(store.health(os.path.join(self.dir, "nope.db")).kind, "missing")
        with mock.patch.object(store, "MIN_FREE_BYTES", 1 << 62):
            self.assertEqual(store.health(self.db).kind, "disk_full")
        self.assertIsNone(store.health(self.db))

    def test_caller_errors_are_not_recorded(self):
        conn = store.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                self.add(conn, "  ")
            with self.assertRaises(actions.Conflict):
                actions.perform(conn, "task.edit", {"task_id": 1, "title": "z", "version": 99},
                                actor="cli", via="cli")
        finally:
            conn.close()
        self.assertEqual((self.events(), self.sleeps), ([], []))

    def test_fallback_survives_a_read_only_data_dir(self):
        """The data volume itself can't be written: the record goes to the spare copy."""
        self.read_only()
        os.chmod(self.dir, 0o555)
        conn = store.connect(self.db)
        try:
            with self.assertRaises(store.DBError) as cm:
                self.add(conn)
        finally:
            conn.close()
            os.chmod(self.dir, 0o755)
        self.assertFalse(os.path.exists(self.fallback))
        self.assertIn(store._spare_path(self.fallback), cm.exception.not_saved)
        self.assertEqual(len(self.events("failed")), 1)


class MissingRecheck(Base):
    """health(): kind "missing" (and "unable to open", classified the same way) gets one
    re-check after a short wait before being reported, so a one-off open failure (a momentary
    mount hiccup) doesn't by itself page the owner (D-171 addendum: only if trying again
    doesn't fix it)."""
    def test_a_one_off_missing_file_recovers_before_the_recheck(self):
        os.unlink(self.db)

        def fixed_by_the_recheck(delay):
            self.sleeps.append(delay)
            store.connect(self.db, create=True).close()    # "the mount reappears"
        with mock.patch.object(store, "_sleep", fixed_by_the_recheck):
            self.assertIsNone(store.health(self.db))
        self.assertEqual(self.sleeps, [store.MISSING_RECHECK_DELAY])

    def test_a_genuinely_missing_file_is_still_reported_after_the_recheck(self):
        p = store.health(os.path.join(self.dir, "nope.db"))
        self.assertEqual(p.kind, "missing")
        self.assertEqual(self.sleeps, [store.MISSING_RECHECK_DELAY])   # one re-check, then reported


class MissingDB(unittest.TestCase):
    """Creating a database is an explicit act (D-171, D-187): connect() never does it as a
    side effect of a missing file, only create=True (the init path) does; and health() tells
    a database that came back empty after really holding data from one that is simply new."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "afclaude.db")

    def test_connect_refuses_a_missing_file_and_creates_nothing(self):
        with self.assertRaises(store.DBUnavailable) as cm:
            store.connect(self.db)
        self.assertEqual(cm.exception.kind, "missing")
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(os.listdir(self.dir), [])             # not even an empty file was left behind
        self.assertEqual(store.health(self.db).kind, "missing")

    def test_create_true_is_the_explicit_init_path(self):
        conn = store.connect(self.db, create=True)
        conn.close()
        self.assertTrue(os.path.isfile(self.db))
        self.assertIsNone(store.health(self.db))
        raw = sqlite3.connect(self.db)
        self.assertIsNotNone(raw.execute("SELECT value FROM meta WHERE key='install_id'").fetchone())
        raw.close()
        store.connect(self.db).close()                         # the file now exists: ordinary connects work

    def test_init_cli_creates_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.main(["init", "--db", self.db])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(self.db))
        self.assertEqual(json.loads(out.getvalue())["created"], True)
        self.assertIsNone(store.health(self.db))
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):                  # a directory that already has one: used as is
            self.assertEqual(store.main(["init", "--db", self.db]), 0)
        self.assertEqual(json.loads(out2.getvalue())["created"], False)

    def test_recreated_empty_is_flagged_even_though_the_file_exists(self):
        conn = store.connect(self.db, create=True)
        store.add_task(conn, "real work")
        conn.close()
        self.assertIsNone(store.health(self.db))
        # the data volume loses just the DB file; a schema backup from before survives, and
        # whatever remounts it leaves a bare empty file at the same path (no store.py init: no
        # install marker, like a stray placeholder from a bad mount would be)
        open(self.db + ".v3-20260101T000000Z.bak", "w").close()
        os.unlink(self.db)
        open(self.db, "w").close()
        problem = store.health(self.db)
        self.assertIsNotNone(problem)
        self.assertEqual(problem.kind, "replaced")
        self.assertIn("evidence of a previous install", str(problem))

    def test_samples_jsonl_also_counts_as_evidence(self):
        with open(os.path.join(self.dir, "samples.jsonl"), "w"):
            pass
        open(self.db, "w").close()
        self.assertEqual(store.health(self.db).kind, "replaced")

    def test_a_genuinely_fresh_empty_db_is_healthy(self):
        """No .bak, no samples.jsonl: nothing suggests a previous install, so an empty,
        markerless database (e.g. one a bare `sqlite3 path` would leave) is accepted."""
        open(self.db, "w").close()
        self.assertIsNone(store.health(self.db))


class CLI(Base):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.main(["--db", self.db, *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_write_on_a_read_only_db(self):
        self.read_only()
        rc, _, err = self.cli("list")
        self.assertEqual(rc, 0)
        rc, _, err = self.cli("add", "new")
        self.assertEqual(rc, 1)
        lines = err.strip().splitlines()
        self.assertTrue(lines[0].startswith("error: database read_only (persistent): "), err)
        self.assertTrue(lines[1].startswith("not saved: task.add was not applied; recorded in"), err)
        self.assertTrue(lines[2].startswith("escalation: "), err)
        self.assertEqual(lines[3], "next step: " + store.NEXT_STEP)
        self.assertNotIn("Traceback", err)

    def test_corrupted_db(self):
        self.corrupt()
        rc, _, err = self.cli("list")
        self.assertEqual(rc, 1)
        self.assertIn("error: database corrupt (persistent)", err)
        self.assertIn("not saved: nothing was changed by this call", err)
        self.assertEqual(self.events("failed")[0]["action"], "cli:list")


class SeparateAlertEpisodes(Base):
    """store.py ~2021/2039: an interactive request failure (report_db_error, e.g. an MCP read)
    and a runner pause (db_gate) must not share one alert episode -- otherwise a request-failure
    episode could make db_gate wrongly announce "automation resumes" for a pause that never
    happened, or suppress a real runner-pause alert because a request episode was already open."""
    def test_healthy_db_gate_does_not_resume_for_an_open_request_episode(self):
        store._alert(self.fallback, "a request failed twice", "body", "mcp:abc", scope="request")
        self.assertEqual(len(self.alerts), 1)
        self.assertIsNone(store.db_gate("test"))                 # the database itself is healthy
        self.assertEqual(len(self.alerts), 1)                    # no "automation resumes" alert
        self.assertNotIn("recovered", [e["event"] for e in self.events()])

    def test_runner_pause_alert_is_not_suppressed_by_an_open_request_episode(self):
        self.read_only()
        conn = store.connect(self.db)
        try:
            for _ in range(2):                                    # opens a "request" episode
                with self.assertRaises(store.DBError):
                    self.add(conn, actor="mcp:abc", via="mcp")
        finally:
            conn.close()
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("failed again", self.alerts[0][0])
        # a real runner pause, same broken database: must still alert (a different scope)
        problem = store.db_gate("test")
        self.assertIsNotNone(problem)
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("automation paused", self.alerts[1][0])
        # healthy again: db_gate closes its own (runner) episode with its own resume alert
        os.chmod(self.db, 0o644)
        self.assertIsNone(store.db_gate("test"))
        self.assertEqual(len(self.alerts), 3)
        self.assertIn("healthy again", self.alerts[2][0])


class SparePathHardening(Base):
    """store.py ~1891 _spare_path: the spare copy (used when the data volume itself can't be
    written) now lives in a per-user directory (not a bare predictable name in shared /tmp),
    created 0700, and is opened O_NOFOLLOW so a symlink planted at that name is refused."""
    def test_spare_dir_is_per_user_and_0700(self):
        d = os.path.dirname(store._spare_path(self.fallback))
        self.assertEqual(os.path.basename(d), f"afclaude-{os.getuid()}")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_spare_dir_tightens_a_loosened_permission(self):
        d = os.path.dirname(store._spare_path(self.fallback))
        os.chmod(d, 0o755)
        self.assertEqual(store._spare_dir(), d)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_spare_write_refuses_a_planted_symlink(self):
        """The data volume can't be written (read-only dir): the record falls to the spare
        copy; a symlink already sitting at that predictable name must be refused, not written
        through (O_NOFOLLOW), so an attacker who can plant one in the shared temp dir can't
        redirect the write."""
        spare = store._spare_path(self.fallback)
        target = os.path.join(self.dir, "not-this-file.md")
        os.symlink(target, spare)
        try:
            os.chmod(self.dir, 0o555)
            try:
                store.report_db_error(store.DBUnavailable("simulated", "locked"), actor="cli",
                                      action="task.add", write=False, db_path=self.db)
            finally:
                os.chmod(self.dir, 0o755)
            self.assertFalse(os.path.exists(target))          # never followed or written through
            self.assertTrue(os.path.islink(spare))             # the symlink itself untouched
        finally:
            os.unlink(spare)


class Runners(Base):
    """keepalive and the dispatcher start nothing on a broken DB, alert once per episode and
    resume on their own (with an alert) once it is healthy again."""
    def setUp(self):
        super().setUp()
        import keepalive as ka
        import dispatcher as dp
        self.ka, self.dp = ka, dp
        self.logs, self.notes, self.preflights = [], [], []
        for obj, name, value in ((ka, "log", self.logs.append), (ka, "progress_note", self.notes.append),
                                 (ka, "save_state", lambda st: None),
                                 (ka, "preflight", lambda sid: self.preflights.append(sid) or (False, ["stop"], "-")),
                                 (ka, "alert", lambda s, b="": None), (ka, "_DB_PAUSE", None),
                                 (dp, "log", self.logs.append), (dp, "DATA_DIR", self.dir),
                                 (dp, "STATE_FILE", os.path.join(self.dir, "dispatcher_state.json")),
                                 (dp, "LOCK_FILE", os.path.join(self.dir, "dispatcher.lock")),
                                 (dp, "CONFIG_FILE", os.path.join(self.dir, "dispatcher.json"))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def fire(self, key):
        st = {"handled": {}, "fires": {}}
        self.ka.handle_fire("00000000-0000-4000-8000-00000000000a", {"uuid": key, "timestamp": None},
                            "window start", st, mock.Mock(arm=False))
        return st

    def test_keepalive_pauses_and_resumes(self):
        self.read_only()
        for key in ("k1", "k2"):
            st = self.fire(key)
            self.assertEqual(st["handled"], {})                         # not handled: decided again later
        self.assertEqual(self.preflights, [])                           # no start
        self.assertEqual(len(self.alerts), 1)                           # once per episode
        self.assertIn("automation paused", self.alerts[0][0])
        self.assertEqual(sum("PAUSED" in m for m in self.logs), 1)
        os.chmod(self.db, 0o644)
        st = self.fire("k3")
        self.assertEqual(len(self.preflights), 1)                       # starts again on its own
        self.assertIn("preflight-failed", json.dumps(st["handled"]))
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("healthy again", self.alerts[1][0])
        self.assertIn("autonomous starts resume", " ".join(self.logs))
        self.assertEqual([e["event"] for e in self.events()], ["alert", "recovered"])

    def test_dispatcher_skips_its_pass(self):
        self.corrupt()
        with mock.patch.object(store, "connect", side_effect=AssertionError("connected to a broken DB")):
            for _ in range(2):
                self.assertEqual(self.dp.main(["--arm", "--no-scan"]), 0)
            self.assertEqual(self.dp.main([]), 0)                       # a dry run: checks, never alerts
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(sum("pass skipped" in m for m in self.logs), 2)   # armed: once per problem
        c = store.connect(os.path.join(self.dir, "fresh.db"), create=True)
        c.close()
        os.replace(os.path.join(self.dir, "fresh.db"), self.db)
        with mock.patch.object(self.dp, "run_pass", return_value={"continue": [], "cleanup": [], "skip": []}) as rp:
            self.assertEqual(self.dp.main(["--arm", "--no-scan"]), 0)
        rp.assert_called_once()
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("healthy again", self.alerts[1][0])
        self.assertIn("database healthy again: passes resume", self.logs)

    def test_healthy_db_no_alerts(self):
        self.assertIsNone(store.db_gate("test"))
        self.assertEqual((self.alerts, self.events()), ([], []))


if __name__ == "__main__":
    unittest.main()
