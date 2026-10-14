#!/usr/bin/env python3
"""Offline tests for the runner state in the DB (dashboard phase 2d, D-161, runner_state.py):
the tables (runner_state with a trigger-bumped version, scheduled_jobs never deleted, app_log
immutable), actions.state_save (merge / file / replace, the delete policy, the audit row), the
importer of the legacy files (counts, idempotency, the marks, files untouched, imported
components retired), the DB as the only copy (concurrent read-modify-write of the keepalive
state in one process and in several, the legacy files never written or re-read once
imported), DB failures (the fallback record, db_gate's pause, alerts), side files, own
sessions and the at-shim's jobs. Each test uses its own temp DB and data dir; never touches
the live data/."""
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import store  # noqa: E402
import actions  # noqa: E402
import runner_state as rs  # noqa: E402
import keepalive as ka  # noqa: E402
import dispatcher as dp  # noqa: E402
import pacing  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KA_STATE = {"handled": {"window-start-2026-10-01": {"at": "2026-10-01T21:00:00+00:00", "plan": "resume",
                                                    "result": "continued-in-place"},
                        "last-mile-2026-10-02": {"at": "2026-10-02T12:00:00+00:00", "result": "dry-run"}},
            "fires": {"2026-10-01": 1},
            "limit_hits": {"u1": "2026-10-01T23:00:00+00:00"},
            "run_stops": {"2026-10-02T01:00:00+00:00": "s"}}
FILES = {
    "keepalive/keepalive_state.json": KA_STATE,
    "keepalive/keepalive_deferred.json": {"window-start-2026-10-03": {"session": "s", "recheck_at": "2026-10-03T22:00:00+00:00"}},
    "keepalive/keepalive_fillup.json": {"plan": {"key": "fillup-x", "status": "skip", "recheck_at": None}},
    "keepalive/usage_refresh_state.json": {"window_start": {"w1": {"fails": 0, "alerted": False}},
                                           "poke_at": "2026-10-01T00:00:00+00:00"},
    "dispatcher_state.json": {"sessions": {"a": {"sent_at": "2026-10-01T10:00:00+00:00"}}, "handled": {},
                              "starts": {}, "alerted": {}, "rc_held": {}, "last_skips": ["x", "y"]},
    "sampler_state.json": {"offsets": {"/p/a.jsonl": 10, "/p/b.jsonl": 20}, "sessions": {"a": {"n": 1}},
                           "last_sample_at": "2026-10-01T00:00:00+00:00", "prev_pct": {"weekly": 3.0}},
    "usage_review_state.json": {"next_run_at": "2026-10-29T15:00:00+00:00",
                                "runs": [{"at": "2026-10-01T15:00:00+00:00", "rc": 0}]},
}
LOGS = {
    "keepalive/keepalive.log": "[2026-10-01 00:11:50 CEST] keepalive start\n[2026-10-01 00:11:50 CEST] NO_STALL\n"
                               "[2026-10-01 00:11:50 CEST] NO_STALL\n",
    "dispatcher.log": "[2026-12-01 20:45:05 CET]   skip stalled a: undecided\n",
    "sampler.log": "2026-10-01T12:30:01.984664+00:00 tag=cron weekly=35.0\nTraceback (most recent call last):\n"
                   "  File \"x\", line 1\n2026-10-01T12:45:01+00:00 tag=cron weekly=35.0\n",
    "host_exec.log": "2026-10-01T10:20:36 OK claude agents --json\n2026-10-01T10:20:36 OK claude agents --json\n"
                     "2026-10-01T10:20:37 DENY not whitelisted: 'cat /etc/passwd'\n",
    "keepalive/watchdog.log": "start_watcher: a keepalive.py watcher runs on the HOST\n",
}
JOBS = [{"id": "4d6dda0d5724", "at": "2026-10-15T17:06:00+00:00", "cmd": "true\n", "cwd": "/"},
        {"id": "a4a2cedcd99a", "at": "2026-10-15T16:56:00+00:00", "cmd": "true\n", "cwd": "/"}]
OWN = "# own sessions\na954f3c8-e15e-41dd-9b25-b2f8b1a88a06\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(path + ".tmp", path)


