#!/usr/bin/env python3
"""Offline tests for the task store (store.py v3) and tasks.py (temp DBs only)."""
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

# Goal-3 additions exactly as shipped in 401ab9c (schema_version 2): integer priority, free-text project.
V2_TABLES = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT, project TEXT,
    priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
    kind TEXT NOT NULL DEFAULT 'task', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    created_by_session TEXT, assigned_session TEXT, blocked_question TEXT, blocked_at TEXT, answer TEXT,
    answered_at TEXT, result_summary TEXT, done_at TEXT);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id),
    ts TEXT NOT NULL, event TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, id);
CREATE TRIGGER IF NOT EXISTS task_events_no_update BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TABLE IF NOT EXISTS session_decisions (
    session_id TEXT PRIMARY KEY, decision TEXT NOT NULL, decided_at TEXT NOT NULL, note TEXT, stall_ref TEXT);
CREATE TABLE IF NOT EXISTS standing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, match TEXT NOT NULL, decision TEXT NOT NULL,
    created_at TEXT NOT NULL, note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS standing_rules_scope_match ON standing_rules(scope, match);
UPDATE meta SET value='2' WHERE key='schema_version';
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
                         ("Write MCP server", "pending", "task", "high"))
        self.assertEqual((t["project"], t["project_rank"], t["stage_seq"]), ("p", 1, 1))
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
        store.set_stage_priority(self.conn, t["id"], "low")
        store.set_priority(self.conn, t["id"], " Medium")                 # alias; normalized
        store.set_priority(self.conn, t["id"], "medium")                  # no change -> no event
        ev = store.task_events(self.conn, t["id"])
        self.assertEqual([e["event"] for e in ev], ["created", "updated", "priority", "priority"])
        self.assertEqual(ev[1]["detail"], {"title": ["x", "y"]})
        self.assertEqual(ev[3]["detail"], {"from": "low", "to": "medium"})
        with self.assertRaises(ValueError):
            store.update_task(self.conn, t["id"], status="done")    # not whitelisted
        with self.assertRaises(ValueError):
            store.update_task(self.conn, t["id"], title="  ")

    def test_event_log_complete_and_append_only(self):
        t = store.add_task(self.conn, "x", priority="medium")
        tid = t["id"]
        store.set_priority(self.conn, tid, "low")
        store.start_task(self.conn, tid, SID)
        store.block_task(self.conn, tid, "q?")
        store.answer_task(self.conn, tid, "a!")
        store.start_task(self.conn, tid, SID2)
        store.reopen_task(self.conn, tid, "run died")
        store.cancel_task(self.conn, tid, "obsolete")
        ev = store.task_events(self.conn, tid)
        self.assertEqual([e["event"] for e in ev], ["created", "priority", "started", "blocked", "answered",
                                                    "started", "reopened", "cancelled"])
        self.assertEqual(ev[0]["detail"], {"title": "x", "priority": "medium", "kind": "task"})
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


def titles(rows):
    return [t["title"] for t in rows]


