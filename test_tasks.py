#!/usr/bin/env python3
"""Offline tests for the task store (store.py v2) and tasks.py (temp DBs only)."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import tasks  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
SID3 = "00000000-0000-4000-8000-000000000003"

# Goal-2 schema exactly as shipped in 09357f4 (schema_version 1), for the migration test.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, project_dir TEXT, path TEXT, cwd TEXT, title TEXT, title_rank INTEGER,
    first_seen TEXT, last_activity TEXT, own INTEGER NOT NULL DEFAULT 0,
    last_scanned_offset INTEGER NOT NULL DEFAULT 0, last_scanned_size INTEGER NOT NULL DEFAULT 0,
    last_msg_uuid TEXT, last_msg_type TEXT, last_msg_ts TEXT, stalled INTEGER NOT NULL DEFAULT 0,
    stalled_since TEXT, stall_kind TEXT, stall_reset_at TEXT, stall_text TEXT, stall_uuid TEXT,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS limit_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(session_id),
    entry_uuid TEXT UNIQUE, ts TEXT, kind TEXT, reset_at TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS limit_hits_session ON limit_hits(session_id, ts);
CREATE INDEX IF NOT EXISTS sessions_stalled ON sessions(stalled);
INSERT INTO meta(key, value) VALUES ('schema_version', '1');
"""


class Clock:
    """Deterministic store clock: every call advances one second."""
    def __init__(self):
        self.t = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "db", "t.db")
        self._orig_now = store._utcnow
        store._utcnow = Clock()
        self.conn = store.connect(self.db)

    def tearDown(self):
        store._utcnow = self._orig_now
        self.conn.close()
        self.tmp.cleanup()

    def stall(self, sid, uuid="n1", cwd="/home/x/proj", project_dir="-home-x-proj"):
        store.upsert_session(self.conn, sid, cwd=cwd, project_dir=project_dir, stalled=1,
                             stalled_since="2026-09-29T09:00:00.000Z", stall_kind="session",
                             stall_reset_at="2026-09-29T12:00:00Z", stall_uuid=uuid, stall_text="hit")
        self.conn.commit()

    def unstall(self, sid):
        store.upsert_session(self.conn, sid, stalled=0, stalled_since=None, stall_uuid=None)
        self.conn.commit()

    def events(self, tid):
        return [e["event"] for e in store.task_events(self.conn, tid)]