def snapshot(d):
    out = {}
    for root, _, names in os.walk(d):
        for n in names:
            if n.startswith("afclaude.db"):
                continue
            p = os.path.join(root, n)
            with open(p, "rb") as fh:
                out[p] = (fh.read(), os.stat(p).st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="afclaude-runner-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        old = (store.DB_PATH, ka.STATE_FILE, ka.DEFER_FILE, ka.FILLUP_FILE, ka.USAGE_STATE_FILE,
               dp.STATE_FILE, dp.LOG_FILE, dp.DATA_DIR)
        self.addCleanup(self._restore, old)
        store.DB_PATH = os.path.join(self.dir, "afclaude.db")
        store.connect(store.DB_PATH, create=True).close()
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        kd = os.path.join(self.dir, "keepalive")
        os.makedirs(kd)
        ka.STATE_FILE, ka.DEFER_FILE = os.path.join(kd, "keepalive_state.json"), os.path.join(kd, "keepalive_deferred.json")
        ka.FILLUP_FILE, ka.USAGE_STATE_FILE = (os.path.join(kd, "keepalive_fillup.json"),
                                               os.path.join(kd, "usage_refresh_state.json"))
        dp.DATA_DIR = self.dir
        dp.STATE_FILE, dp.LOG_FILE = os.path.join(self.dir, "dispatcher_state.json"), os.path.join(self.dir, "dispatcher.log")

    @staticmethod
    def _restore(old):
        (store.DB_PATH, ka.STATE_FILE, ka.DEFER_FILE, ka.FILLUP_FILE, ka.USAGE_STATE_FILE,
         dp.STATE_FILE, dp.LOG_FILE, dp.DATA_DIR) = old

    def p(self, rel):
        return os.path.join(self.dir, rel)

    def fill(self):
        for rel, doc in FILES.items():
            write(self.p(rel), json.dumps(doc, indent=1))
        for rel, text in LOGS.items():
            write(self.p(rel), text)
        for j in JOBS:
            write(self.p(f"at_spool/{j['id']}.json"), json.dumps(j))
        write(self.p("own_sessions.txt"), OWN)

    def handled_in_db(self):
        return {json.loads(p)[1] for p in store.state_rows(self.conn, "keepalive")
                if json.loads(p)[0] == "handled"}