class Ordering(Base):
    def test_without_projects_level_then_created(self):
        a = store.add_task(self.conn, "a", priority="medium")
        b = store.add_task(self.conn, "b")
        store.add_task(self.conn, "c", priority="low")
        d = store.add_task(self.conn, "d", priority="medium")
        e = store.add_task(self.conn, "e")
        self.assertEqual(titles(store.list_tasks(self.conn)), ["b", "e", "a", "d", "c"])
        self.assertEqual(store.next_ready_task(self.conn)["id"], b["id"])
        store.start_task(self.conn, b["id"], SID)
        store.block_task(self.conn, a["id"], "q")
        self.assertEqual(titles(store.execution_order(self.conn)), ["e", "d", "c"])   # only pending counts
        store.set_priority(self.conn, d["id"], "high")        # manual bump reorders (d is older than e)
        self.assertEqual(titles(store.execution_order(self.conn)), ["d", "e", "c"])
        store.set_priority(self.conn, e["id"], "low")
        self.assertEqual(titles(store.execution_order(self.conn)), ["d", "c", "e"])

    def test_same_timestamp_falls_back_to_id(self):
        store._utcnow = lambda: datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
        ids = [store.add_task(self.conn, n)["id"] for n in "abc"]
        self.assertEqual([t["id"] for t in store.list_tasks(self.conn)], ids)

    def test_three_projects_mixed_priorities(self):
        # ranks: A=1, B=2, C=3 (creation order); stages in add order
        store.add_task(self.conn, "A1", project="A")
        store.add_task(self.conn, "A2", project="A", priority="low")
        store.add_task(self.conn, "A3", project="A", priority="medium")
        store.add_task(self.conn, "B1", project="B", priority="medium")
        store.add_task(self.conn, "B2", project="B")
        store.add_task(self.conn, "C1", project="C", priority="low")
        store.add_task(self.conn, "C2", project="C")
        store.add_task(self.conn, "N1")                                   # no project: after all projects
        store.add_task(self.conn, "N2", priority="medium")
        self.assertEqual([p["name"] for p in store.list_projects(self.conn)], ["A", "B", "C"])
        want = ["A1", "B2", "C2", "N1",        # high: by project rank, then stage
                "A3", "B1", "N2",              # medium
                "A2", "C1"]                    # low
        self.assertEqual(titles(store.execution_order(self.conn)), want)
        self.assertEqual(titles(store.list_tasks(self.conn)), want)
        # reorder the projects: C to the top
        store.move_project(self.conn, "C", 1)
        self.assertEqual([(p["name"], p["rank"]) for p in store.list_projects(self.conn)],
                         [("C", 1), ("A", 2), ("B", 3)])
        self.assertEqual(titles(store.execution_order(self.conn)),
                         ["C2", "A1", "B2", "N1", "A3", "B1", "N2", "C1", "A2"])
        # stage order within a project
        a3 = store.list_tasks(self.conn, project="A")[1]
        self.assertEqual(a3["title"], "A3")
        store.update_task(self.conn, a3["id"], priority="high")
        self.assertEqual(titles(store.execution_order(self.conn, project="A")), ["A1", "A3", "A2"])
        t = store.move_stage(self.conn, a3["id"], 1)
        self.assertEqual(t["stage_seq"], 1)
        self.assertEqual(titles(store.execution_order(self.conn)[:4]), ["C2", "A3", "A1", "B2"])
        self.assertEqual([(x["title"], x["stage_seq"]) for x in store.list_tasks(self.conn, project="A")],
                         [("A3", 1), ("A1", 2), ("A2", 3)])
        self.assertEqual(store.task_events(self.conn, a3["id"])[-1]["detail"], {"from": 3, "to": 1})
        # skipped: blocked / in_progress / done stages don't count, later stages run
        a1 = [x for x in store.list_tasks(self.conn) if x["title"] == "A1"][0]
        store.block_task(self.conn, a3["id"], "q")
        store.start_task(self.conn, a1["id"], SID)
        self.assertEqual(titles(store.execution_order(self.conn, project="A")), ["A2"])

    def test_bulk_project_priority(self):
        for p in ("A", "B", "C"):
            for i in (1, 2):
                store.add_task(self.conn, f"{p}{i}", project=p)
        c2 = store.list_tasks(self.conn, project="C")[1]
        store.set_priority(self.conn, c2["id"], "low")
        done = store.finish_task(self.conn, store.list_tasks(self.conn, project="A")[1]["id"])
        self.assertEqual(titles(store.execution_order(self.conn)), ["A1", "B1", "B2", "C1", "C2"])
        r = store.set_project_priority(self.conn, "A", "medium")        # whole top project -> medium
        self.assertEqual((r["project"], r["priority"]), ("A", "medium"))
        self.assertEqual(len(r["changed"]), 1)                           # the done stage stays as it was
        self.assertEqual(store.get_task(self.conn, done["id"])["priority"], "high")
        self.assertEqual(titles(store.execution_order(self.conn)), ["B1", "B2", "C1", "A1", "C2"])
        ev = store.task_events(self.conn, r["changed"][0])[-1]
        self.assertEqual((ev["event"], ev["detail"]), ("priority", {"from": "high", "to": "medium", "via": "project"}))
        # open includes blocked and in_progress stages
        b1, b2 = store.list_tasks(self.conn, project="B")
        store.block_task(self.conn, b1["id"], "q")
        store.start_task(self.conn, b2["id"], SID)
        r = store.set_project_priority(self.conn, "B", "low")
        self.assertEqual(sorted(r["changed"]), sorted([b1["id"], b2["id"]]))
        self.assertEqual(store.set_project_priority(self.conn, "B", "low")["changed"], [])   # idempotent
        p = {x["name"]: x for x in store.list_projects(self.conn)}
        self.assertEqual((p["A"]["open"], p["A"]["ready"]), (1, {"high": 0, "medium": 1, "low": 0}))
        self.assertEqual((p["B"]["open"], p["B"]["ready"]), (2, {"high": 0, "medium": 0, "low": 0}))
        with self.assertRaises(store.NotFound):
            store.set_project_priority(self.conn, "Z", "low")
        with self.assertRaises(ValueError):
            store.set_project_priority(self.conn, "A", "urgent")

    def test_filters(self):
        store.add_task(self.conn, "a", project="/p1")
        b = store.add_task(self.conn, "b", project="/p2", kind="backlog_project")
        c = store.add_task(self.conn, "c", project="/p1")
        store.finish_task(self.conn, c["id"])
        self.assertEqual(titles(store.list_tasks(self.conn, project="/p1")), ["a", "c"])
        self.assertEqual(titles(store.list_tasks(self.conn, project="p1")), ["a", "c"])       # by name
        self.assertEqual(titles(store.list_tasks(self.conn, kind="backlog_project")), ["b"])
        self.assertEqual(titles(store.list_tasks(self.conn, status="done")), ["c"])
        self.assertEqual(titles(store.list_tasks(self.conn, status=["pending", "done"], project="/p1")), ["a", "c"])
        self.assertEqual(store.next_ready_task(self.conn)["title"], "a")
        self.assertEqual(store.next_ready_task(self.conn, kind="backlog_project")["id"], b["id"])
        with self.assertRaises(store.NotFound):
            store.list_tasks(self.conn, project="nope")


