#!/usr/bin/env python3
"""Offline tests for actions.py (the shared write path) and the schema v4 upgrade
(temp DBs only; never data/afclaude.db).

    python3 -m unittest test_actions
"""
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
import actions  # noqa: E402
import afclaude_config  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
AUTO = "00000000-0000-4000-8000-0000000000aa"
KEY = "req-0001-abcdef"

# Schema v3 exactly as main's store.py (3a19687) creates it, comments dropped.
V3_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, project_dir TEXT, path TEXT, cwd TEXT, title TEXT, title_rank INTEGER,
    first_seen TEXT, last_activity TEXT, own INTEGER NOT NULL DEFAULT 0,
    last_scanned_offset INTEGER NOT NULL DEFAULT 0, last_scanned_size INTEGER NOT NULL DEFAULT 0,
    last_msg_uuid TEXT, last_msg_type TEXT, last_msg_ts TEXT, stalled INTEGER NOT NULL DEFAULT 0,
    stalled_since TEXT, stall_kind TEXT, stall_reset_at TEXT, stall_text TEXT, stall_uuid TEXT, updated_at TEXT);
CREATE TABLE limit_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(session_id),
    entry_uuid TEXT UNIQUE, ts TEXT, kind TEXT, reset_at TEXT, text TEXT);
CREATE INDEX limit_hits_session ON limit_hits(session_id, ts);
CREATE INDEX sessions_stalled ON sessions(stalled);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, rank INTEGER NOT NULL, description TEXT,
    path TEXT, manager_session TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX projects_path ON projects(path) WHERE path IS NOT NULL;
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
    project_id INTEGER REFERENCES projects(id), stage_seq INTEGER, project TEXT,
    priority TEXT NOT NULL DEFAULT 'high', status TEXT NOT NULL DEFAULT 'pending',
    kind TEXT NOT NULL DEFAULT 'task', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    created_by_session TEXT, assigned_session TEXT, blocked_question TEXT, blocked_at TEXT, answer TEXT,
    answered_at TEXT, result_summary TEXT, done_at TEXT);
CREATE INDEX tasks_order ON tasks(status, priority, project_id, stage_seq);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id),
    ts TEXT NOT NULL, event TEXT NOT NULL, detail TEXT);
CREATE INDEX task_events_task ON task_events(task_id, id);
CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TABLE session_decisions (
    session_id TEXT PRIMARY KEY, decision TEXT NOT NULL, decided_at TEXT NOT NULL, note TEXT, stall_ref TEXT);
CREATE TABLE standing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, match TEXT NOT NULL, decision TEXT NOT NULL,
    created_at TEXT NOT NULL, note TEXT);