class Lifecycle(Base):
    def test_add_defaults(self):
        t = store.add_task(self.conn, "  Write MCP server ", project="/p", created_by_session=SID)
        self.assertEqual((t["title"], t["status"], t["kind"], t["priority"]),
                         ("Write MCP server", "pending", "task", 0))
        self.assertEqual(t["created_by_session"], SID)
        self.assertTrue(t["created_at"].endswith("Z") and "." in t["created_at"])
        self.assertEqual(t["created_at"], t["updated_at"])
        self.assertEqual(self.events(t["id"]), ["created"])

    def test_block_answer_back_to_pending(self):
        t = store.add_task(self.conn, "x")
        t = store.start_task(self.conn, t["id"], SID)
        t = store.block_task(self.conn, t["id"], "Which port?")
        self.assertEqual((t["status"], t["blocked_question"]), ("blocked", "Which port?"))
        self.assertIsNotNone(t["blocked_at"])
        self.assertIsNone(store.next_ready_task(self.conn))
        t = store.answer_task(self.conn, t["id"], "8765")
        self.assertEqual((t["status"], t["answer"]), ("pending", "8765"))
        self.assertEqual(t["blocked_question"], "Which port?")   # kept for the next run
        self.assertEqual(t["assigned_session"], SID)            # so the dispatcher can resume it
        self.assertIsNotNone(t["answered_at"])
        self.assertEqual(store.next_ready_task(self.conn)["id"], t["id"])
        # blocking again clears the old answer
        t = store.block_task(self.conn, t["id"], "And the host?")
        self.assertIsNone(t["answer"])
        self.assertIsNone(t["answered_at"])

    def test_start_finish(self):
        t = store.add_task(self.conn, "x")
        t = store.start_task(self.conn, t["id"], SID)
        self.assertEqual((t["status"], t["assigned_session"]), ("in_progress", SID))
        self.assertEqual(store.start_task(self.conn, t["id"], SID)["status"], "in_progress")  # idempotent
        with self.assertRaises(store.InvalidTransition):
            store.start_task(self.conn, t["id"], SID2)
        t = store.finish_task(self.conn, t["id"], "built it")
        self.assertEqual((t["status"], t["result_summary"]), ("done", "built it"))
        self.assertIsNotNone(t["done_at"])
        self.assertEqual(self.events(t["id"]), ["created", "started", "done"])

    def test_invalid_transitions(self):
        t = store.add_task(self.conn, "x")
        with self.assertRaises(store.InvalidTransition):
            store.answer_task(self.conn, t["id"], "a")          # not blocked
        store.block_task(self.conn, t["id"], "q")
        for fn in (lambda: store.start_task(self.conn, t["id"], SID),
                   lambda: store.finish_task(self.conn, t["id"], "s"),
                   lambda: store.reopen_task(self.conn, t["id"])):
            with self.assertRaises(store.InvalidTransition):
                fn()
        store.cancel_task(self.conn, t["id"], "not needed")
        for fn in (lambda: store.block_task(self.conn, t["id"], "q"),
                   lambda: store.cancel_task(self.conn, t["id"])):
            with self.assertRaises(store.InvalidTransition):
                fn()
        self.assertEqual(store.reopen_task(self.conn, t["id"])["status"], "pending")
        with self.assertRaises(store.NotFound):
            store.block_task(self.conn, 999, "q")
        self.assertIsNone(store.get_task(self.conn, 999))

    def test_failed_transition_changes_nothing(self):
        t = store.add_task(self.conn, "x")
        n = len(store.task_events(self.conn, t["id"]))
        with self.assertRaises(store.InvalidTransition):
            store.answer_task(self.conn, t["id"], "a")
        self.assertEqual(store.get_task(self.conn, t["id"]), t)
        self.assertEqual(len(store.task_events(self.conn, t["id"])), n)
        self.assertFalse(self.conn.in_transaction)

    def test_update_and_priority(self):
        t = store.add_task(self.conn, "x", description="d")
        t2 = store.update_task(self.conn, t["id"], title="y", description="d")
        self.assertEqual(t2["title"], "y")
        self.assertGreater(t2["updated_at"], t["updated_at"])
        store.update_task(self.conn, t["id"], title="y")           # no change -> no event
        store.set_priority(self.conn, t["id"], 7)
        store.set_priority(self.conn, t["id"], -2)
        ev = store.task_events(self.conn, t["id"])
        self.assertEqual([e["event"] for e in ev], ["created", "updated", "priority", "priority"])
        self.assertEqual(ev[1]["detail"], {"title": ["x", "y"]})
        self.assertEqual(ev[3]["detail"], {"from": 7, "to": -2})
        with self.assertRaises(ValueError):
            store.update_task(self.conn, t["id"], status="done")    # not whitelisted
        with self.assertRaises(ValueError):
            store.update_task(self.conn, t["id"], title="  ")

    def test_event_log_complete_and_append_only(self):
        t = store.add_task(self.conn, "x", priority=1)
        tid = t["id"]
        store.set_priority(self.conn, tid, 3)
        store.start_task(self.conn, tid, SID)
        store.block_task(self.conn, tid, "q?")
        store.answer_task(self.conn, tid, "a!")
        store.start_task(self.conn, tid, SID2)
        store.reopen_task(self.conn, tid, "run died")
        store.cancel_task(self.conn, tid, "obsolete")
        ev = store.task_events(self.conn, tid)
        self.assertEqual([e["event"] for e in ev], ["created", "priority", "started", "blocked", "answered",
                                                    "started", "reopened", "cancelled"])
        self.assertEqual(ev[0]["detail"], {"title": "x", "priority": 1, "kind": "task"})
        self.assertEqual(ev[3]["detail"], {"from": "in_progress", "question": "q?"})
        self.assertEqual(ev[4]["detail"], {"from": "blocked", "answer": "a!"})
        self.assertEqual(ev[5]["detail"], {"from": "pending", "session": SID2})
        self.assertEqual(ev[7]["detail"], {"from": "pending", "reason": "obsolete"})
        self.assertEqual([e["ts"] for e in ev], sorted(e["ts"] for e in ev))
        for sql in ("UPDATE task_events SET event='x'", "DELETE FROM task_events"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        self.conn.rollback()
        self.assertEqual(len(store.task_events(self.conn, tid)), 8)


class Ordering(Base):
    def test_priority_then_created(self):
        a = store.add_task(self.conn, "a", priority=1)
        b = store.add_task(self.conn, "b", priority=5)
        c = store.add_task(self.conn, "c", priority=1)
        d = store.add_task(self.conn, "d", priority=5)
        e = store.add_task(self.conn, "e", priority=-1)
        ids = [t["id"] for t in store.list_tasks(self.conn)]
        self.assertEqual(ids, [b["id"], d["id"], a["id"], c["id"], e["id"]])
        self.assertEqual(store.next_ready_task(self.conn)["id"], b["id"])
        store.start_task(self.conn, b["id"], SID)
        store.block_task(self.conn, d["id"], "q")
        self.assertEqual(store.next_ready_task(self.conn)["id"], a["id"])
        store.set_priority(self.conn, e["id"], 10)        # manual bump reorders
        self.assertEqual(store.next_ready_task(self.conn)["id"], e["id"])

    def test_same_timestamp_falls_back_to_id(self):
        store._utcnow = lambda: datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
        ids = [store.add_task(self.conn, n)["id"] for n in "abc"]
        self.assertEqual([t["id"] for t in store.list_tasks(self.conn)], ids)

    def test_filters(self):
        store.add_task(self.conn, "a", project="/p1")
        b = store.add_task(self.conn, "b", project="/p2", kind="backlog_project", priority=9)
        c = store.add_task(self.conn, "c", project="/p1")
        store.finish_task(self.conn, c["id"])
        self.assertEqual([t["title"] for t in store.list_tasks(self.conn, project="/p1")], ["a", "c"])
        self.assertEqual([t["title"] for t in store.list_tasks(self.conn, kind="backlog_project")], ["b"])
        self.assertEqual([t["title"] for t in store.list_tasks(self.conn, status="done")], ["c"])
        self.assertEqual([t["title"] for t in store.list_tasks(self.conn, status=["pending", "done"],
                                                                project="/p1")], ["a", "c"])
        self.assertEqual(store.next_ready_task(self.conn)["id"], b["id"])
        self.assertEqual(store.next_ready_task(self.conn, kind="task")["title"], "a")


class Validation(Base):
    def test_enums_and_types(self):
        for kw in ({"kind": "project"}, {"priority": "5"}, {"priority": 1.5}, {"priority": True},
                   {"title": ""}, {"title": None}, {"description": 5}):
            args = dict({"title": "x"}, **kw)
            with self.assertRaises(ValueError, msg=kw):
                store.add_task(self.conn, **args)
        self.assertEqual(store.list_tasks(self.conn), [])
        with self.assertRaises(ValueError):
            store.list_tasks(self.conn, status="open")
        with self.assertRaises(ValueError):
            store.list_tasks(self.conn, kind="x")
        t = store.add_task(self.conn, "x")
        for fn in (lambda: store.block_task(self.conn, t["id"], " "),
                   lambda: store.set_priority(self.conn, t["id"], "high"),
                   lambda: store.update_task(self.conn, t["id"], kind="epic")):
            with self.assertRaises(ValueError):
                fn()
        self.stall(SID)
        for fn in (lambda: store.decide_session(self.conn, SID, "maybe"),
                   lambda: store.add_rule(self.conn, "host", "/x", "continue"),
                   lambda: store.add_rule(self.conn, "project", "/x", "always"),
                   lambda: store.add_rule(self.conn, "project", "", "continue")):
            with self.assertRaises(ValueError):
                fn()
        self.assertEqual(store.list_rules(self.conn), [])
        self.assertEqual(self.events(t["id"]), ["created"])


class Decisions(Base):
    def test_precedence(self):
        self.stall(SID, cwd="/home/x/proj/sub")
        eff = lambda: store.effective_decision(self.conn, SID)  # noqa: E731
        self.assertEqual(eff(), (None, None))                    # undecided
        r1 = store.add_rule(self.conn, "project", "/home/x/proj/", "ignore")   # normalized, parent dir
        self.assertEqual(r1["match"], "/home/x/proj")
        self.assertEqual(eff(), ("ignore", f"project_rule:{r1['id']}"))
        r2 = store.add_rule(self.conn, "session", SID, "continue")
        self.assertEqual(eff(), ("continue", f"session_rule:{r2['id']}"))
        store.decide_session(self.conn, SID, "ignore", note="not tonight")
        self.assertEqual(eff(), ("ignore", "session"))
        store.clear_decision(self.conn, SID)
        self.assertEqual(eff(), ("continue", f"session_rule:{r2['id']}"))
        store.remove_rule(self.conn, r2["id"])
        self.assertEqual(eff(), ("ignore", f"project_rule:{r1['id']}"))
        store.remove_rule(self.conn, r1["id"])
        self.assertEqual(eff(), (None, None))
        with self.assertRaises(store.NotFound):
            store.remove_rule(self.conn, r1["id"])

    def test_project_rule_matching(self):
        self.stall(SID, cwd="/home/x/proj", project_dir="-home-x-proj")
        self.stall(SID2, cwd="/home/x/project2", project_dir="-home-x-project2")
        store.add_rule(self.conn, "project", "/home/x/proj", "continue")
        self.assertEqual(store.effective_decision(self.conn, SID)[0], "continue")
        self.assertEqual(store.effective_decision(self.conn, SID2), (None, None))  # not a prefix match
        r = store.add_rule(self.conn, "project", "-home-x-project2", "ignore")      # by project_dir
        self.assertEqual(store.effective_decision(self.conn, SID2), ("ignore", f"project_rule:{r['id']}"))
        # most specific path wins; path rules beat project_dir rules
        store.add_rule(self.conn, "project", "/home/x", "ignore")
        self.assertEqual(store.effective_decision(self.conn, SID)[0], "continue")
        r3 = store.add_rule(self.conn, "project", "/home/x/project2", "continue")
        self.assertEqual(store.effective_decision(self.conn, SID2), ("continue", f"project_rule:{r3['id']}"))
        # same scope+match replaces the rule instead of adding a second one
        n = len(store.list_rules(self.conn))
        store.add_rule(self.conn, "project", "/home/x/proj", "ignore")
        self.assertEqual(len(store.list_rules(self.conn)), n)
        self.assertEqual(store.effective_decision(self.conn, SID)[0], "ignore")

    def test_decision_is_for_the_current_stall_only(self):
        self.stall(SID, uuid="n1")
        store.decide_session(self.conn, SID, "continue")
        self.assertEqual(store.effective_decision(self.conn, SID), ("continue", "session"))
        self.unstall(SID)                      # resumed
        self.assertEqual(store.effective_decision(self.conn, SID), (None, None))
        self.stall(SID, uuid="n2")             # stalls again: ask again
        self.assertEqual(store.effective_decision(self.conn, SID), (None, None))
        self.assertEqual([s["session_id"] for s in store.pending_user_input(self.conn)["undecided_sessions"]],
                         [SID])

    def test_decide_needs_a_current_stall(self):
        with self.assertRaises(store.NotFound):
            store.decide_session(self.conn, SID, "continue")
        store.upsert_session(self.conn, SID, cwd="/x")
        with self.assertRaises(store.InvalidTransition):
            store.decide_session(self.conn, SID, "continue")
        self.stall(SID)
        d = store.decide_session(self.conn, SID, "ignore")
        self.assertEqual((d["decision"], d["stall_ref"]), ("ignore", "n1"))
        d = store.decide_session(self.conn, SID, "continue")   # overwrite
        self.assertEqual(d["decision"], "continue")

    def test_unknown_session_only_session_rules(self):
        self.assertEqual(store.effective_decision(self.conn, SID3), (None, None))
        r = store.add_rule(self.conn, "session", SID3, "ignore")
        self.assertEqual(store.effective_decision(self.conn, SID3), ("ignore", f"session_rule:{r['id']}"))

    def test_stalled_decisions(self):
        self.stall(SID)
        self.stall(SID2)
        store.upsert_session(self.conn, SID3, cwd="/home/x/proj")      # not stalled
        store.decide_session(self.conn, SID, "continue")
        got = {s["session_id"]: (s["decision"], s["decision_source"]) for s in store.stalled_decisions(self.conn)}
        self.assertEqual(got, {SID: ("continue", "session"), SID2: (None, None)})


class Inbox(Base):
    def test_contents(self):
        self.assertEqual(store.pending_user_input(self.conn), {"blocked_tasks": [], "undecided_sessions": []})
        a = store.add_task(self.conn, "a", priority=1)
        b = store.add_task(self.conn, "b", priority=5)
        c = store.add_task(self.conn, "c")
        store.block_task(self.conn, a["id"], "qa")
        store.block_task(self.conn, b["id"], "qb")
        store.start_task(self.conn, c["id"], SID)
        self.stall(SID, cwd="/home/x/proj")
        self.stall(SID2, cwd="/home/y/other", project_dir="-home-y-other")
        self.stall(SID3, cwd="/home/y/third", project_dir="-home-y-third")
        store.upsert_session(self.conn, "00000000-0000-4000-8000-00000000000f", cwd="/z")  # not stalled
        self.conn.commit()
        store.decide_session(self.conn, SID2, "ignore")
        store.add_rule(self.conn, "project", "/home/y/third", "continue")
        box = store.pending_user_input(self.conn)
        self.assertEqual([t["id"] for t in box["blocked_tasks"]], [b["id"], a["id"]])
        self.assertEqual([s["session_id"] for s in box["undecided_sessions"]], [SID])
        store.answer_task(self.conn, b["id"], "yes")
        store.decide_session(self.conn, SID, "continue")
        box = store.pending_user_input(self.conn)
        self.assertEqual([t["id"] for t in box["blocked_tasks"]], [a["id"]])
        self.assertEqual(box["undecided_sessions"], [])


class Migration(unittest.TestCase):
    def test_goal2_db_upgrades_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            old = sqlite3.connect(path)
            old.executescript(V1_SCHEMA)
            old.execute("INSERT INTO sessions(session_id, cwd, project_dir, stalled, stalled_since, stall_uuid) "
                        "VALUES (?, '/home/x/proj', '-home-x-proj', 1, '2026-09-29T09:00:00Z', 'n1')", (SID,))
            old.execute("INSERT INTO limit_hits(session_id, entry_uuid, ts, kind) "
                        "VALUES (?, 'n1', '2026-09-29T09:00:00Z', 'session')", (SID,))
            old.commit()
            old.close()

            conn = store.connect(path)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"tasks", "task_events", "session_decisions", "standing_rules"} <= tables)
                self.assertEqual(store.get_meta(conn, "schema_version"), str(store.SCHEMA_VERSION))
                self.assertEqual(store.SCHEMA_VERSION, 2)
                self.assertEqual(store.counts(conn)["hits"], 1)                 # old data intact
                self.assertEqual(store.get_session(conn, SID)["cwd"], "/home/x/proj")
                self.assertEqual([s["session_id"] for s in store.pending_user_input(conn)["undecided_sessions"]],
                                 [SID])
                store.decide_session(conn, SID, "continue")
                self.assertEqual(store.effective_decision(conn, SID), ("continue", "session"))
                t = store.add_task(conn, "after migration")
                self.assertEqual(store.next_ready_task(conn)["id"], t["id"])
            finally:
                conn.close()
            # reopening is a no-op; a newer mark is never lowered
            conn = store.connect(path)
            store.set_meta(conn, "schema_version", 99)
            conn.commit()
            store.init(conn)
            self.assertEqual(store.get_meta(conn, "schema_version"), "99")
            self.assertEqual(len(store.list_tasks(conn)), 1)
            conn.close()

    def test_columns_migration_applies_to_new_tables(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            store.connect(path).close()
            orig = store.COLUMNS["tasks"]
            store.COLUMNS["tasks"] = orig + [("due_hint", "TEXT")]
            try:
                conn = store.connect(path)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
                self.assertIn("due_hint", cols)
                conn.close()
            finally:
                store.COLUMNS["tasks"] = orig


class Transactions(Base):
    def test_inside_caller_transaction_uses_savepoint(self):
        store.upsert_session(self.conn, SID, cwd="/x")       # caller's open transaction
        self.assertTrue(self.conn.in_transaction)
        t = store.add_task(self.conn, "x")
        with self.assertRaises(store.InvalidTransition):
            store.answer_task(self.conn, t["id"], "a")       # rolls back to its savepoint only
        self.assertTrue(self.conn.in_transaction)            # caller still owns it
        self.conn.rollback()
        self.assertIsNone(store.get_task(self.conn, t["id"]))
        self.assertIsNone(store.get_session(self.conn, SID))

    def test_visible_to_other_connections(self):
        t = store.add_task(self.conn, "x")
        other = store.connect(self.db)
        try:
            self.assertEqual(store.get_task(other, t["id"])["title"], "x")
            store.start_task(other, t["id"], SID)
            with self.assertRaises(store.InvalidTransition):
                store.start_task(self.conn, t["id"], SID2)       # already claimed elsewhere
        finally:
            other.close()


class CLI(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.main(["--db", self.db] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_flow(self):
        rc, out, _ = self.run_cli("add", "Write the MCP server", "-p", "5", "--project", "/p/AFClaude",
                                  "-d", "goal 4", "--json")
        self.assertEqual(rc, 0)
        tid = json.loads(out)["id"]
        self.run_cli("add", "second")
        rc, out, _ = self.run_cli("list")
        lines = out.splitlines()
        self.assertIn("Write the MCP server", lines[1])            # priority 5 first
        self.assertIn("AFClaude", lines[1])
        self.assertIn("2026-09-29 12:00", lines[1])                # 10:00:0x UTC shown in Berlin (CEST)
        self.run_cli("block", str(tid), "Which port?")
        rc, out, _ = self.run_cli("inbox", "--no-scan")
        self.assertIn("Which port?", out)
        rc, out, _ = self.run_cli("inbox", "--no-scan", "--json")
        self.assertEqual([t["id"] for t in json.loads(out)["blocked_tasks"]], [tid])
        self.run_cli("answer", str(tid), "8765")
        self.run_cli("start", str(tid), "--session", SID)
        self.run_cli("prio", str(tid), "7")
        rc, out, _ = self.run_cli("done", str(tid), "built")
        self.assertIn("done", out)
        rc, out, _ = self.run_cli("show", str(tid), "--json")
        d = json.loads(out)
        self.assertEqual([e["event"] for e in d["events"]],
                         ["created", "blocked", "answered", "started", "priority", "done"])
        rc, out, _ = self.run_cli("show", str(tid))
        self.assertIn("answer", out)
        self.assertIn("CEST", tasks.berlin(d["created_at"], "%Z"))
        rc, out, _ = self.run_cli("list")
        self.assertNotIn("Write the MCP server", out)              # done hidden by default
        rc, out, _ = self.run_cli("list", "--all")
        self.assertIn("Write the MCP server", out)
        rc, out, _ = self.run_cli("inbox", "--no-scan")
        self.assertIn("nothing needs your input", out)

    def test_errors(self):
        rc, _, err = self.run_cli("answer", "1", "x")
        self.assertEqual((rc, err.strip()), (1, "error: no task #1"))
        self.run_cli("add", "x")
        rc, _, err = self.run_cli("answer", "1", "x")
        self.assertEqual(rc, 1)
        self.assertIn("answer needs blocked", err)
        rc, _, err = self.run_cli("decide", "0000", "continue", "--no-scan")
        self.assertEqual(rc, 1)
        self.assertIn("no session matches", err)

    def test_decide_and_rules(self):
        self.stall(SID, cwd="/home/x/proj")
        self.stall(SID2, cwd="/home/x/other", project_dir="-home-x-other")
        rc, out, _ = self.run_cli("inbox", "--no-scan")
        self.assertIn("2 undecided", out)
        rc, out, err = self.run_cli("decide", SID[:36], "ignore", "--no-scan")
        self.assertEqual((rc, out.strip()), (0, f"{SID[:8]}: ignore (session)"))
        rc, out, err = self.run_cli("decide", "00000000-0000-4000-8000-0000000000", "ignore", "--no-scan")
        self.assertEqual(rc, 1)
        self.assertIn("2 sessions match", err)                     # ambiguous prefix
        rc, out, _ = self.run_cli("rule", "add", "project", "/home/x/other", "continue", "--note", "mine",
                                  "--json")
        rid = json.loads(out)["id"]
        rc, out, _ = self.run_cli("inbox", "--no-scan", "--json")
        self.assertEqual(json.loads(out)["undecided_sessions"], [])
        rc, out, _ = self.run_cli("rule", "add", "project", "--", "-home-x-proj", "ignore")
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("rule", "list")
        self.assertIn("/home/x/other  (mine)", out)
        self.assertIn("-home-x-proj", out)
        self.run_cli("rule", "rm", str(rid))
        rc, out, _ = self.run_cli("decide", SID2, "clear", "--no-scan", "--json")
        self.assertEqual(json.loads(out), {"session_id": SID2, "decision": None, "source": None})


if __name__ == "__main__":
    unittest.main()