class Projects(Base):
    def test_paths_names_and_ranks(self):
        p = store.project_for_path(self.conn, "/home/x/AFClaude/")
        self.assertEqual((p["name"], p["path"], p["rank"]), ("AFClaude", "/home/x/AFClaude", 1))
        # a subdirectory maps to the nearest parent project, not a new one
        self.assertEqual(store.project_for_path(self.conn, "/home/x/AFClaude/quickview")["id"], p["id"])
        t = store.add_task(self.conn, "x", project="/home/x/AFClaude/sub/dir")
        self.assertEqual(t["project"], "AFClaude")
        self.assertIsNone(store.project_for_path(self.conn, "/home/x/AFClaudeX", create=False))  # not a prefix
        # same basename elsewhere -> parent/name
        q = store.project_for_path(self.conn, "/srv/AFClaude")
        self.assertEqual((q["name"], q["rank"]), ("srv/AFClaude", 2))
        n = store.add_project(self.conn, "Notes", description="plain", rank=1)
        self.assertEqual([(x["name"], x["rank"]) for x in store.list_projects(self.conn)],
                         [("Notes", 1), ("AFClaude", 2), ("srv/AFClaude", 3)])
        store.move_project(self.conn, "Notes", 99)                        # clamped to the bottom
        store.move_project(self.conn, q["id"], 0)                         # clamped to the top
        self.assertEqual([(x["name"], x["rank"]) for x in store.list_projects(self.conn)],
                         [("srv/AFClaude", 1), ("AFClaude", 2), ("Notes", 3)])
        r = store.update_project(self.conn, "/srv/AFClaude", name="AFClaude-srv", description="copy")
        self.assertEqual((r["name"], r["path"], r["rank"]), ("AFClaude-srv", "/srv/AFClaude", 1))
        self.assertEqual(store.update_project(self.conn, "Notes", path="/home/x/notes")["path"], "/home/x/notes")
        self.assertEqual(store.project_for_path(self.conn, "/home/x/notes/a")["id"], n["id"])
        for fn in (lambda: store.add_project(self.conn, "Notes"),
                   lambda: store.add_project(self.conn, "/abs"),
                   lambda: store.add_project(self.conn, "Other", path="/home/x/AFClaude"),
                   lambda: store.update_project(self.conn, "Notes", name="AFClaude"),
                   lambda: store.update_project(self.conn, "Notes", rank=1),
                   lambda: store.move_project(self.conn, "Notes", "1")):
            with self.assertRaises(ValueError):
                fn()
        with self.assertRaises(store.NotFound):
            store.move_project(self.conn, "nope", 1)

    def test_task_changes_project(self):
        a1 = store.add_task(self.conn, "a1", project="A")
        a2 = store.add_task(self.conn, "a2", project="A")
        a3 = store.add_task(self.conn, "a3", project="A")
        store.add_task(self.conn, "b1", project="B")
        t = store.update_task(self.conn, a1["id"], project="B")
        self.assertEqual((t["project"], t["stage_seq"]), ("B", 2))       # appended to B
        self.assertEqual([(x["title"], x["stage_seq"]) for x in store.list_tasks(self.conn, project="A")],
                         [("a2", 1), ("a3", 2)])                          # A compacted
        self.assertEqual(store.task_events(self.conn, a1["id"])[-1]["detail"], {"project": ["A", "B"]})
        t = store.update_task(self.conn, a2["id"], project=None)
        self.assertEqual((t["project"], t["project_id"], t["stage_seq"]), (None, None, None))
        with self.assertRaises(store.InvalidTransition):
            store.move_stage(self.conn, a2["id"], 1)                      # no project, no stage order
        self.assertEqual(store.move_stage(self.conn, a3["id"], 1)["stage_seq"], 1)   # already first: no-op
        self.assertEqual(store.task_events(self.conn, a3["id"])[-1]["event"], "created")
        with self.assertRaises(ValueError):
            store.move_stage(self.conn, a3["id"], 1.5)