CREATE UNIQUE INDEX standing_rules_scope_match ON standing_rules(scope, match);
INSERT INTO meta(key, value) VALUES ('schema_version', '3');
"""

V4_TABLES = {"settings", "prompt_overrides", "audit_log", "idempotency_keys", "run_log", "driven_sessions",
             "action_requests"}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.db = os.path.join(t, "db", "t.db")
        self.clock = Clock()
        self._saved = (store._utcnow, actions.OWN_LIST, actions.PROMPTS_DIR, afclaude_config.CONFIG_FILE)
        store._utcnow = self.clock
        actions.OWN_LIST = os.path.join(t, "own_sessions.txt")
        actions.PROMPTS_DIR = os.path.join(t, "prompts")
        afclaude_config.CONFIG_FILE = os.path.join(t, "afclaude.json")    # no local file: code defaults
        os.makedirs(actions.PROMPTS_DIR)
        with open(os.path.join(actions.PROMPTS_DIR, "continue.md"), "w") as fh:
            fh.write("Continue ({reason}). Progress: {progress}. Literal {{braces}}.\n")
        with open(os.path.join(actions.PROMPTS_DIR, "manager.md"), "w") as fh:
            fh.write("Act as a project manager.\n")
        self.conn = store.connect(self.db, create=True)

    def tearDown(self):
        store._utcnow, actions.OWN_LIST, actions.PROMPTS_DIR, afclaude_config.CONFIG_FILE = self._saved
        self.conn.close()
        self.tmp.cleanup()

    def do(self, _action, /, actor="cli", via="cli", idem=None, autonomous=False, **params):
        return actions.perform(self.conn, _action, params, actor=actor, via=via, key=idem, autonomous=autonomous)

    def audit(self, **kw):
        return list(reversed(store.audit_log(self.conn, limit=1000, **kw)))

    def stall(self, sid, uuid="n1", cwd="/home/x/proj"):
        store.upsert_session(self.conn, sid, cwd=cwd, project_dir="-home-x-proj", stalled=1,
                             stalled_since="2026-10-01T09:00:00.000Z", stall_kind="session",
                             stall_reset_at="2026-10-01T12:00:00Z", stall_uuid=uuid, stall_text="hit")
        self.conn.commit()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- the write path itself

class Validation(Base):
    def test_unknown_action_param_actor_via_key(self):
        with self.assertRaisesRegex(ValueError, "unknown action"):
            self.do("task.fly", title="x")
        with self.assertRaisesRegex(ValueError, r"unknown parameter\(s\) \['colour'\]"):
            self.do("task.add", title="x", colour="red")
        with self.assertRaisesRegex(ValueError, r"missing parameter\(s\) \['title'\]"):
            self.do("task.add")
        with self.assertRaisesRegex(ValueError, "via must be"):
            self.do("task.add", via="web", title="x")
        for bad in ("root", "mcp:", "mcp:a b", "", None):
            with self.assertRaisesRegex(ValueError, "bad actor"):
                self.do("task.add", actor=bad, title="x")
        for bad in ("short", "x" * 129, "with space!!", 12345678):
            with self.assertRaisesRegex(ValueError, "idempotency key"):
                self.do("task.add", idem=bad, title="x")
        t = self.do("task.add", title="x")
        with self.assertRaisesRegex(ValueError, "version must be an integer"):
            self.do("task.edit", task_id=t["id"], version="0", title="y")
        self.assertEqual(self.count("tasks"), 1)

    def test_store_validation_passes_through_and_changes_nothing(self):
        t = self.do("task.add", title="x")
        n_audit = self.count("audit_log")
        with self.assertRaisesRegex(ValueError, "priority must be one of"):
            self.do("task.priority", task_id=t["id"], priority="urgent")
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            self.do("task.edit", task_id=t["id"], title="  ")
        with self.assertRaises(store.NotFound):
            self.do("task.cancel", task_id=999)
        with self.assertRaises(store.InvalidTransition):
            self.do("task.answer", task_id=t["id"], answer="a")             # not blocked
        with self.assertRaisesRegex(ValueError, "not editable"):
            self.do("task.update", task_id=t["id"], fields={"status": "done"})
        self.assertEqual(self.count("audit_log"), n_audit)                   # failures leave no audit row
        self.assertEqual(store.get_task(self.conn, t["id"])["title"], "x")

    def test_dashboard_size_limits_only_for_the_dashboard(self):
        long = "t" * 201
        with self.assertRaisesRegex(ValueError, "title is too long"):
            self.do("task.add", actor="owner", via="dashboard", title=long)
        t = self.do("task.add", title=long)                                  # CLI: unchanged behaviour
        self.do("task.block", task_id=t["id"], question="q?")
        with self.assertRaisesRegex(ValueError, "answer is too long"):
            self.do("task.answer", actor="owner", via="dashboard", task_id=t["id"], answer="a" * 8193)
        with self.assertRaisesRegex(ValueError, "title is too long"):              # nested fields too
            self.do("task.update", actor="owner", via="dashboard", task_id=t["id"], fields={"title": long})

    def test_failed_action_rolls_back_everything(self):
        t = self.do("task.add", title="x", project="P")
        with self.assertRaises(store.InvalidTransition):                     # fields ok, status fails
            self.do("task.update", task_id=t["id"], fields={"title": "renamed"}, status="pending")
        self.assertEqual(store.get_task(self.conn, t["id"])["title"], "x")
        self.do("task.block", task_id=t["id"], question="q")
        with self.assertRaisesRegex(store.InvalidTransition, "answer it with afclaude_answer_task"):
            self.do("task.update", task_id=t["id"], fields={"title": "renamed"}, status="pending")
        self.assertEqual(store.get_task(self.conn, t["id"])["title"], "x")

    def test_inside_a_caller_transaction(self):
        with store.transaction(self.conn):
            self.do("task.add", title="a")
            self.assertTrue(self.conn.in_transaction)                        # savepoint, caller commits
        self.assertEqual(self.count("audit_log"), 1)


class Audit(Base):
    def test_rows_actor_via_and_changed_fields_only(self):
        t = self.do("task.add", title="x", description="d", project="P", idem=KEY)
        self.do("task.edit", actor="mcp:" + SID, via="mcp", task_id=t["id"], title="y")
        self.do("task.edit", task_id=t["id"], title="y")                     # no-op: no row
        rows = self.audit()
        self.assertEqual([(r["action"], r["actor"], r["via"], r["target_type"], r["target_id"]) for r in rows],
                         [("task.add", "cli", "cli", "task", str(t["id"])),
                          ("task.edit", "mcp:" + SID, "mcp", "task", str(t["id"]))])
        self.assertIsNone(rows[0]["before"])
        self.assertEqual((rows[0]["after"]["title"], rows[0]["after"]["project"]), ("x", "P"))
        self.assertEqual(rows[0]["request_id"], KEY)
        self.assertEqual((rows[1]["before"], rows[1]["after"]), ({"title": "x"}, {"title": "y"}))
        self.assertIsNone(rows[1]["request_id"])

    def test_append_only(self):
        self.do("task.add", title="x")
        for sql in ("UPDATE audit_log SET actor='owner'", "DELETE FROM audit_log"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                self.conn.execute(sql)
        self.conn.rollback()
        self.assertEqual(self.count("audit_log"), 1)

    def test_cli_and_mcp_style_callers_write_audit_rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = tasks.main(["--db", self.db, "add", "from the cli", "--project", "P", "--json"])
        self.assertEqual(rc, 0)
        tid = json.loads(out.getvalue())["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            tasks.main(["--db", self.db, "prio", str(tid), "low"])
        rows = self.audit()
        self.assertEqual([(r["action"], r["actor"], r["via"]) for r in rows],
                         [("task.add", "cli", "cli"), ("task.priority", "cli", "cli")])
        self.assertEqual((rows[1]["before"], rows[1]["after"]), ({"priority": "high"}, {"priority": "low"}))


class Idempotency(Base):
    def test_replay_returns_the_stored_response_without_acting(self):
        a = self.do("task.add", idem=KEY, title="once")
        b = self.do("task.add", idem=KEY, title="once")
        self.assertEqual(a, b)
        self.assertEqual(self.count("tasks"), 1)
        self.assertEqual(self.count("audit_log"), 1)
        c2 = store.connect(self.db)                                          # another process sees the key
        try:
            self.assertEqual(actions.perform(c2, "task.add", {"title": "once"}, actor="cli", via="cli", key=KEY), a)
        finally:
            c2.close()
        self.assertEqual(self.count("tasks"), 1)

    def test_replay_of_a_transition_is_not_a_conflict(self):
        t = self.do("task.add", title="x")
        self.do("task.block", task_id=t["id"], question="q")
        r1 = self.do("task.answer", actor="owner", via="dashboard", idem=KEY, task_id=t["id"], answer="yes")
        r2 = self.do("task.answer", actor="owner", via="dashboard", idem=KEY, task_id=t["id"], answer="yes")
        self.assertEqual(r1, r2)
        self.assertEqual(self.events(t["id"]).count("answered"), 1)

    def events(self, tid):
        return [e["event"] for e in store.task_events(self.conn, tid)]

    def test_same_key_other_request_or_actor_is_a_conflict(self):
        self.do("task.add", idem=KEY, title="once")
        with self.assertRaisesRegex(actions.Conflict, "already used for a different request"):
            self.do("task.add", idem=KEY, title="twice")
        with self.assertRaisesRegex(actions.Conflict, "already used"):
            self.do("task.add", idem=KEY, actor="owner", via="dashboard", title="once")
        with self.assertRaisesRegex(actions.Conflict, "already used"):
            self.do("project.add", idem=KEY, name="once")
        self.assertEqual(self.count("tasks"), 1)

    def test_a_failed_request_stores_no_key(self):
        with self.assertRaises(ValueError):
            self.do("task.add", idem=KEY, title="x", priority="urgent")
        self.assertEqual(self.count("idempotency_keys"), 0)
        self.do("task.add", idem=KEY, title="x", priority="low")                 # the retry acts
        self.assertEqual(self.count("idempotency_keys"), 1)

    def test_keys_are_pruned_after_seven_days(self):
        self.do("task.add", idem=KEY, title="old")
        self.clock.t += timedelta(days=7, seconds=5)
        self.do("task.add", idem="req-0002-abcdef", title="new")
        self.assertIsNone(store.get_idempotency(self.conn, KEY))
        self.do("task.add", idem=KEY, title="old")                            # acts again: it is a new key
        self.assertEqual(self.count("tasks"), 3)


class Versions(Base):
    def test_every_update_bumps_the_version_whoever_writes(self):
        t = self.do("task.add", title="x", project="P")
        self.assertEqual(t["version"], 0)
        t = self.do("task.edit", task_id=t["id"], title="y")
        self.assertEqual(t["version"], 1)
        store.start_task(self.conn, t["id"], SID)                            # the dispatcher, directly
        self.assertEqual(store.get_task(self.conn, t["id"])["version"], 2)
        self.conn.execute("UPDATE tasks SET title='raw' WHERE id=?", (t["id"],))
        self.assertEqual(store.get_task(self.conn, t["id"])["version"], 3)
        self.conn.execute("UPDATE tasks SET title='set', version=10 WHERE id=?", (t["id"],))
        self.assertEqual(store.get_task(self.conn, t["id"])["version"], 10)  # an explicit value is kept
        self.conn.commit()

    def test_stale_version_conflicts_with_the_current_row(self):
        t = self.do("task.add", title="x")
        self.do("task.edit", task_id=t["id"], title="by someone else")
        with self.assertRaises(actions.Conflict) as cm:
            self.do("task.edit", task_id=t["id"], version=0, title="mine")
        self.assertEqual(cm.exception.current["title"], "by someone else")
        self.assertEqual(cm.exception.current["version"], 1)
        self.assertEqual(self.do("task.edit", task_id=t["id"], version=1, title="mine")["title"], "mine")
        for name, extra in (("task.priority", {"priority": "low"}), ("task.cancel", {}),
                            ("task.block", {"question": "q"})):
            with self.assertRaises(actions.Conflict, msg=name):
                self.do(name, task_id=t["id"], version=0, **extra)

    def test_project_rule_and_decision_versions(self):
        p = self.do("project.add", name="P")
        q = self.do("project.add", name="Q")
        self.do("project.move", project="Q", rank=1)                         # moves P too: P's version bumps
        self.assertEqual(store.get_project(self.conn, p["id"])["version"], 1)
        with self.assertRaises(actions.Conflict):
            self.do("project.edit", project="P", version=0, description="d")
        self.do("project.edit", project="P", version=1, description="d")
        with self.assertRaises(actions.Conflict):
            self.do("project.move", project=q["id"], version=5, rank=2)
        r = self.do("rule.add", scope="project", match="/x", decision="continue")
        self.do("rule.add", scope="project", match="/x", decision="ignore")  # replaces: version 1
        with self.assertRaises(actions.Conflict):
            self.do("rule.remove", rule_id=r["id"], version=0)
        self.do("rule.remove", rule_id=r["id"], version=1)
        self.stall(SID)
        with self.assertRaises(actions.Conflict):
            self.do("session.decide", session_id=SID, decision="continue", version=3)
        d = self.do("session.decide", session_id=SID, decision="continue", version=0)
        self.assertEqual(d["version"], 0)
        self.assertEqual(self.do("session.decide", session_id=SID, decision="ignore", version=0)["version"], 1)


# ---------------------------------------------------------------- the actions

class TaskActions(Base):
    def test_lifecycle_and_moves(self):
        a = self.do("task.add", title="a", project="P")
        b = self.do("task.add", title="b", project="P", priority="low", created_by_session=SID)
        self.assertEqual((b["stage_seq"], b["priority"], b["created_by_session"]), (2, "low", SID))
        self.assertEqual(self.do("task.move", task_id=b["id"], stage=1)["stage_seq"], 1)
        self.do("task.move", task_id=b["id"], stage=1)                       # no-op, no audit
        self.assertEqual(self.do("task.priority", task_id=a["id"], priority="medium")["priority"], "medium")
        self.assertEqual(self.do("task.start", task_id=a["id"], session=SID)["status"], "in_progress")
        self.assertEqual(self.do("task.block", task_id=a["id"], question="which?")["status"], "blocked")
        self.assertEqual(self.do("task.answer", task_id=a["id"], answer="this")["status"], "pending")
        self.assertEqual(self.do("task.finish", task_id=a["id"], summary="ok")["status"], "done")
        self.assertEqual(self.do("task.reopen", task_id=a["id"], reason="again")["status"], "pending")
        self.assertEqual(self.do("task.cancel", task_id=a["id"], reason="no")["status"], "cancelled")
        self.assertEqual(self.do("task.edit", task_id=b["id"], project=None)["project"], None)
        acts = [r["action"] for r in self.audit()]
        self.assertEqual(acts, ["task.add", "task.add", "task.move", "task.priority", "task.start", "task.block",
                                "task.answer", "task.finish", "task.reopen", "task.cancel", "task.edit"])

    def test_update_is_one_atomic_action(self):
        t = self.do("task.add", title="a", project="P")
        self.do("task.add", title="b", project="P")
        r = self.do("task.update", actor="mcp:" + SID, via="mcp", task_id=t["id"],
                    fields={"title": "A", "priority": "low"}, stage=2, status="in_progress", session=SID)
        self.assertEqual((r["title"], r["priority"], r["stage_seq"], r["status"], r["assigned_session"]),
                         ("A", "low", 2, "in_progress", SID))
        rows = self.audit(target_type="task", target_id=t["id"])
        self.assertEqual([x["action"] for x in rows], ["task.add", "task.update"])
        self.assertEqual(rows[1]["after"]["status"], "in_progress")
        with self.assertRaises(store.InvalidTransition):                     # in_progress can't be blocked
            self.do("task.answer", task_id=t["id"], answer="early")
        self.assertEqual(self.do("task.update", task_id=t["id"], status="blocked", note="q?")["status"], "blocked")
        self.assertEqual(self.do("task.answer", task_id=t["id"], answer="a")["status"], "pending")
        self.assertEqual(self.do("task.update", task_id=t["id"], status="done", note="fin")["result_summary"], "fin")
        self.assertEqual(self.do("task.update", task_id=t["id"], status="pending")["status"], "pending")
        self.assertEqual(self.do("task.update", task_id=t["id"], status="cancelled", note="x")["status"],
                         "cancelled")
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            self.do("task.update", task_id=t["id"], status="paused")
        with self.assertRaisesRegex(ValueError, "fields must be an object"):
            self.do("task.update", task_id=t["id"], fields=["title"])

    def test_answer_replay_from_the_dashboard_only(self):
        t = self.do("task.add", title="a")
        self.do("task.block", task_id=t["id"], question="q?")
        self.do("task.answer", actor="owner", via="dashboard", task_id=t["id"], answer="yes")
        n = self.count("audit_log")
        again = self.do("task.answer", actor="owner", via="dashboard", task_id=t["id"], answer=" yes ")
        self.assertEqual(again["status"], "pending")
        self.assertEqual(self.count("audit_log"), n)                         # no-op
        with self.assertRaises(store.InvalidTransition):
            self.do("task.answer", actor="owner", via="dashboard", task_id=t["id"], answer="no")
        with self.assertRaises(store.InvalidTransition):                     # CLI/MCP: unchanged, an error
            self.do("task.answer", task_id=t["id"], answer="yes")


class Questions(Base):
    def test_ask_answer_reopen(self):
        q = self.do("task.ask", actor="mcp:" + SID, via="mcp", question="Deploy now?\nDetails follow.",
                    project="AFClaude")
        self.assertEqual((q["kind"], q["status"], q["title"], q["blocked_question"], q["created_by_session"]),
                         ("question", "blocked", "Deploy now?", "Deploy now?\nDetails follow.", SID))
        self.assertEqual([t["id"] for t in store.pending_user_input(self.conn)["blocked_tasks"]], [q["id"]])
        self.assertEqual(store.execution_order(self.conn, kind="task"), [])  # the dispatcher never sees it
        with self.assertRaisesRegex(store.InvalidTransition, "is a question"):
            self.do("task.start", task_id=q["id"], session=SID)
        a = self.do("task.answer", task_id=q["id"], answer="Yes, go.")
        self.assertEqual((a["status"], a["answer"]), ("done", "Yes, go."))
        self.assertIsNotNone(a["done_at"])
        self.assertEqual(store.pending_user_input(self.conn)["blocked_tasks"], [])
        r = self.do("task.reopen", task_id=q["id"], reason="asked again")
        self.assertEqual((r["status"], r["answer"]), ("blocked", None))
        self.assertEqual([t["id"] for t in store.list_tasks(self.conn, kind="question")], [q["id"]])
        long = self.do("task.ask", question="x" * 300)
        self.assertEqual(len(long["title"]), 120)

    def test_question_kind_only_through_ask(self):
        with self.assertRaisesRegex(ValueError, "ask_question"):
            self.do("task.add", title="x", kind="question")
        t = self.do("task.add", title="x")
        with self.assertRaisesRegex(ValueError, "only for questions"):
            self.do("task.edit", task_id=t["id"], kind="question")
        with self.assertRaisesRegex(ValueError, "question must not be empty"):
            self.do("task.ask", question=" ")


class ProjectActions(Base):
    def test_add_edit_move_priority(self):
        p = self.do("project.add", name="P", description="d", path="/home/x/p")
        self.do("project.add", name="Q", rank=1)
        self.assertEqual(store.get_project(self.conn, "P")["rank"], 2)
        self.do("task.add", title="s1", project="P")
        self.do("task.add", title="s2", project="P")
        r = self.do("project.priority", project="P", priority="low")
        self.assertEqual(len(r["changed"]), 2)
        n = self.count("audit_log")
        self.do("project.priority", project="P", priority="low")             # nothing changes: no audit
        self.assertEqual(self.count("audit_log"), n)
        self.assertEqual(self.do("project.move", project="P", rank=1)["rank"], 1)
        n = self.count("audit_log")
        self.do("project.move", project="P", rank=1)                         # unchanged: no-op
        self.assertEqual(self.count("audit_log"), n)
        e = self.do("project.edit", project=p["id"], name="P2", manager_session=SID)
        self.assertEqual((e["name"], e["manager_session"]), ("P2", SID))
        self.assertEqual(self.do("project.edit", project="P2", manager_session=None)["manager_session"], None)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.do("project.add", name="Q")
        last = self.audit(target_type="project")[-1]
        self.assertEqual((last["before"], last["after"]), ({"manager_session": SID}, {"manager_session": None}))


class SessionActions(Base):
    def test_decide_clear_and_stall_ref(self):
        self.stall(SID, uuid="n1")
        d = self.do("session.decide", session_id=SID, decision="continue", note="go", stall_ref="n1")
        self.assertEqual((d["decision"], d["stall_ref"]), ("continue", "n1"))
        self.assertEqual(store.effective_decision(self.conn, SID), ("continue", "session"))
        self.stall(SID, uuid="n2")                                           # stalled again
        with self.assertRaisesRegex(actions.Conflict, "stalled again") as cm:
            self.do("session.decide", session_id=SID, decision="ignore", stall_ref="n1")
        self.assertEqual(cm.exception.current["stall_ref"], "n2")
        self.assertEqual(self.do("session.clear", session_id=SID), {"session_id": SID, "cleared": True})
        self.assertEqual(self.do("session.clear", session_id=SID), {"session_id": SID, "cleared": False})
        self.assertEqual([r["action"] for r in self.audit()], ["session.decide", "session.clear"])
        with self.assertRaises(store.NotFound):
            self.do("session.decide", session_id=SID2, decision="continue")

    def test_rules(self):
        r = self.do("rule.add", scope="project", match="/home/x/proj/", decision="continue", note="n")
        self.assertEqual(r["match"], "/home/x/proj")
        n = self.count("audit_log")
        self.do("rule.add", scope="project", match="/home/x/proj", decision="continue", note="n")   # same
        self.assertEqual(self.count("audit_log"), n)
        self.do("rule.add", scope="project", match="/home/x/proj", decision="ignore", note="n")
        self.assertEqual(len(store.list_rules(self.conn)), 1)
        self.assertEqual(self.do("rule.remove", rule_id=r["id"]), {"removed": r["id"]})
        with self.assertRaises(store.NotFound):
            self.do("rule.remove", rule_id=r["id"])
        with self.assertRaisesRegex(ValueError, "scope must be one of"):
            self.do("rule.add", scope="host", match="x", decision="continue")
        rows = self.audit(target_type="rule")
        self.assertEqual([x["action"] for x in rows], ["rule.add", "rule.add", "rule.remove"])
        self.assertEqual((rows[1]["before"]["decision"], rows[1]["after"]["decision"]), ("continue", "ignore"))
        self.assertIsNone(rows[2]["after"])


class Autonomous(Base):
    OWNER_ONLY = {"session.decide", "session.clear", "rule.add", "rule.remove", "setting.set", "setting.reset",
                  "setting.import", "automation.set", "window.set", "prompt.set", "prompt.reset", "request.add"}

    def test_owner_only_set(self):
        self.assertEqual({n for n, s in actions.ACTIONS.items() if s.owner_only}, self.OWNER_ONLY)

    def test_autonomous_mcp_sessions_cannot_decide_or_write_rules(self):
        self.stall(SID)
        with open(actions.OWN_LIST, "w") as fh:
            fh.write(AUTO + "\n")
        mgr = "00000000-0000-4000-8000-0000000000bb"
        drv = "00000000-0000-4000-8000-0000000000cc"
        self.do("project.add", name="Managed")
        self.do("project.edit", project="Managed", manager_session=mgr)
        store.upsert_driven_session(self.conn, drv, kind="task")
        self.conn.commit()
        cases = [dict(actor="mcp:" + AUTO), dict(actor="mcp:" + mgr), dict(actor="mcp:" + drv),
                 dict(actor="mcp:" + afclaude_config.manager_session()), dict(actor="mcp", autonomous=True),
                 dict(actor="mcp:" + SID2, autonomous=True)]
        for who in cases:
            for name, params in (("session.decide", {"session_id": SID, "decision": "continue"}),
                                 ("session.clear", {"session_id": SID}),
                                 ("rule.add", {"scope": "project", "match": "/x", "decision": "continue"}),
                                 ("automation.set", {"paused": True}),
                                 ("prompt.set", {"name": "manager.md", "body": "evil"}),
                                 ("request.add", {"kind": "review_now"})):
                with self.assertRaises(actions.Forbidden, msg=(who, name)):
                    self.do(name, via="mcp", **who, **params)
            t = self.do("task.add", via="mcp", title="allowed", **who)       # Q3: tasks like anyone's
            self.assertEqual(t["status"], "pending")
        self.assertEqual(store.list_rules(self.conn), [])
        self.assertIsNone(store.get_decision(self.conn, SID))
        # the owner's own sessions via MCP, and the CLI (even with the flag), may
        self.do("session.decide", actor="mcp:" + SID2, via="mcp", session_id=SID, decision="continue")
        self.do("rule.add", actor="cli", via="cli", autonomous=True, scope="project", match="/x",
                decision="ignore")
        self.assertEqual(len(store.list_rules(self.conn)), 1)


class SettingActions(Base):
    def test_defaults_reproduce_todays_behaviour(self):
        s = actions.settings(self.conn)
        self.assertEqual(set(s), set(actions.SETTINGS))
        self.assertTrue(all(v["source"] == "default" and v["version"] == 0 for v in s.values()))
        self.assertEqual(s["window_days"]["value"],
                         {d: {"start": "23:00", "n": 2, "group": "weekly"} for d in actions.DAYS})
        self.assertEqual({k: v["value"] for k, v in s.items() if k != "window_days"},
                         {"window_tz": "Europe/Berlin", "session_hours": 5.0, "usage_model": "pacing",
                          "reserve_threshold": "auto", "last_mile_hours": "auto", "pacing_idle_min": 60.0,
                          "pacing_min_gap": 1.0, "pacing_session_cap": 85.0, "pacing_last_mile_yield": True,
                          "projection_threshold": 90.0, "cutoff_after_window_hours": 2.0,
                          "automation_paused": False, "stall_take_over_idle": True, "stall_verify_minutes": 15.0,
                          "cleanup_finished_grace_minutes": 10.0, "cleanup_idle_hours": 2.0})

    def test_registry_metadata(self):
        """Every setting has a section, a type, a one-line explanation (D-075) and a default that
        passes its own check; numbers carry their range, enums their choices."""
        s = actions.settings(self.conn)
        for k, spec in actions.SETTINGS.items():
            self.assertIn(spec.section, actions.SECTIONS, k)
            self.assertTrue(spec.doc and "\n" not in spec.doc, k)
            self.assertEqual(spec.check(spec.default(), self.conn), spec.default(), k)
            self.assertEqual((s[k]["section"], s[k]["type"], s[k]["doc"]), (spec.section, spec.kind, spec.doc))
            if spec.kind in ("number", "auto|number"):
                lo, hi = spec.bounds
                self.assertEqual((s[k]["min"], s[k]["max"]), (lo, hi), k)
                with self.assertRaises(ValueError, msg=k):
                    spec.check(hi + 1, self.conn)
        self.assertEqual(s["usage_model"]["choices"], ["pacing", "linear"])
        self.assertEqual({k for k, v in s.items() if v["important"]},
                         {"window_days", "reserve_threshold", "last_mile_hours", "automation_paused"})
        self.assertNotIn("session_usage_stop", s)                   # D-205/D-206: nothing reads it

    def test_defaults_come_from_the_code(self):
        """D-169: a local file never feeds a default (its old tunables are imported once instead)."""
        with open(afclaude_config.CONFIG_FILE, "w") as fh:
            json.dump({"last_mile_hours": 2, "window_start": "22:30", "window_hours": 5, "week_target": 92}, fh)
        self.assertEqual(actions.get_setting(self.conn, "last_mile_hours"), "auto")
        self.assertEqual(actions.get_setting(self.conn, "reserve_threshold"), "auto")
        self.assertEqual(actions.get_setting(self.conn, "window_days")["wed"], {"start": "23:00", "n": 2,
                                                                               "group": "weekly"})

    def test_effective_setting_rechecks_the_saved_value(self):
        self.assertEqual(actions.effective_setting(self.conn, "pacing_idle_min"), (60.0, "default"))
        self.do("setting.set", key="pacing_idle_min", value=30)
        self.assertEqual(actions.effective_setting(self.conn, "pacing_idle_min"), (30.0, "db"))
        store.put_setting(self.conn, "pacing_idle_min", 5000)       # around actions.py: out of range
        value, source = actions.effective_setting(self.conn, "pacing_idle_min")
        self.assertEqual(value, 60.0)
        self.assertRegex(source, r"^invalid: .*0\.\.1440")

    def test_set_reset_versions(self):
        r = self.do("setting.set", actor="owner", via="dashboard", key="projection_threshold", value=80)
        self.assertEqual((r["value"], r["version"], r["source"]), (80.0, 1, "db"))
        with self.assertRaises(actions.Conflict):
            self.do("setting.set", key="projection_threshold", value=70, version=0)
        n = self.count("audit_log")
        self.do("setting.set", key="projection_threshold", value=80, version=1)      # same value: no-op
        self.assertEqual(self.count("audit_log"), n)
        r = self.do("setting.reset", key="projection_threshold", version=1)
        self.assertEqual((r["value"], r["version"], r["source"]), (90.0, 2, "default"))
        self.assertEqual(store.get_setting_row(self.conn, "projection_threshold")["updated_by"], "cli")
        r = self.do("setting.set", key="projection_threshold", value=95, version=2)  # versions never repeat
        self.assertEqual(r["version"], 3)
        rows = self.audit(target_type="setting")
        self.assertEqual([(x["action"], x["before"], x["after"]) for x in rows],
                         [("setting.set", {"value": None}, {"value": 80.0}),
                          ("setting.reset", {"value": 80.0}, {"value": None}),
                          ("setting.set", {"value": None}, {"value": 95.0})])

    def test_validation(self):
        for key, value, msg in (("nope", 1, "unknown setting"), ("projection_threshold", 0, "1..100"),
                                ("projection_threshold", "90", "number"), ("automation_paused", 1, "true or false"),
                                ("window_tz", "Mars/Olympus", "unknown time zone"),
                                ("last_mile_hours", -1, "0..168"), ("last_mile_hours", "soon", "auto"),
                                ("session_hours", None, "not be null"),
                                ("reserve_threshold", 40, "50..99"), ("reserve_threshold", "high", "auto")):
            with self.assertRaisesRegex(ValueError, msg, msg=key):
                self.do("setting.set", key=key, value=value)

    def test_reserve_threshold_auto_or_override(self):
        self.assertEqual(self.do("setting.set", key="reserve_threshold", value=88)["value"], 88.0)
        self.assertEqual(self.do("setting.set", key="reserve_threshold", value="auto", version=1)["value"], "auto")
        self.do("setting.reset", key="reserve_threshold", version=2)
        self.assertEqual(actions.get_setting(self.conn, "reserve_threshold"), "auto")
        for key, value, msg in (("usage_model", "reserve", "pacing|linear"),
                                ("pacing_last_mile_yield", "no", "true or false"),
                                ("stall_verify_minutes", 0, "1..240")):
            with self.assertRaisesRegex(ValueError, msg, msg=key):
                self.do("setting.set", key=key, value=value)

    def test_automation_pause_is_idempotent(self):
        self.assertEqual(self.do("automation.set", paused=True)["value"], True)
        self.assertTrue(actions.get_setting(self.conn, "automation_paused"))
        self.do("automation.set", paused=True)
        self.assertEqual(len(self.audit(target_type="setting")), 1)
        self.assertEqual(self.do("automation.set", paused=False)["value"], False)
        with self.assertRaisesRegex(ValueError, "true or false"):
            self.do("automation.set", paused="yes")


class WindowActions(Base):
    def days(self):
        return actions.get_setting(self.conn, "window_days")

    def test_linked_individual_week_link(self):
        r = self.do("window.set", day="mon", start="22:00", n=2)            # linked: the whole weekly group
        self.assertEqual(r["changed_days"], list(actions.DAYS))
        self.assertEqual({w["start"] for w in self.days().values()}, {"22:00"})
        r = self.do("window.set", day="sat", start="20:00", n=2, mode="individual", version=r["version"])
        self.assertEqual(r["changed_days"], ["sat"])
        d = self.days()
        self.assertEqual((d["sat"]["group"], d["fri"]["group"]), ("g1", "weekly"))
        self.do("window.set", day="sun", start="20:00", n=2, mode="individual")     # same window, own group
        self.assertEqual(self.days()["sun"]["group"], "g2")
        r = self.do("window.set", day="sun", start=None, mode="link", group="g1")   # re-link with one tap
        self.assertEqual(r["value"]["sun"], {"start": "20:00", "n": 2, "group": "g1"})
        self.do("window.set", day="sat", start="21:00", n=2)                 # linked: sat + sun move
        d = self.days()
        self.assertEqual((d["sat"]["start"], d["sun"]["start"], d["fri"]["start"]), ("21:00", "21:00", "22:00"))
        self.do("window.set", day="tue", start=None, mode="individual")      # off
        self.assertIsNone(self.days()["tue"])
        self.do("window.set", day="tue", start="23:30", n=1)                 # linked on an off day: individual
        self.assertEqual(self.days()["tue"], {"start": "23:30", "n": 1, "group": "g2"})
        r = self.do("window.set", day=None, start="23:00", n=2, mode="week")
        self.assertEqual({w["group"] for w in r["value"].values()}, {"g3"})
        self.do("window.set", day=None, start=None, mode="week")             # no window any night
        self.assertEqual(set(self.days().values()), {None})

    def test_validation_names_the_conflicting_day(self):
        self.do("window.set", day="mon", start="23:00", n=2, mode="individual")
        with self.assertRaisesRegex(ValueError, r"mon 23:00 x 2 ends tue 09:00, so tue can't start at 08:00"):
            self.do("window.set", day="tue", start="08:00", n=1, mode="individual")
        self.do("window.set", day="tue", start="09:00", n=1, mode="individual")      # touching is fine
        with self.assertRaisesRegex(ValueError, r"sun 23:00 x 2 ends mon 09:00, so mon can't start at 06:00"):
            self.do("window.set", day="mon", start="06:00", n=2, mode="individual")  # week wraps around
        for kw, msg in ((dict(start="23:15", n=1), "30-minute grid"), (dict(start="24:00", n=1), "30-minute grid"),
                        (dict(start="23:00", n=0), ">= 1"), (dict(start="23:00", n=5), "longer than a day"),
                        (dict(start="23:00"), "n .* is needed"), (dict(start="23:00", n=True), ">= 1")):
            with self.assertRaisesRegex(ValueError, msg, msg=kw):
                self.do("window.set", day="wed", mode="individual", **kw)
        with self.assertRaisesRegex(ValueError, "day must be one of"):
            self.do("window.set", day="monday", start="23:00", n=1)
        with self.assertRaisesRegex(ValueError, "mode must be"):
            self.do("window.set", day="mon", start="23:00", n=1, mode="all")
        with self.assertRaisesRegex(ValueError, "no window has link group"):
            self.do("window.set", day="mon", start=None, mode="link", group="zz")
        with self.assertRaisesRegex(ValueError, "linked .* different windows"):
            bad = {d: {"start": "23:00", "n": 1, "group": "weekly"} for d in actions.DAYS}
            bad["fri"]["start"] = "22:00"
            self.do("setting.set", key="window_days", value=bad)

    def test_version_check(self):
        r = self.do("window.set", day="mon", start="22:00", n=2)
        self.assertEqual(r["version"], 1)
        with self.assertRaises(actions.Conflict):
            self.do("window.set", day="mon", start="21:00", n=2, version=0)
        self.do("window.set", day="mon", start="21:00", n=2, version=1)


class PromptActions(Base):
    def test_set_validate_reset(self):
        body = "Go on ({reason}); {progress}. {{literal}}"
        r = self.do("prompt.set", actor="owner", via="dashboard", name="continue.md", body=body)
        self.assertEqual((r["body"], r["version"], r["updated_by"]), (body, 1, "owner"))
        with open(os.path.join(actions.PROMPTS_DIR, "continue.md"), "rb") as fh:
            import hashlib
            self.assertEqual(r["base_sha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertEqual(actions.prompt_text(self.conn, "continue.md"), body)
        for bad, msg in (("Go on {reason}", r"missing \['progress'\]"),
                         ("{reason} {progress} {extra}", r"unknown \['extra'\]"),
                         ("{reason} {progress} {", "unbalanced braces"),
                         ("{reason.x} {progress}", "does not render"),
                         ("  ", "must not be empty"), ("{reason}{progress}" + "x" * 17000, "too long")):
            with self.assertRaisesRegex(ValueError, msg, msg=bad):
                self.do("prompt.set", name="continue.md", body=bad)
        with self.assertRaises(actions.Conflict):
            self.do("prompt.set", name="continue.md", body=body + "!", version=0)
        r = self.do("prompt.reset", name="continue.md", version=1)
        self.assertEqual((r["body"], r["version"]), (None, 2))
        self.assertTrue(actions.prompt_text(self.conn, "continue.md").startswith("Continue ("))
        self.assertEqual(store.list_prompt_overrides(self.conn), [])
        self.do("prompt.reset", name="continue.md")                          # nothing to reset: no-op
        rows = self.audit(target_type="prompt")
        self.assertEqual([(x["action"], x["before"], x["after"]) for x in rows],
                         [("prompt.set", {"body": None}, {"body": body}),     # old bodies stay in the audit log
                          ("prompt.reset", {"body": body}, {"body": None})])

    def test_names(self):
        for bad in ("../store.py", "README.md", "Continue.md", "continue", "a/b.md"):
            with self.assertRaisesRegex(ValueError, "bad prompt name", msg=bad):
                self.do("prompt.set", name=bad, body="x")
        with self.assertRaises(store.NotFound):
            self.do("prompt.set", name="nope.md", body="x")
        self.assertEqual(self.do("prompt.set", name="manager.md", body="Be brief.")["body"], "Be brief.")


class RequestActions(Base):
    def test_one_open_request_per_kind_and_target(self):
        self.stall(SID)
        r = self.do("request.add", actor="owner", via="dashboard", kind="continue_now", target=SID)
        self.assertEqual((r["status"], r["actor"], r["target"]), ("open", "owner", SID))
        again = self.do("request.add", actor="owner", via="dashboard", kind="continue_now", target=SID)
        self.assertEqual(again["id"], r["id"])
        rv = self.do("request.add", kind="review_now")
        self.assertNotEqual(rv["id"], r["id"])
        self.assertEqual(self.do("request.add", kind="review_now")["id"], rv["id"])
        self.assertEqual(self.count("action_requests"), 2)
        self.assertEqual(len(self.audit(target_type="request")), 2)
        self.conn.execute("UPDATE action_requests SET status='done' WHERE id=?", (rv["id"],))
        self.conn.commit()
        self.assertNotEqual(self.do("request.add", kind="review_now")["id"], rv["id"])
        with self.assertRaises(store.NotFound):
            self.do("request.add", kind="continue_now", target=SID2)
        with self.assertRaisesRegex(ValueError, "target is required"):
            self.do("request.add", kind="continue_now")
        with self.assertRaisesRegex(ValueError, "takes no target"):
            self.do("request.add", kind="review_now", target=SID)
        with self.assertRaisesRegex(ValueError, "kind must be one of"):
            self.do("request.add", kind="deploy")


class EveryAction(Base):
    def test_every_registered_action_writes_one_audit_row(self):
        self.stall(SID)
        script = [
            ("task.add", {"title": "t", "project": "P"}), ("task.ask", {"question": "q?"}),
            ("task.edit", {"task_id": 1, "description": "d"}), ("task.priority", {"task_id": 1, "priority": "low"}),
            ("task.add", {"title": "t2", "project": "P"}), ("task.move", {"task_id": 3, "stage": 1}),
            ("task.start", {"task_id": 1, "session": SID}), ("task.block", {"task_id": 1, "question": "?"}),
            ("task.answer", {"task_id": 1, "answer": "!"}), ("task.finish", {"task_id": 1}),
            ("task.reopen", {"task_id": 1}), ("task.cancel", {"task_id": 1}),
            ("task.update", {"task_id": 3, "fields": {"title": "T2"}}),
            ("project.add", {"name": "Q"}), ("project.edit", {"project": "Q", "description": "d"}),
            ("project.move", {"project": "Q", "rank": 1}), ("project.priority", {"project": "P", "priority": "low"}),
            ("session.decide", {"session_id": SID, "decision": "continue"}), ("session.clear", {"session_id": SID}),
            ("rule.add", {"scope": "session", "match": SID, "decision": "ignore"}),
            ("rule.remove", {"rule_id": 1}),
            ("setting.set", {"key": "stall_verify_minutes", "value": 20}),
            ("setting.reset", {"key": "stall_verify_minutes"}), ("automation.set", {"paused": True}),
            ("setting.import", {"source": "data/dispatcher.json", "values": {"cleanup_idle_hours": 3}}),
            ("window.set", {"day": "fri", "start": "22:00", "n": 2, "mode": "individual"}),
            ("prompt.set", {"name": "manager.md", "body": "x"}), ("prompt.reset", {"name": "manager.md"}),
            ("request.add", {"kind": "continue_now", "target": SID}),
        ]
        self.assertEqual({n for n, _ in script}, set(actions.ACTIONS))      # a new action needs a line here
        for i, (name, params) in enumerate(script, 1):
            self.do(name, actor="owner", via="dashboard", idem=f"every-{i:04d}", **params)
            rows = self.audit()
            self.assertEqual(len(rows), i, name)
            self.assertEqual((rows[-1]["action"], rows[-1]["request_id"]), (name, f"every-{i:04d}"))


# ---------------------------------------------------------------- v3 -> v4

class MigrationV4(unittest.TestCase):
    def make_v3(self, path, data=True):
        old = sqlite3.connect(path)
        old.executescript(V3_SCHEMA)
        if data:
            old.executescript(f"""
            INSERT INTO sessions(session_id, cwd, project_dir, stalled, stalled_since, stall_uuid)
                VALUES ('{SID}', '/home/x/proj', '-home-x-proj', 1, '2026-09-30T09:00:00Z', 'n1');
            INSERT INTO limit_hits(session_id, entry_uuid, ts, kind) VALUES ('{SID}', 'n1', '2026-09-30T09:00:00Z', 's');
            INSERT INTO projects(id, name, rank, path, created_at, updated_at)
                VALUES (1, 'proj', 1, '/home/x/proj', '2026-09-30T08:00:00.000Z', '2026-09-30T08:00:00.000Z');
            INSERT INTO tasks(id, title, project_id, stage_seq, priority, status, created_at, updated_at,
                              blocked_question) VALUES
                (1, 'a', 1, 1, 'high', 'pending', '2026-09-30T08:00:00.000Z', '2026-09-30T08:00:00.000Z', NULL),
                (2, 'b', 1, 2, 'low', 'blocked', '2026-09-30T08:01:00.000Z', '2026-09-30T08:01:00.000Z', 'q?');
            INSERT INTO task_events(task_id, ts, event, detail) VALUES (1, '2026-09-30T08:00:00.000Z', 'created', '{{}}'),
                (2, '2026-09-30T08:01:00.000Z', 'created', '{{}}');
            INSERT INTO session_decisions(session_id, decision, decided_at, stall_ref)
                VALUES ('{SID}', 'continue', '2026-09-30T09:01:00.000Z', 'n1');
            INSERT INTO standing_rules(scope, match, decision, created_at)
                VALUES ('project', '/home/x/proj', 'continue', '2026-09-30T08:00:00.000Z');
            """)
        old.commit()
        old.close()

    def counts(self, conn):
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("sessions", "limit_hits", "projects", "tasks", "task_events", "session_decisions",
                          "standing_rules")}

    def test_v3_db_upgrades_in_place_after_a_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v3.db")
            self.make_v3(path)
            src = sqlite3.connect(path)
            before = self.counts(src)
            src.close()
            conn = store.connect(path)
            try:
                self.assertEqual(store.get_meta(conn, "schema_version"), "4")
                self.assertIsNotNone(store.get_meta(conn, "migrated_v4_at"))
                baks = [f for f in os.listdir(d) if f.startswith("v3.db.v3-") and f.endswith(".bak")]
                self.assertEqual(len(baks), 1)
                b = sqlite3.connect(os.path.join(d, baks[0]))
                self.assertEqual(self.counts(b), before)                         # the copy is the old DB
                self.assertEqual(b.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "3")
                self.assertNotIn("version", {r[1] for r in b.execute("PRAGMA table_info(tasks)")})
                b.close()
                self.assertEqual(self.counts(conn), before)                      # data intact
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue(V4_TABLES <= tables)
                for t in ("projects", "tasks", "standing_rules", "session_decisions"):
                    self.assertEqual({r[0] for r in conn.execute(f"SELECT version FROM {t}")}, {0}, t)
                trig = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertTrue({"tasks_version", "projects_version", "audit_log_no_update",
                                 "task_events_no_update"} <= trig)
                self.assertEqual(store.effective_decision(conn, SID), ("continue", "session"))
                self.assertEqual([t["title"] for t in store.execution_order(conn)], ["a"])
                t = actions.perform(conn, "task.answer", {"task_id": 2, "answer": "yes", "version": 0},
                                    actor="cli", via="cli")
                self.assertEqual((t["status"], t["version"]), ("pending", 1))
                self.assertEqual(len(store.audit_log(conn)), 1)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                conn.close()
            conn = store.connect(path)                                           # second connect: no-op
            try:
                self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".bak")]), 1)
                self.assertEqual(store.get_task(conn, 2)["version"], 1)
                store.init(conn)                                                 # idempotent
                self.assertEqual(len(store.audit_log(conn)), 1)
            finally:
                conn.close()

    def test_empty_v3_db_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v3.db")
            self.make_v3(path, data=False)
            conn = store.connect(path)
            try:
                self.assertEqual(store.get_meta(conn, "schema_version"), "4")
                self.assertEqual([f for f in os.listdir(d) if f.endswith(".bak")], [])
            finally:
                conn.close()

    def test_fresh_db_is_v4_without_migration_mark(self):
        with tempfile.TemporaryDirectory() as d:
            conn = store.connect(os.path.join(d, "new.db"), create=True)
            try:
                self.assertEqual(store.get_meta(conn, "schema_version"), "4")
                self.assertIsNone(store.get_meta(conn, "migrated_v4_at"))
                self.assertIsNotNone(store.get_meta(conn, "install_id"))
                self.assertFalse([f for f in os.listdir(d) if f.endswith(".bak")])
            finally:
                conn.close()

    def test_v3_code_on_a_v4_db_keeps_working(self):
        """An older checkout (main's v3 store.py) reopening a v4 DB: no downgrade, and its
        plain UPDATEs still bump the versions (triggers live in the DB)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v3.db")
            self.make_v3(path)
            store.connect(path).close()
            raw = sqlite3.connect(path)
            raw.executescript(V3_SCHEMA.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
                              .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
                              .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
                              .replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ")
                              .replace("INSERT INTO meta", "INSERT OR IGNORE INTO meta"))
            raw.execute("UPDATE tasks SET title='by v3' WHERE id=1")
            raw.commit()
            self.assertEqual(raw.execute("SELECT version FROM tasks WHERE id=1").fetchone()[0], 1)
            self.assertEqual(raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "4")
            raw.close()


class DrivenSessionsAndRunLog(Base):
    def test_low_level_rows(self):
        s = store.upsert_driven_session(self.conn, SID, kind="task", tmux="ka-00000000", task_id=1)
        self.assertEqual((s["kind"], s["tmux"], s["ended_at"]), ("task", "ka-00000000", None))
        store.upsert_driven_session(self.conn, SID, ended_at=self.clock.t)
        self.assertEqual(store.driven_sessions(self.conn, include_ended=False), [])
        with self.assertRaisesRegex(ValueError, "needs kind"):
            store.upsert_driven_session(self.conn, SID2, tmux="x")
        with self.assertRaisesRegex(ValueError, "unknown driven_sessions fields"):
            store.upsert_driven_session(self.conn, SID, colour="x")
        store.add_run_log(self.conn, "dispatcher", "HOLD", "budget", session_id=SID)
        store.add_run_log(self.conn, "keepalive", "FIRE")
        self.assertEqual([r["decision"] for r in store.run_log(self.conn, "dispatcher")], ["HOLD"])
        self.assertEqual(len(store.run_log(self.conn)), 2)
        self.conn.commit()
        self.assertIn(SID, actions.autonomous_sessions(self.conn))


if __name__ == "__main__":
    unittest.main()
