#!/usr/bin/env python3
"""Offline tests for the runner state in the DB (dashboard phase 2d, D-161, runner_state.py):
the tables (runner_state with a trigger-bumped version, scheduled_jobs never deleted, app_log
immutable), actions.state_save (merge / file / replace, the delete policy, the audit row), the
importer (counts, idempotency, the marks, files untouched), the readers' DB-vs-file parity,
the concurrent read-modify-write of keepalive_state.json (two writers in one process, and
several processes), the fallback to the file (a file changed outside the DB, a broken DB) and
the dual-writes (dispatcher log, at-shim jobs). Each test uses its own temp DB and data dir;
never touches the live data/."""
import importlib.util
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

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
            "limit_hits": {"u1": "2026-10-01T23:00:00+00:00"}}
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
        self.assertEqual([s["status"] for s in r["state"]], ["ok"] * 4 + ["no file"] + ["ok"] * 3)
        self.assertEqual(r["own_sessions"]["status"], "ok")
        self.assertEqual(r["scheduled_jobs"]["status"], "ok")
        self.assertEqual({s["component"]: s["status"] for s in r["logs"]},
                         {"keepalive": "ok", "dispatcher": "ok", "sampler": "ok", "watchdog": "ok",
                          "host_exec": "ok", "at_shim": "no file"})
        counts = r["counts"]
        self.assertEqual(counts["app_log"], {"keepalive": 3, "dispatcher": 1, "sampler": 4, "watchdog": 1,
                                             "host_exec": 3})
        self.assertEqual(counts["scheduled_jobs"], {"pending": 2})
        self.assertEqual(counts["runner_state"]["keepalive"], 4)
        self.assertEqual(snapshot(self.dir), before)                     # files untouched
        r2 = rs.import_all()                                             # again: nothing new
        self.assertEqual([s["status"] for s in r2["state"] if s["status"] != "no file"], ["in sync"] * 7)
        self.assertEqual(sum(s.get("inserted", 0) for s in r2["logs"]), 0)
        self.assertEqual(r2["scheduled_jobs"]["same"], 2)
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
            if rel not in FILES:
                self.assertIsNone(rs.db_load(comp, self.p(rel)))
                continue
            got = rs.db_load(comp, self.p(rel))
            self.assertIsInstance(got, rs.StateDoc, comp)
            self.assertEqual(got, {p: {} for p in actions.state_component(comp).split} | FILES[rel], comp)
            self.assertEqual(rs.read_json(self.p(rel)), got)
        self.assertEqual(ka.load_state(), KA_STATE)
        self.assertIsInstance(ka.load_state(), rs.StateDoc)
        st = dp.load_state()
        self.assertIsInstance(st, rs.StateDoc)
        self.assertEqual(st, FILES["dispatcher_state.json"])
        # a reader of the fire files gets the same from the DB as from the file
        files = [(self.p("keepalive/keepalive_state.json"), "handled", "at"),
                 (self.p("dispatcher_state.json"), "sessions", "sent_at")]
        from_db = pacing.fire_times(files)
        os.utime(self.p("dispatcher_state.json"))   # now changed outside the DB: the file is read
        os.utime(self.p("keepalive/keepalive_state.json"))
        self.assertIsNone(rs.db_load("keepalive", self.p("keepalive/keepalive_state.json")))
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

    def test_two_writers_merge_per_entry(self):
        a, b = ka.load_state(), ka.load_state()        # the watcher and the cron run, both from the DB
        a["handled"]["k1"] = {"at": "2026-10-05T21:00:00+00:00", "result": "continued-in-place"}
        ka.save_state(a)
        b["handled"]["k2"] = {"at": "2026-10-05T21:00:01+00:00", "result": "dry-run"}
        b["fires"]["2026-10-05"] = 1
        ka.save_state(b)
        with open(ka.STATE_FILE) as fh:
            self.assertNotIn("k1", json.load(fh)["handled"])         # the file: last writer wins (as before)
        st = ka.load_state()                                          # the DB: both
        self.assertTrue({"k1", "k2"} <= set(st["handled"]))
        self.assertEqual(st["fires"]["2026-10-05"], 1)
        # a writer that saves again changes only what it changed itself
        a["handled"]["k1"]["result"] = "no-reply-within-timeout"
        ka.save_state(a)
        st = ka.load_state()
        self.assertEqual(st["handled"]["k1"]["result"], "no-reply-within-timeout")
        self.assertIn("k2", st["handled"])

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
        self.assertEqual(set(rs.db_load("keepalive.deferred") or {}), {"window-start-2026-10-05"})

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
        self.assertTrue(want <= set(ka.load_state()["handled"]) or rs.db_load("keepalive") is None)

    def test_file_changed_outside_the_db_and_resync(self):
        st = json.loads(json.dumps(KA_STATE))
        st["handled"]["by-old-watcher"] = {"at": "2026-10-06T21:00:00+00:00"}
        write(ka.STATE_FILE, json.dumps(st))                          # a runner on older code
        self.assertIsNone(rs.db_load("keepalive"))
        self.assertIn("by-old-watcher", ka.load_state()["handled"])   # read from the file
        self.assertNotIsInstance(ka.load_state(), rs.StateDoc)
        r = rs.import_state(self.conn, "keepalive", ka.STATE_FILE)
        self.assertEqual(r["status"], "ok")
        self.assertIn("by-old-watcher", rs.db_load("keepalive")["handled"])

    def test_file_mode_save_keeps_other_writers_entries(self):
        a = ka.load_state()
        a["handled"]["k1"] = {"at": "x"}
        ka.save_state(a)
        plain = json.loads(json.dumps(KA_STATE))                      # read from the file before a's save
        plain["handled"]["k2"] = {"at": "y"}
        ka.save_state(plain)
        self.assertTrue({"k1", "k2"} <= self.handled_in_db())