class Validation(Base):
    def test_enums_and_types(self):
        for kw in ({"kind": "project"}, {"priority": 5}, {"priority": "urgent"}, {"priority": None},
                   {"priority": True}, {"title": ""}, {"title": None}, {"description": 5}, {"project": 5}):
            args = dict({"title": "x"}, **kw)
            with self.assertRaises(ValueError, msg=kw):
                store.add_task(self.conn, **args)
        self.assertEqual(store.list_tasks(self.conn), [])
        self.assertEqual(store.list_projects(self.conn), [])              # nothing half-created
        with self.assertRaises(ValueError):
            store.list_tasks(self.conn, status="open")
        with self.assertRaises(ValueError):
            store.list_tasks(self.conn, kind="x")
        t = store.add_task(self.conn, "x")
        for fn in (lambda: store.block_task(self.conn, t["id"], " "),
                   lambda: store.set_priority(self.conn, t["id"], 3),
                   lambda: store.set_priority(self.conn, t["id"], "highest"),
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
        a = store.add_task(self.conn, "a", priority="medium")
        b = store.add_task(self.conn, "b")
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
                self.assertEqual(store.SCHEMA_VERSION, 3)
                self.assertIn("projects", tables)
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

    def test_v2_db_with_integer_priorities(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v2.db")
            old = sqlite3.connect(path)
            old.executescript(V1_SCHEMA + V2_TABLES)
            rows = [  # id, title, project, priority, status, created_at
                (1, "p-low", "/home/x/proj", -1, "pending", "2026-09-29T08:00:00.000Z"),
                (2, "other-a", "/home/x/other", 3, "blocked", "2026-09-29T08:01:00.000Z"),
                (3, "p-high", "/home/x/proj", 5, "pending", "2026-09-29T08:02:00.000Z"),
                (4, "loose", None, 9, "pending", "2026-09-29T08:03:00.000Z"),
                (5, "named", "AFClaude", 0, "done", "2026-09-29T08:04:00.000Z"),
                (6, "p-mid", "/home/x/proj", 1, "in_progress", "2026-09-29T08:05:00.000Z"),
                (7, "same-as-2", "/home/x/other", 3, "pending", "2026-09-29T08:00:30.000Z"),
            ]
            for tid, title, proj, prio, status, ts in rows:
                old.execute("INSERT INTO tasks(id, title, project, priority, status, created_at, updated_at, "
                            "blocked_question) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (tid, title, proj, prio, status, ts, ts, "q?" if status == "blocked" else None))
                old.execute("INSERT INTO task_events(task_id, ts, event, detail) VALUES (?, ?, 'created', ?)",
                            (tid, ts, json.dumps({"title": title, "priority": prio})))
            old.execute("INSERT INTO tasks(id, title, created_at, updated_at) VALUES (9, 'gone', 'x', 'x')")
            old.execute("DELETE FROM tasks WHERE id=9")        # sqlite_sequence is now 9: ids must not be reused
            old.execute("INSERT INTO standing_rules(scope, match, decision, created_at) "
                        "VALUES ('project', '/home/x/proj', 'continue', '2026-09-29T08:00:00.000Z')")
            old.commit()
            old.close()

            conn = store.connect(path)
            try:
                self.assertEqual(store.get_meta(conn, "schema_version"), "3")
                baks = [f for f in os.listdir(d) if f.startswith("v2.db.v2-") and f.endswith(".bak")]
                self.assertEqual(len(baks), 1)                                   # copy taken first
                b = sqlite3.connect(os.path.join(d, baks[0]))
                self.assertEqual(b.execute("SELECT COUNT(*), SUM(priority) FROM tasks").fetchone(), (7, 20))
                b.close()
                # projects ranked by first use: proj (08:00), other (08:00:30), AFClaude (08:04)
                self.assertEqual([(p["name"], p["path"], p["rank"]) for p in store.list_projects(conn)],
                                 [("proj", "/home/x/proj", 1), ("other", "/home/x/other", 2),
                                  ("AFClaude", None, 3)])
                ts = {t["id"]: t for t in store.list_tasks(conn)}
                self.assertEqual(set(ts), {1, 2, 3, 4, 5, 6, 7})                # ids kept
                self.assertEqual({t["priority"] for t in ts.values()}, {"high"})
                # stages in the old execution order (priority desc, then created)
                self.assertEqual([(ts[i]["title"], ts[i]["stage_seq"]) for i in (3, 6, 1)],
                                 [("p-high", 1), ("p-mid", 2), ("p-low", 3)])
                self.assertEqual([(ts[i]["project"], ts[i]["stage_seq"]) for i in (7, 2, 5, 4)],
                                 [("other", 1), ("other", 2), ("AFClaude", 1), (None, None)])
                self.assertEqual((ts[2]["status"], ts[2]["blocked_question"]), ("blocked", "q?"))
                self.assertEqual(titles(store.execution_order(conn)), ["p-high", "p-low", "same-as-2", "loose"])
                ev = store.task_events(conn, 3)
                self.assertEqual([e["event"] for e in ev], ["created", "migrated"])
                self.assertEqual(ev[1]["detail"], {"priority": [5, "high"], "project": "proj"})
                self.assertEqual(store.task_events(conn, 4)[1]["detail"], {"priority": [9, "high"]})
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                with self.assertRaises(sqlite3.DatabaseError):                  # triggers survived
                    conn.execute("DELETE FROM task_events")
                conn.rollback()
                self.assertEqual(store.add_task(conn, "new", project="/home/x/proj/sub")["id"], 10)
                self.assertEqual(store.effective_decision(conn, SID), (None, None))
                self.assertEqual(len(store.list_rules(conn)), 1)                 # other tables untouched
                idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn("tasks_order", idx)
                self.assertNotIn("tasks_ready", idx)
            finally:
                conn.close()
            conn = store.connect(path)                                          # second connect: no-op
            try:
                self.assertFalse(store._migrate_v3(conn))
                self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".bak")]), 1)
                self.assertEqual(len(store.list_tasks(conn)), 8)
            finally:
                conn.close()

    def test_empty_v2_db_migrates_without_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v2.db")
            old = sqlite3.connect(path)
            old.executescript(V1_SCHEMA + V2_TABLES)
            old.close()
            conn = store.connect(path)
            try:
                self.assertIn("project_id", {r[1] for r in conn.execute("PRAGMA table_info(tasks)")})
                self.assertEqual([f for f in os.listdir(d) if f.endswith(".bak")], [])   # nothing to back up
                self.assertEqual(store.add_task(conn, "x")["priority"], "high")
            finally:
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
        rc, out, _ = self.run_cli("add", "Write the MCP server", "-p", "high", "--project", "/p/AFClaude",
                                  "-d", "goal 4", "--json")
        self.assertEqual(rc, 0)
        tid = json.loads(out)["id"]
        self.run_cli("add", "second")
        rc, out, _ = self.run_cli("list")
        lines = out.splitlines()
        self.assertIn("Write the MCP server", lines[1])            # a project's stage before no-project
        self.assertIn("AFClaude", lines[1])
        self.assertIn("2026-09-29 12:00", lines[1])                # 10:00:0x UTC shown in Berlin (CEST)
        self.run_cli("block", str(tid), "Which port?")
        rc, out, _ = self.run_cli("inbox", "--no-scan")
        self.assertIn("Which port?", out)
        rc, out, _ = self.run_cli("inbox", "--no-scan", "--json")
        self.assertEqual([t["id"] for t in json.loads(out)["blocked_tasks"]], [tid])
        self.run_cli("answer", str(tid), "8765")
        self.run_cli("start", str(tid), "--session", SID)
        self.run_cli("prio", str(tid), "low")
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

    def test_projects_and_order(self):
        for args in (("add", "A1", "--project", "A"), ("add", "B1", "--project", "B"),
                     ("add", "A2", "--project", "A", "-p", "medium"), ("add", "loose")):
            self.assertEqual(self.run_cli(*args)[0], 0)
        rc, out, _ = self.run_cli("project", "add", "C", "--rank", "1", "-d", "urgent one", "--json")
        self.assertEqual((rc, json.loads(out)["rank"]), (0, 1))
        self.run_cli("add", "C1", "--project", "C")
        rc, out, _ = self.run_cli("order", "--json")
        self.assertEqual(titles(json.loads(out)), ["C1", "A1", "B1", "loose", "A2"])
        rc, out, _ = self.run_cli("project", "prio", "C", "low")
        self.assertEqual(out.strip(), "C: 1 open stage(s) set to low")
        rc, out, _ = self.run_cli("project", "move", "B", "1")
        rc, out, _ = self.run_cli("project", "list")
        self.assertEqual([ln.split()[3] for ln in out.splitlines()[1:]], ["B", "C", "A"])
        rc, out, _ = self.run_cli("order")
        lines = out.splitlines()
        self.assertEqual([ln.split()[1] for ln in lines[1:]], ["2", "1", "4", "3", "5"])   # task ids
        self.assertIn("1.1", lines[1])                                     # stage label rank.stage
        self.run_cli("move", "3", "1")
        rc, out, _ = self.run_cli("show", "3")
        self.assertIn("stage 1 of project #3 A", out)
        self.assertIn("moved", out)
        rc, out, _ = self.run_cli("project", "edit", "A", "--name", "Alpha", "--path", ".", "--json")
        self.assertEqual((json.loads(out)["name"], json.loads(out)["path"]), ("Alpha", os.getcwd()))
        rc, out, _ = self.run_cli("list", "--project", ".", "--json")
        self.assertEqual(titles(json.loads(out)), ["A1", "A2"])
        rc, _, err = self.run_cli("project", "move", "nope", "1")
        self.assertEqual((rc, err.strip()), (1, "error: no project 'nope'"))
        rc, _, err = self.run_cli("project", "add", "B")
        self.assertIn("already exists", err)

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