class Schema(Base):
    def test_tables_and_triggers(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"runner_state", "scheduled_jobs", "app_log"} <= names)
        self.assertEqual(store.get_meta(self.conn, "schema_version"), str(store.SCHEMA_VERSION))
        actions.app_log_append(self.conn, "keepalive", [("2026-10-01T00:00:00Z", "info", "a line")],
                               actor="runner:keepalive", via="runner")
        for sql in ("UPDATE app_log SET line='x'", "DELETE FROM app_log"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        actions.job_put(self.conn, JOBS[0], actor="runner:at", via="runner")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM scheduled_jobs")
        actions.state_save(self.conn, "keepalive", {"handled": {"k": 1}}, mode="file", actor="runner:keepalive",
                           via="runner")
        v0 = self.conn.execute("SELECT version FROM runner_state").fetchone()[0]
        actions.state_save(self.conn, "keepalive", {"handled": {"k": 2}}, mode="file", actor="runner:keepalive",
                           via="runner")
        self.assertEqual(self.conn.execute("SELECT version FROM runner_state").fetchone()[0], v0 + 1)
        audit = store.audit_log(self.conn, target_type="runner_state")
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]["actor"], "runner:keepalive")

    def test_app_log_retention_gate_and_batches(self):
        """store.prune_app_log (phase 2d part 2, D-161/D-130): the no-delete trigger still blocks
        an ordinary DELETE/UPDATE on app_log (before, during and after a prune); the gated path
        deletes only what's due, in several batches when there's more than one batch's worth, and
        always closes the gate again (even nothing-to-do and an error inside the loop)."""
        now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        old = store.iso(now - timedelta(days=40))
        fresh = store.iso(now - timedelta(days=1))
        entries = [(old, "info", f"line {i}") for i in range(25)] + [(fresh, "info", "keep me")]
        actions.app_log_append(self.conn, "host_exec", entries, actor="runner:host_exec", via="runner")
        self.assertEqual(self.gate(), 0)
        for sql in ("DELETE FROM app_log", "UPDATE app_log SET line='x'"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        cutoffs = {"host_exec": store.iso(now - timedelta(days=30))}
        deleted = store.prune_app_log(self.conn, cutoffs, batch_size=7)       # 25 rows, 7 at a time: 4 batches
        self.assertEqual(deleted, {"host_exec": 25})
        self.assertEqual(self.gate(), 0)                                     # closed again after the loop
        rows = self.conn.execute("SELECT line FROM app_log WHERE component='host_exec'").fetchall()
        self.assertEqual([r[0] for r in rows], ["keep me"])
        for sql in ("DELETE FROM app_log", "UPDATE app_log SET line='x'"):   # still blocked afterwards
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        self.assertEqual(store.prune_app_log(self.conn, cutoffs), {})        # nothing due: no-op, still closes

    def test_app_log_prune_counts_is_read_only(self):
        now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        actions.app_log_append(self.conn, "sampler",
                               [(store.iso(now - timedelta(days=d)), "info", f"{d}d") for d in (1, 40, 45)],
                               actor="runner:sampler", via="runner")
        cutoffs = {"sampler": store.iso(now - timedelta(days=30))}
        self.assertEqual(store.app_log_prune_counts(self.conn, cutoffs), {"sampler": 2})
        self.assertEqual(self.count("app_log"), 3)                           # the count changed nothing
        self.assertEqual(self.gate(), 0)

    def test_prune_logs_cli_dry_run_and_real(self):
        now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        actions.app_log_append(self.conn, "at_shim",
                               [(store.iso(now - timedelta(days=d)), "info", f"{d}d") for d in (1, 40)],
                               actor="runner:at_shim", via="runner")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.main(["prune-logs", "--db", store.DB_PATH, "--dry-run"])
        self.assertEqual(rc, 0)
        preview = json.loads(out.getvalue())
        self.assertEqual(preview["would_delete"].get("at_shim"), 1)
        self.assertEqual(self.count("app_log"), 2)                           # the dry-run changed nothing

        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            rc = store.main(["prune-logs", "--db", store.DB_PATH, "--batch-size", "1"])
        self.assertEqual(rc, 0)
        result = json.loads(out2.getvalue())
        self.assertEqual(result["deleted"].get("at_shim"), 1)
        self.assertEqual(self.count("app_log"), 1)
        rows = store.audit_log(self.conn, target_type="app_log")
        self.assertEqual(rows[0]["actor"], "runner:retention")
        self.assertEqual(rows[0]["via"], "runner")

        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            rc = store.main(["prune-logs", "--db", store.DB_PATH, "--batch-size", "0"])    # invalid: a clean error
        self.assertNotEqual(rc, 0)

    def gate(self):
        return self.conn.execute("SELECT allowed FROM app_log_prune_gate WHERE id=1").fetchone()[0]

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_validation(self):
        for bad in ({"component": "nope"}, {"actor": "evil person"}, {"via": "web"}):
            kw = dict(component="keepalive", actor="runner:keepalive", via="runner") | bad
            with self.assertRaises(ValueError):
                actions.state_save(self.conn, kw["component"], {}, mode="file", actor=kw["actor"], via=kw["via"])
        with self.assertRaises(ValueError):
            actions.state_save(self.conn, "keepalive", {}, mode="merge", actor="runner:keepalive", via="runner")
        with self.assertRaises(ValueError):
            actions.app_log_append(self.conn, "keepalive", [("", "info", "two\nlines")], actor="runner:keepalive",
                                   via="runner")
        # a runner that never deletes: removing a key is refused, nothing written
        actions.state_save(self.conn, "usage_review", {"a": 1, "b": 2}, mode="file", actor="runner:usage_review",
                           via="runner")
        with self.assertRaises(ValueError):
            actions.state_save(self.conn, "usage_review", {"a": 1}, mode="file", actor="runner:usage_review",
                               via="runner")
        self.assertEqual(len(store.state_rows(self.conn, "usage_review")), 2)

    def test_flatten_round_trip(self):
        for comp, rel in rs.STATE_FILES.items():
            doc = FILES.get(rel, {"x": [1, 2]})
            back = actions.state_unflatten(comp, actions.state_flatten(comp, doc))
            want = {p: {} for p in actions.state_component(comp).split} | doc
            self.assertEqual(back, want, comp)
        rows = actions.state_flatten("keepalive", {"handled": "not a map"})   # stored whole
        self.assertEqual(rows, {'["handled"]': '"not a map"'})

    def test_log_line_times_and_levels(self):
        self.assertEqual(rs.line_ts("[2026-10-01 00:11:50 CEST] x"), "2026-09-30T22:11:50Z")
        self.assertEqual(rs.line_ts("[2026-12-01 20:45:05 CET] x"), "2026-12-01T19:45:05Z")
        self.assertEqual(rs.line_ts("2026-10-01T12:30:01.984664+00:00 tag"), "2026-10-01T12:30:01.984Z")
        self.assertEqual(rs.line_ts("2026-10-01T10:20:36 OK x"), "2026-10-01T10:20:36Z")
        self.assertIsNone(rs.line_ts("Traceback (most recent call last):"))
        self.assertEqual(rs.line_level("Traceback (most recent call last):"), "error")
        self.assertEqual(rs.line_level("2026 DENY not whitelisted"), "warn")
        self.assertEqual(rs.line_level("pass (ARMED): continue 0"), "info")


class Importer(Base):
    def test_import_counts_idempotent_files_untouched(self):
        self.fill()
        before = snapshot(self.dir)
        r = rs.import_all()
        self.assertEqual([s["status"] for s in r["state"]], ["ok"] * 7)
        self.assertEqual(r["own_sessions"]["status"], "ok")
        self.assertEqual(r["scheduled_jobs"]["status"], "ok")
        self.assertEqual({s["component"]: s["status"] for s in r["logs"]},
                         {"keepalive": "ok", "dispatcher": "ok", "sampler": "ok", "watchdog": "ok",
                          "host_exec": "ok", "at_shim": "no file"})
        counts = r["counts"]
        self.assertEqual(counts["app_log"], {"keepalive": 3, "dispatcher": 1, "sampler": 4, "watchdog": 1,
                                             "host_exec": 3})
        self.assertEqual(counts["scheduled_jobs"], {"pending": 2})
        self.assertEqual(counts["runner_state"]["keepalive"], 5)
        self.assertEqual(snapshot(self.dir), before)                     # files untouched
        r2 = rs.import_all()                                             # again: the imported ones are retired
        self.assertEqual([s["status"] for s in r2["state"] if s["status"] != "no file"], [rs.RETIRED] * 7)
        self.assertEqual((r2["own_sessions"]["status"], r2["scheduled_jobs"]["status"]), (rs.RETIRED, rs.RETIRED))
        self.assertEqual(sum(s.get("inserted", 0) for s in r2["logs"]), 0)   # the logs are still synced
        self.assertEqual(r2["counts"], counts)
        self.assertEqual(snapshot(self.dir), before)
        # the traceback line got the previous line's time; levels derived
        rows = self.conn.execute("SELECT ts, level, line FROM app_log WHERE component='sampler' ORDER BY id").fetchall()
        self.assertEqual(rows[1]["ts"], rows[0]["ts"])
        self.assertEqual(rows[1]["level"], "error")
        self.assertIn("a954f3c8-e15e-41dd-9b25-b2f8b1a88a06", actions.autonomous_sessions(self.conn))
        self.assertEqual(store.get_driven_session(self.conn, "a954f3c8-e15e-41dd-9b25-b2f8b1a88a06")["kind"], "own")

    def test_parity_db_vs_file(self):
        self.fill()
        rs.import_all()
        for comp, rel in rs.STATE_FILES.items():
            got = rs.load(comp, self.p(rel))
            self.assertIsInstance(got, rs.StateDoc, comp)
            want = {p: {} for p in actions.state_component(comp).split} | FILES.get(rel, {})
            self.assertEqual(got, want, comp)
            self.assertEqual(rs.read_json(self.p(rel)), want)
        self.assertEqual(ka.load_state(), KA_STATE)
        self.assertIsInstance(ka.load_state(), rs.StateDoc)
        st = dp.load_state()
        self.assertIsInstance(st, rs.StateDoc)
        self.assertEqual(st, FILES["dispatcher_state.json"])
        # a reader of the fire files gets the DB; the frozen legacy file no longer counts
        files = [(self.p("keepalive/keepalive_state.json"), "handled", "at"),
                 (self.p("dispatcher_state.json"), "sessions", "sent_at")]
        from_db = pacing.fire_times(files)
        write(self.p("dispatcher_state.json"), json.dumps({"sessions": {"z": {"sent_at": "2026-10-09T10:00:00+00:00"}}}))
        self.assertEqual(pacing.fire_times(files), from_db)

    def test_log_import_counts_copies_and_spares_dual_written_lines(self):
        self.fill()
        line = "[2026-10-01 00:11:50 CEST] NO_STALL"
        rs.log_lines("keepalive", line)                                   # the dual-write stored one copy
        r = rs.import_log(self.conn, "keepalive", self.p("keepalive/keepalive.log"))
        self.assertEqual((r["inserted"], r["present"], r["missing"]), (2, 1, 0))
        n = self.conn.execute("SELECT COUNT(*) FROM app_log WHERE line=?", (line,)).fetchone()[0]
        self.assertEqual(n, 2)                                            # the file has it twice
        with open(self.p("keepalive/keepalive.log"), "a") as fh:          # a line only the file has
            fh.write("[2026-10-01 00:12:00 CEST] new\n")
        r = rs.import_log(self.conn, "keepalive", self.p("keepalive/keepalive.log"))
        self.assertEqual(r["inserted"], 1)


class Concurrency(Base):
    def setUp(self):
        super().setUp()
        self.fill()
        rs.import_all()
        self.files = snapshot(self.dir)

    def assertFilesUntouched(self):
        self.assertEqual(snapshot(self.dir), self.files)                  # the legacy files are frozen

    def test_two_writers_merge_per_entry(self):
        a, b = ka.load_state(), ka.load_state()        # the watcher and the cron run, both from the DB
        a["handled"]["k1"] = {"at": "2026-10-05T21:00:00+00:00", "result": "continued-in-place"}
        ka.save_state(a)
        b["handled"]["k2"] = {"at": "2026-10-05T21:00:01+00:00", "result": "dry-run"}
        b["fires"]["2026-10-05"] = 1
        ka.save_state(b)
        st = ka.load_state()                                          # both
        self.assertTrue({"k1", "k2"} <= set(st["handled"]))
        self.assertEqual(st["fires"]["2026-10-05"], 1)
        # a writer that saves again changes only what it changed itself
        a["handled"]["k1"]["result"] = "no-reply-within-timeout"
        ka.save_state(a)
        st = ka.load_state()
        self.assertEqual(st["handled"]["k1"]["result"], "no-reply-within-timeout")
        self.assertIn("k2", st["handled"])
        self.assertFilesUntouched()

    def test_deferral_consumed_while_another_is_added(self):
        a, b = ka.load_deferred(), ka.load_deferred()
        a.pop("window-start-2026-10-03")                 # consumed
        ka.save_deferred(a)
        b["window-start-2026-10-04"] = {"session": "s", "recheck_at": "2026-10-04T22:00:00+00:00"}
        ka.save_deferred(b)
        self.assertEqual(set(ka.load_deferred()), {"window-start-2026-10-04"})
        # defer_window_start builds a new map (older starts are over): replaces the top-level keys
        ka.defer_window_start("window-start-2026-10-05", "s", datetime(2026, 10, 5, 22, tzinfo=timezone.utc), "r")
        self.assertEqual(set(ka.load_deferred()), {"window-start-2026-10-05"})
        self.assertEqual(set(store.state_rows(self.conn, "keepalive.deferred")), {'["window-start-2026-10-05"]'})
        self.assertFilesUntouched()

    def test_processes_never_lose_entries(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_worker, args=(i, 12)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        want = {f"p{i}-{n}" for i in range(4) for n in range(12)}
        self.assertTrue(want <= self.handled_in_db(), sorted(want - self.handled_in_db())[:5])
        self.assertTrue(want <= set(ka.load_state()["handled"]))
        self.assertFilesUntouched()

    def test_frozen_legacy_file_is_ignored_and_never_reimported(self):
        st = json.loads(json.dumps(KA_STATE))
        st["handled"]["by-old-watcher"] = {"at": "2026-10-06T21:00:00+00:00"}
        write(ka.STATE_FILE, json.dumps(st))                          # a runner on older code, a hand edit
        self.assertNotIn("by-old-watcher", ka.load_state()["handled"])   # the DB is the only copy
        a = ka.load_state()
        a["handled"]["k1"] = {"at": "x"}
        ka.save_state(a)
        r = rs.import_state(self.conn, "keepalive", ka.STATE_FILE)    # would revert k1: refused
        self.assertEqual(r["status"], rs.RETIRED)
        self.assertEqual(self.handled_in_db(), set(KA_STATE["handled"]) | {"k1"})

    def test_file_mode_save_keeps_other_writers_entries(self):
        a = ka.load_state()
        a["handled"]["k1"] = {"at": "x"}
        ka.save_state(a)
        plain = json.loads(json.dumps(KA_STATE))                      # a state built anew
        plain["handled"]["k2"] = {"at": "y"}
        ka.save_state(plain)
        self.assertTrue({"k1", "k2"} <= self.handled_in_db())
        self.assertFilesUntouched()

    def test_run_stop_survives_the_watchers_older_state(self):
        """--run-stopped (D-224) merges into the state as it is: a watcher that loaded its state
        before the stop and saves afterwards (merge or file mode) never drops it from the DB."""
        a, plain = ka.load_state(), json.loads(json.dumps(KA_STATE))
        t = datetime(2026, 10, 5, 23, 40, tzinfo=timezone.utc)
        ka.mark_run_stopped("s1", t)
        a["handled"]["k1"] = {"at": "x"}
        ka.save_state(a)
        plain["handled"]["k2"] = {"at": "y"}
        ka.save_state(plain)
        self.assertEqual(ka.load_state()["run_stops"][t.isoformat()], "s1")
        paths = [json.loads(p) for p in store.state_rows(self.conn, "keepalive")]
        self.assertIn(["run_stops", t.isoformat()], paths)                 # one row per stop
        audit = self.conn.execute("SELECT after FROM audit_log WHERE action='runner_state.save' "
                                  "ORDER BY id").fetchall()
        self.assertTrue(any(t.isoformat() in r[0] for r in audit))


def _worker(i, n):
    store.DB_PATH = store.DB_PATH   # inherited (fork)
    for k in range(n):
        st = ka.load_state()
        st["handled"][f"p{i}-{k}"] = {"at": f"2026-10-0{1 + k % 9}T00:00:00+00:00", "worker": i}
        if not ka.save_state(st) and not rs.save("keepalive", st):
            os._exit(1)
    os._exit(0)


class Failures(Base):
    """A DB failure: nothing silently lost (the fallback file, §7.8), the runners paused by
    db_gate, the frozen legacy files never read or written instead."""
    def setUp(self):
        super().setUp()
        self.fill()
        rs.import_all()
        self.files = snapshot(self.dir)
        self.alerts = []
        p = mock.patch.object(store, "NOTIFY", lambda subject, body="": self.alerts.append(subject) or "recorded")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(rs._LOAD_FAILED.clear)                       # a failed load pauses this process's starts

    def events(self):
        return store.fallback_events(store.fallback_path())

    def break_db(self):
        self.conn.close()
        with open(store.DB_PATH, "wb") as fh:
            fh.write(b"this is not a database" * 100)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(store.DB_PATH + suffix):
                os.remove(store.DB_PATH + suffix)
        other = tempfile.mkdtemp(prefix="afclaude-runner-state-other-")
        self.addCleanup(shutil.rmtree, other, True)
        self.conn = store.connect(os.path.join(other, "other.db"), create=True)

    def test_broken_db_records_the_save_and_pauses(self):
        self.break_db()
        st = ka.load_state()                                          # an empty state, never the file
        self.assertIsInstance(st, rs.StateDoc)
        self.assertEqual(st["handled"], {})
        self.assertIsNotNone(store.db_gate("keepalive"))              # the runners pause (one alert)
        st["handled"]["k9"] = {"at": "z"}
        self.assertFalse(rs.save("keepalive", st))                    # never raises
        ka.save_state(st)
        failed = [e for e in self.events() if e.get("event") == "failed"]
        self.assertEqual([e["action"] for e in failed], ["runner_state.save"] * 2)
        self.assertIn('["handled", "k9"]', failed[0]["params"]["set"])   # the change is kept for a replay
        self.assertEqual(len(self.alerts), 1)                          # db_gate's alert only
        self.assertFalse(rs.log_lines("dispatcher", "x"))
        self.assertTrue(dp.log("still logged"))                         # the log files stay
        self.assertEqual({k: v for k, v in snapshot(self.dir).items() if "ALERTS" not in k and "log" not in k},
                         {k: v for k, v in self.files.items() if "log" not in k})

    def test_unreadable_state_starts_nothing(self):
        self.break_db()
        st = ka.load_state()
        self.assertTrue(st.unavailable)
        self.assertIsNotNone(rs.load_failed())
        with mock.patch.object(ka, "preflight") as pf, mock.patch.object(ka, "log"):
            r = ka.handle_fire("00000000-0000-4000-8000-00000000000a", {"uuid": "k", "timestamp": None}, "test",
                               st, mock.Mock(arm=True))
        self.assertEqual(r, "db_paused")                               # not handled: decided again later
        pf.assert_not_called()
        import usage_review as ur
        with mock.patch.object(ur, "run") as run, mock.patch.object(ur, "run_once") as once, \
                mock.patch.object(sys, "argv", ["usage_review.py", "--now"]), mock.patch.object(ka, "log"):
            ur.main()
        run.assert_not_called()
        once.assert_not_called()

    def test_repeated_failure_alerts_once_without_a_pause(self):
        with mock.patch.object(actions, "state_save", side_effect=store.DBUnavailable("disk i/o error", "io")):
            self.assertFalse(rs.save("dispatcher", {"sessions": {"a": 1}}))
            self.assertEqual(self.alerts, [])                         # the first failure: recorded only
            self.assertFalse(rs.save("dispatcher", {"sessions": {"b": 2}}))
        self.assertEqual(len(self.alerts), 1)                          # the retry failed too: the owner
        self.assertIn("runner_state.save failed again", self.alerts[0])

    def test_missing_db(self):
        os.remove(self.p("afclaude.db"))
        self.assertEqual(ka.load_state(), {"handled": {}, "fires": {}, "limit_hits": {}, "run_stops": {}})   # not the file
        self.assertEqual(rs.own_sessions(), set())
        with self.assertRaises(OSError):
            rs.read_json(ka.STATE_FILE)
        self.assertFalse(rs.save("keepalive", {"handled": {}}))
        self.assertEqual([e["action"] for e in self.events() if e.get("event") == "failed"], ["runner_state.save"])

    def test_side_paths_are_files_and_never_touch_the_db(self):
        other = os.path.join(self.dir, "elsewhere", "keepalive_state.json")
        self.assertFalse(rs.is_live("keepalive", other))
        self.assertEqual(rs.load("keepalive", other, {"handled": {}}), {"handled": {}})
        before = store.state_rows(store.connect(), "keepalive")
        self.assertTrue(rs.save("keepalive", {"handled": {"x": 1}}, other))
        self.assertEqual(rs.load("keepalive", other), {"handled": {"x": 1}})
        self.assertEqual(store.state_rows(store.connect(), "keepalive"), before)
        self.assertFalse(rs.log_lines("dispatcher", "x", os.path.join(self.dir, "elsewhere.log")))


class OwnSessionsStalled(Base):
    """stalled.py reads the own sessions through own_sessions(): the DB (driven_sessions kind 'own')."""
    DB_ONLY = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        super().setUp()
        self.fill()
        self.live = self.p("own_sessions.txt")

    def test_stalled_scan_flags_a_db_only_own_session(self):
        import stalled
        rs.import_all()
        actions.own_session_add(self.conn, self.DB_ONLY, actor="runner:import", via="runner")
        store.upsert_session(self.conn, self.DB_ONLY, path="/nowhere.jsonl", cwd="/somewhere")
        self.conn.commit()
        proj = os.path.join(self.dir, "projects")
        os.makedirs(proj)
        stalled.scan(self.conn, proj, self.live)
        own = self.conn.execute("SELECT own FROM sessions WHERE session_id=?", (self.DB_ONLY,)).fetchone()[0]
        self.assertEqual(own, 1)


class Writes(Base):
    def test_dispatcher_log_and_state(self):
        self.fill()
        dp.log("pass (ARMED): continue 0")
        tail = store.app_log_tail(self.conn, "dispatcher", 5)
        self.assertTrue(tail[-1].endswith("pass (ARMED): continue 0"))
        r = rs.import_log(self.conn, "dispatcher", dp.LOG_FILE)   # not doubled by the importer
        self.assertEqual(r["inserted"], 1)                          # only the pre-existing file line
        files = snapshot(self.dir)
        st = dp.load_state()                                        # never imported: the legacy file
        self.assertNotIsInstance(st, rs.StateDoc)
        self.assertEqual(st["last_skips"], ["x", "y"])
        st["sessions"]["b"] = {"sent_at": "2026-10-02T10:00:00+00:00"}
        dp.save_state(st)                                           # the whole state into the DB, marked
        self.assertIn('["sessions", "b"]', store.state_rows(self.conn, "dispatcher"))
        self.assertIn('["sessions", "a"]', store.state_rows(self.conn, "dispatcher"))
        self.assertIsNotNone(store.get_meta(self.conn, rs.MARK + "dispatcher"))
        self.assertIsInstance(dp.load_state(), rs.StateDoc)          # from now on the DB
        self.assertEqual({k: v for k, v in snapshot(self.dir).items() if "log" not in k},
                         {k: v for k, v in files.items() if "log" not in k})   # the state file untouched

    def test_new_component_without_a_file(self):
        self.assertEqual(dict(ka.load_fillup()), {})
        ka.save_fillup({"plan": {"key": "fillup-x"}})
        self.assertEqual(ka.load_fillup()["plan"]["key"], "fillup-x")
        self.assertFalse(os.path.exists(ka.FILLUP_FILE))
        self.assertIsNotNone(store.get_meta(self.conn, rs.MARK + "keepalive.fillup"))

    def test_own_sessions(self):
        self.fill()
        sid = "a954f3c8-e15e-41dd-9b25-b2f8b1a88a06"
        self.assertEqual(rs.own_sessions(), {sid})                    # never imported: the legacy list
        new = "b954f3c8-e15e-41dd-9b25-b2f8b1a88a07"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rs.main(["own", "add", new]), 0)         # imports the list first, then adds
        self.assertEqual(out.getvalue().strip(), "added")
        self.assertEqual(rs.own_sessions(), {sid, new})
        write(self.p("own_sessions.txt"), "c954f3c8-e15e-41dd-9b25-b2f8b1a88a08\n")   # the frozen file: ignored
        self.assertEqual(rs.own_sessions(), {sid, new})
        self.assertIn(new, actions.autonomous_sessions(self.conn))
        self.assertEqual(rs.own_sessions(os.path.join(self.dir, "side.txt")), set())   # a side list: its file

    def _shim(self):
        spec = importlib.util.spec_from_file_location("at_shim", os.path.join(HERE, "docker", "at_shim.py"))
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        shim.SPOOL = os.path.join(self.dir, "at_spool")
        return shim

    def test_at_shim_jobs(self):
        shim = self._shim()
        jid = shim.spool(datetime(2020, 1, 1, tzinfo=timezone.utc), "true\n")
        self.assertEqual(store.get_job(self.conn, jid)["status"], "pending")
        self.assertFalse(os.path.exists(shim.SPOOL))                 # no spool file any more
        self.assertEqual(shim.run_due(), 0)
        self.assertEqual(store.get_job(self.conn, jid)["status"], "started")
        self.assertEqual(shim.run_due(), 0)                          # never twice
        self.assertEqual([a["action"] for a in store.audit_log(self.conn, target_type="scheduled_job")],
                         ["job.started", "job.put"])
        later = shim.spool(datetime(2099, 1, 1, tzinfo=timezone.utc), "true\n")
        shim.run_due()
        self.assertEqual(store.get_job(self.conn, later)["status"], "pending")   # not due yet
        r = rs.import_jobs(self.conn, shim.SPOOL)                    # the DB owns the jobs: never marks them
        self.assertEqual(r["status"], rs.RETIRED)
        self.assertEqual(store.get_job(self.conn, later)["status"], "pending")

    def test_at_shim_adopts_a_legacy_spool_file(self):
        shim = self._shim()
        self.fill()
        rs.import_all()                                              # the two spooled jobs: in the DB
        old = {"id": "0123456789ab", "at": "2020-01-01T00:00:00+00:00", "cmd": "true\n", "cwd": "/"}
        write(os.path.join(shim.SPOOL, "0123456789ab.json"), json.dumps(old))   # an older shim copy's job
        files = snapshot(shim.SPOOL)
        shim.run_due()
        self.assertEqual(store.get_job(self.conn, "0123456789ab")["status"], "started")
        self.assertEqual(snapshot(shim.SPOOL), files)                 # read-only: nothing deleted
        self.assertEqual({j["id"]: j["status"] for j in store.list_jobs(self.conn)},
                         {"0123456789ab": "started", "4d6dda0d5724": "pending", "a4a2cedcd99a": "pending"})

    def test_at_shim_db_failure(self):
        shim = self._shim()
        self.conn.close()
        os.remove(store.DB_PATH)
        with mock.patch.object(sys, "stdin", io.StringIO("true\n")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(shim.main(["-t", "209901010000"]), 1)
        failed = [e for e in store.fallback_events(store.fallback_path()) if e.get("event") == "failed"]
        self.assertEqual(failed[0]["action"], "job.put")
        self.assertEqual(failed[0]["params"]["job"]["cmd"], "true\n")   # kept for a replay
        self.assertEqual(shim.run_due(), 1)
        self.conn = store.connect(os.path.join(self.dir, "other.db"), create=True)


if __name__ == "__main__":
    unittest.main()