def _worker(i, n):
    store.DB_PATH = store.DB_PATH   # inherited (fork)
    for k in range(n):
        st = ka.load_state()
        st["handled"][f"p{i}-{k}"] = {"at": f"2026-10-0{1 + k % 9}T00:00:00+00:00", "worker": i}
        for _ in range(20):
            try:   # the file write's fixed .tmp name races between processes (kept as is): retry it
                ka.save_state(st)
                break
            except FileNotFoundError:
                continue
    os._exit(0)


class Fallback(Base):
    def setUp(self):
        super().setUp()
        self.fill()
        rs.import_all()

    def test_broken_db_reads_and_writes_the_file(self):
        self.conn.close()
        with open(store.DB_PATH, "wb") as fh:
            fh.write(b"this is not a database" * 100)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(store.DB_PATH + suffix):
                os.remove(store.DB_PATH + suffix)
        self.assertIsNone(rs.db_load("keepalive"))
        st = ka.load_state()
        self.assertEqual(st, KA_STATE)                                # the file
        st["handled"]["k9"] = {"at": "z"}
        ka.save_state(st)                                             # never raises
        with open(ka.STATE_FILE) as fh:
            self.assertIn("k9", json.load(fh)["handled"])
        self.assertFalse(rs.save("keepalive", st))
        self.assertFalse(rs.log_lines("dispatcher", "x"))
        self.assertTrue(dp.log("still logged"))
        self.conn = store.connect(os.path.join(self.dir, "other.db"), create=True)

    def test_missing_db_and_not_imported(self):
        os.remove(self.p("afclaude.db"))
        self.assertIsNone(rs.db_load("keepalive"))
        self.assertEqual(ka.load_state(), KA_STATE)

    def test_not_live_paths_never_touch_the_db(self):
        other = os.path.join(self.dir, "elsewhere", "keepalive_state.json")
        self.assertFalse(rs.is_live("keepalive", other))
        self.assertIsNone(rs.db_load("keepalive", other))
        self.assertFalse(rs.save("keepalive", {"handled": {}}, other))
        self.assertFalse(rs.log_lines("dispatcher", "x", os.path.join(self.dir, "elsewhere.log")))


class DualWrite(Base):
    def test_dispatcher_log_and_state(self):
        self.fill()
        dp.log("pass (ARMED): continue 0")
        tail = store.app_log_tail(self.conn, "dispatcher", 5)
        self.assertTrue(tail[-1].endswith("pass (ARMED): continue 0"))
        r = rs.import_log(self.conn, "dispatcher", dp.LOG_FILE)   # not doubled by the importer
        self.assertEqual(r["inserted"], 1)                          # only the pre-existing file line
        st = dp.load_state()                                        # not imported: the file
        st["sessions"]["b"] = {"sent_at": "2026-10-02T10:00:00+00:00"}
        dp.save_state(st)
        self.assertIn('["sessions", "b"]', store.state_rows(self.conn, "dispatcher"))

    def test_at_shim_jobs(self):
        spec = importlib.util.spec_from_file_location("at_shim", os.path.join(HERE, "docker", "at_shim.py"))
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        shim.SPOOL = os.path.join(self.dir, "at_spool")
        jid = shim.spool(datetime(2020, 1, 1, tzinfo=timezone.utc), "true\n")
        self.assertEqual(store.get_job(self.conn, jid)["status"], "pending")
        shim.run_due()
        self.assertEqual(store.get_job(self.conn, jid)["status"], "started")
        self.assertEqual([a["action"] for a in store.audit_log(self.conn, target_type="scheduled_job")],
                         ["job.started", "job.put"])
        r = rs.import_jobs(self.conn, shim.SPOOL)                    # nothing left to do
        self.assertEqual((r["status"], r["files"], r["marked_started"]), ("ok", 0, 0))


if __name__ == "__main__":
    unittest.main()
