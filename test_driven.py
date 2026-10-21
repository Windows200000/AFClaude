#!/usr/bin/env python3
"""Offline tests for phase 3b S1 (docs/phase3b_plan.md §5 S1): driven_sessions.project_id and the
action_requests columns (additive, on an older-shaped DB too), driven.set, the run-now request kinds
and states, store.task_managers and the driven.py backfill / check (temp DBs only; tmux is faked).

    python3 -m unittest test_driven
"""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic env first)
import actions  # noqa: E402
import driven  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

TM = "f0000000-0000-4000-8000-000000000001"        # the AFClaude task-manager
REV = "a0000000-0000-4000-8000-000000000002"       # a review session, running
REV_DEAD = "b0000000-0000-4000-8000-000000000003"  # a review session whose tmux is gone
STALL = "c0000000-0000-4000-8000-000000000004"     # a stalled session the dispatcher continued in its own tmux
FOREIGN = "d0000000-0000-4000-8000-000000000005"   # continued by typing into someone else's tmux
OWN = "e0000000-0000-4000-8000-000000000006"       # the 'own' row the live DB has

# driven_sessions and action_requests exactly as main's store.py (e9b09b7) creates them, comments dropped
OLD_TABLES = """
DROP INDEX action_requests_one_open;
DROP TABLE action_requests;
DROP TABLE driven_sessions;
CREATE TABLE driven_sessions (
    session_id TEXT PRIMARY KEY, tmux TEXT, kind TEXT NOT NULL, task_id INTEGER, started_at TEXT NOT NULL,
    last_seen TEXT, ended_at TEXT, holder_kind TEXT, rc_url TEXT);
CREATE TABLE action_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, target TEXT,
    status TEXT NOT NULL DEFAULT 'open', result TEXT, handled_at TEXT);
CREATE UNIQUE INDEX action_requests_one_open ON action_requests(kind, COALESCE(target, '')) WHERE status = 'open';
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "afclaude.db")
        self.conn = store.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def do(self, _action, /, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, _action, params, actor=actor, via=via, autonomous=autonomous)

    def audit(self, **kw):
        return list(reversed(store.audit_log(self.conn, limit=1000, **kw)))

    def project(self, name, session=None):
        p = self.do("project.add", name=name)
        if session:
            self.do("project.edit", project=name, manager_session=session)
        return p


# ---------------------------------------------------------------- schema (additive)

class Schema(unittest.TestCase):
    def test_an_older_shaped_db_gains_the_columns_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "afclaude.db")
            store.connect(db, create=True).close()
            raw = sqlite3.connect(db)            # the live DB's shape: the tables as main created them
            raw.executescript(OLD_TABLES)
            raw.execute("INSERT INTO driven_sessions(session_id, kind, started_at) VALUES (?, 'own', ?)",
                        (OWN, "2026-09-29T10:30:07.578Z"))
            raw.execute("INSERT INTO action_requests(ts, actor, kind) VALUES ('2026-10-01T00:00:00Z', 'owner', "
                        "'review_now')")
            raw.commit()
            raw.close()
            conn = store.connect(db)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(driven_sessions)")}
                self.assertIn("project_id", cols)
                self.assertTrue({"run_id", "updated_at"} <= {r[1] for r in conn.execute("PRAGMA table_info(action_requests)")})
                self.assertEqual(store.get_meta(conn, "schema_version"), "4")      # no version bump
                own = store.get_driven_session(conn, OWN)
                self.assertEqual((own["kind"], own["project_id"], own["tmux"]), ("own", None, None))
                self.assertEqual([r["status"] for r in store.list_requests(conn)], ["open"])
                self.assertEqual(store.task_managers(conn), [])
            finally:
                conn.close()
            conn = store.connect(db)               # a second connect changes nothing
            try:
                self.assertEqual(len(store.driven_sessions(conn)), 1)
            finally:
                conn.close()

    def test_a_fresh_db_has_the_columns_in_its_create(self):
        with tempfile.TemporaryDirectory() as d:
            conn = store.connect(os.path.join(d, "x.db"), create=True)
            try:
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='driven_sessions'").fetchone()[0]
                self.assertIn("project_id", ddl)
            finally:
                conn.close()


# ---------------------------------------------------------------- driven.set

class DrivenSet(Base):
    def test_new_row_validation_audit_and_noop(self):
        self.project("AFClaude", TM)
        r = self.do("driven.set", actor="runner:import", via="runner", session_id=TM, kind="task-manager",
                    tmux="ka-f0000000", project="AFClaude")
        self.assertEqual((r["kind"], r["tmux"], r["ended_at"]), ("task-manager", "ka-f0000000", None))
        self.assertEqual(r["project_id"], store.get_project(self.conn, "AFClaude")["id"])
        rows = self.audit(target_type="driven_session")
        self.assertEqual([(a["action"], a["actor"], a["target_id"]) for a in rows], [("driven.set", "runner:import", TM)])
        again = self.do("driven.set", session_id=TM, tmux="ka-f0000000")                   # nothing changes
        self.assertEqual(again["tmux"], "ka-f0000000")
        self.assertEqual(len(self.audit(target_type="driven_session")), 1)
        renamed = self.do("driven.set", session_id=TM, tmux="afc-afclaude-task-manager")
        self.assertEqual(renamed["tmux"], "afc-afclaude-task-manager")
        self.assertEqual(self.audit(target_type="driven_session")[-1]["before"], {"tmux": "ka-f0000000"})

    def test_bad_input(self):
        self.project("P")
        for bad in ("afc-Upper", "afc-a b", "afc-a;b", "afc-" + "a" * 37, "ka-1234567", "ka-1234567g", "tmux", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.do("driven.set", session_id=REV, kind="review", tmux=bad)
        self.do("driven.set", session_id=REV, kind="review", tmux="afc-" + "a" * 36)       # 40 characters
        with self.assertRaisesRegex(ValueError, "kind must be one of"):
            self.do("driven.set", session_id=STALL, kind="own", tmux="afc-x")
        with self.assertRaisesRegex(ValueError, "needs kind and tmux"):
            self.do("driven.set", session_id=STALL, kind="stall")
        with self.assertRaisesRegex(ValueError, "full session UUID"):
            self.do("driven.set", session_id="c0000000", kind="stall", tmux="afc-x")
        with self.assertRaisesRegex(ValueError, "needs its project"):
            self.do("driven.set", session_id=STALL, kind="task-manager", tmux="afc-x")
        with self.assertRaises(store.NotFound):
            self.do("driven.set", session_id=STALL, kind="task-manager", tmux="afc-x", project="nope")
        with self.assertRaisesRegex(ValueError, "ISO time"):
            self.do("driven.set", session_id=STALL, kind="stall", tmux="afc-x", ended_at="yesterday")
        self.assertEqual([r["session_id"] for r in store.driven_sessions(self.conn)], [REV])

    def test_a_live_name_is_unique_and_ending_frees_it(self):
        self.do("driven.set", session_id=REV, kind="review", tmux="afc-review")
        with self.assertRaises(actions.Conflict) as cm:
            self.do("driven.set", session_id=STALL, kind="stall", tmux="afc-review")
        self.assertEqual(cm.exception.current["session_id"], REV)
        self.do("driven.set", session_id=REV, ended_at="2026-10-21T10:00:00Z")
        self.assertEqual(self.do("driven.set", session_id=STALL, kind="stall", tmux="afc-review")["tmux"], "afc-review")
        self.assertEqual(store.get_driven_session(self.conn, REV)["ended_at"], "2026-10-21T10:00:00Z")

    def test_one_live_task_manager_per_project(self):
        self.project("AFClaude", TM)
        self.do("driven.set", session_id=TM, kind="task-manager", tmux="ka-f0000000", project="AFClaude")
        with self.assertRaisesRegex(actions.Conflict, "already has the live task-manager"):
            self.do("driven.set", session_id=REV, kind="task-manager", tmux="afc-x", project="AFClaude")
        self.do("driven.set", session_id=TM, ended_at="2026-10-21T10:00:00Z")
        self.do("driven.set", session_id=REV, kind="task-manager", tmux="afc-x", project="AFClaude")

    def test_own_rows_are_not_changed_here_and_mcp_may_not_write(self):
        actions.own_session_add(self.conn, OWN, actor="runner:import", via="runner")
        with self.assertRaisesRegex(ValueError, "kind 'own'"):
            self.do("driven.set", session_id=OWN, tmux="afc-x")
        with self.assertRaises(actions.Forbidden):
            self.do("driven.set", actor="mcp:" + REV, via="mcp", session_id=REV, kind="review", tmux="afc-x")
        self.assertEqual(store.get_driven_session(self.conn, OWN)["kind"], "own")

    def test_stall_rows_stay_the_owners_for_the_autonomous_rule(self):
        self.do("driven.set", session_id=STALL, kind="stall", tmux="afc-stall-x-c000")
        self.do("driven.set", session_id=REV, kind="review", tmux="afc-review")
        ids = actions.autonomous_sessions(self.conn)
        self.assertIn(REV, ids)
        self.assertNotIn(STALL, ids)
        with self.assertRaises(actions.Forbidden):
            self.do("rule.add", actor="mcp:" + REV, via="mcp", scope="project", match="/x", decision="ignore")
        self.do("rule.add", actor="mcp:" + STALL, via="mcp", scope="project", match="/x", decision="ignore")


# ---------------------------------------------------------------- requests (run-now)

class Requests(Base):
    def test_work_on_now_targets_a_project(self):
        p = self.project("AFClaude", TM)
        q = self.project("Claude")                                   # no task-manager yet (D-281): still a target
        r = self.do("request.add", actor="owner", via="dashboard", kind="work_on_now", target="AFClaude")
        self.assertEqual((r["kind"], r["target"], r["status"]), ("work_on_now", str(p["id"]), "open"))
        self.assertEqual(r["updated_at"], r["ts"])
        self.assertEqual(self.do("request.add", kind="work_on_now", target=p["id"])["id"], r["id"])   # by id: the same
        r2 = self.do("request.add", kind="work_on_now", target="Claude")
        self.assertEqual(r2["target"], str(q["id"]))
        with self.assertRaises(store.NotFound):
            self.do("request.add", kind="work_on_now", target="nope")
        with self.assertRaisesRegex(ValueError, "needs a target"):
            self.do("request.add", kind="work_on_now")
        with self.assertRaises(actions.Forbidden):                   # owner-only: not an autonomous session
            self.do("request.add", actor="mcp:" + TM, via="mcp", kind="work_on_now", target="AFClaude")
        self.assertEqual(len(self.audit(target_type="request")), 2)

    def test_states(self):
        self.project("AFClaude", TM)
        a = self.do("request.add", kind="work_on_now", target="AFClaude")
        b = self.do("request.add", kind="review_now")
        s = self.do("request.start", actor="runner:request", via="runner", request_id=a["id"], run_id=12,
                    tmux="ka-f0000000", session_id=TM)
        self.assertEqual((s["status"], s["run_id"], json.loads(s["result"])),
                         ("started", 12, {"tmux": "ka-f0000000", "session_id": TM}))
        self.assertIsNotNone(s["handled_at"])
        with self.assertRaisesRegex(store.InvalidTransition, "not open"):
            self.do("request.start", request_id=a["id"], tmux="ka-f0000000")
        with self.assertRaisesRegex(store.InvalidTransition, "can't become refused"):
            self.do("request.finish", request_id=a["id"], status="refused", reason="running")
        f = self.do("request.finish", actor="runner:request", via="runner", request_id=a["id"], status="failed",
                    reason="no reply in 10 min")
        self.assertEqual((f["status"], f["run_id"], json.loads(f["result"])), ("failed", 12, {"reason": "no reply in 10 min"}))
        rf = self.do("request.finish", request_id=b["id"], status="refused", reason="at its limit until 23:00",
                     run_id=13)
        self.assertEqual((rf["status"], rf["run_id"]), ("refused", 13))
        with self.assertRaisesRegex(store.InvalidTransition, "can't become failed"):
            self.do("request.finish", request_id=b["id"], status="failed", reason="x")
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            self.do("request.finish", request_id=b["id"], status="done", reason="x")
        with self.assertRaises(store.NotFound):
            self.do("request.start", request_id=99, tmux="afc-x")
        with self.assertRaises(ValueError):
            self.do("request.start", request_id=b["id"], tmux="bad name")
        for name, params in (("request.start", {"request_id": a["id"], "tmux": "afc-x"}),
                             ("request.finish", {"request_id": a["id"], "status": "failed", "reason": "x"})):
            with self.assertRaises(actions.Forbidden):
                self.do(name, actor="mcp:" + TM, via="mcp", **params)
        self.assertEqual([x["action"] for x in self.audit(target_type="request")],
                         ["request.add", "request.add", "request.start", "request.finish", "request.finish"])
        # a finished one no longer blocks a new request for the same target
        self.assertNotEqual(self.do("request.add", kind="work_on_now", target="AFClaude")["id"], a["id"])


# ---------------------------------------------------------------- the registry

class TaskManagers(Base):
    def test_every_project_by_rank_with_session_tmux_launch_and_pause(self):
        self.project("A", TM)
        self.project("B")
        self.project("C", REV)
        self.do("project.move", project="C", rank=1)
        self.do("project.manager_launch", project="A", model="claude-sonnet-5-5")
        self.do("automation.set_project", project="A", paused=True, reason="inbox error")
        self.do("driven.set", session_id=TM, kind="task-manager", tmux="ka-f0000000", project="A")
        self.do("task.add", title="t", project="A")
        tm = store.task_managers(self.conn)
        self.assertEqual([(t["name"], t["rank"], t["session"]) for t in tm], [("C", 1, REV), ("A", 2, TM), ("B", 3, None)])
        a = tm[1]
        self.assertEqual((a["tmux"], a["model"], a["paused"], a["pause_reason"]),
                         ("ka-f0000000", "claude-sonnet-5-5", True, "inbox error"))
        self.assertEqual(a["ready"]["high"], 1)
        self.assertEqual((tm[0]["tmux"], tm[0]["paused"], tm[0]["pause_reason"]), (None, False, None))
        self.do("automation.set_project", project="A", paused=False)
        self.assertFalse(store.task_managers(self.conn)[1]["paused"])


# ---------------------------------------------------------------- backfill and check

class FakeTmux:
    def __init__(self, *names):
        self.names = set(names)

    def alive(self, name):
        return name in self.names

    def listed(self):
        return sorted(self.names | {"Claude", "tsmb"})


class Backfill(Base):
    def setUp(self):
        super().setUp()
        self.project("AFClaude", TM)
        self.project("Claude")
        actions.own_session_add(self.conn, OWN, actor="runner:import", via="runner")
        with store.transaction(self.conn):
            store.state_set(self.conn, "usage_review", json.dumps(["runs"]), json.dumps(
                [{"at": "2026-10-01T15:00:00.122733+00:00", "session": REV_DEAD, "rc": 0}]), "test", "runner")
            store.state_set(self.conn, "usage_review", json.dumps(["one_off_runs"]), json.dumps(
                [{"at": "2026-10-16T22:48:03.797728+00:00", "only": "usage", "session": REV, "rc": 0},
                 {"at": "2026-10-17T00:00:00+00:00", "session": "not-a-uuid"}]), "test", "runner")
            for sid, entry in ((STALL, {"kind": "continue", "status": "running", "own_tmux": True,
                                        "tmux": "ka-c0000000", "sent_at": "2026-10-20T10:00:00Z"}),
                               (FOREIGN, {"kind": "continue", "status": "running", "own_tmux": False,
                                          "tmux": "ka-d0000000"})):
                store.state_set(self.conn, "dispatcher", json.dumps(["sessions", sid]), json.dumps(entry), "test",
                                "runner")
        self.tmux = FakeTmux("ka-f0000000", "ka-a0000000", "ka-c0000000", "ka-d0000000")

    def run_backfill(self, **kw):
        return driven.backfill(self.conn, alive=self.tmux.alive, listed=self.tmux.listed, **kw)

    def test_twice_and_the_check(self):
        dry = self.run_backfill(dry_run=True)
        self.assertEqual(len(dry["added"]), 3)
        self.assertEqual(len(store.driven_sessions(self.conn)), 1)                 # wrote nothing
        out = self.run_backfill()
        self.assertEqual({(a["session_id"], a["kind"], a["tmux"]) for a in out["added"]},
                         {(TM, "task-manager", "ka-f0000000"), (REV, "review", "ka-a0000000"),
                          (STALL, "stall", "ka-c0000000")})
        self.assertEqual(out["not_running"], [REV_DEAD])
        self.assertTrue(any("not-a-uuid" in n for n in out["notes"]))
        self.assertIsNone(store.get_driven_session(self.conn, FOREIGN))            # someone else's tmux
        tm = store.get_driven_session(self.conn, TM)
        self.assertEqual(tm["project_id"], store.get_project(self.conn, "AFClaude")["id"])
        self.assertEqual(store.get_driven_session(self.conn, REV)["started_at"], "2026-10-16T22:48:03.797Z")
        self.assertEqual(store.get_driven_session(self.conn, OWN)["kind"], "own")  # untouched
        chk = out["check"]
        # ka-d0000000 (typed into, not started by the dispatcher) runs without a row: the check names it
        self.assertEqual(chk["problems"], ["tmux ka-d0000000 runs without a live driven_sessions row"])
        self.assertTrue(any("Claude: no task-manager session yet" in n for n in chk["notes"]))
        self.tmux.names.discard("ka-d0000000")
        n_audit = len(self.audit())
        again = self.run_backfill()
        self.assertEqual((again["added"], again["ended"]), ([], []))
        self.assertEqual(sorted(again["kept"]), sorted([TM, REV, STALL]))
        self.assertTrue(again["check"]["ok"], again["check"])
        self.assertEqual(len(self.audit()), n_audit)                                # no change the second time
        self.assertEqual(store.task_managers(self.conn)[0]["tmux"], "ka-f0000000")

    def test_a_gone_review_is_ended_and_a_renamed_row_kept(self):
        self.tmux.names.discard("ka-d0000000")
        self.run_backfill()
        self.do("driven.set", session_id=TM, tmux="afc-afclaude-task-manager")       # S2's rename
        self.tmux.names = {"afc-afclaude-task-manager", "ka-c0000000"}               # the review ended
        chk = driven.check(self.conn, alive=self.tmux.alive, listed=self.tmux.listed)
        self.assertEqual(len(chk["problems"]), 1)
        self.assertIn("review row a0000000: tmux ka-a0000000 is gone", chk["problems"][0])
        out = self.run_backfill()
        self.assertEqual(out["ended"], [{"session_id": REV, "kind": "review", "tmux": "ka-a0000000"}])
        self.assertTrue(out["check"]["ok"], out["check"])
        self.assertEqual(store.get_driven_session(self.conn, TM)["tmux"], "afc-afclaude-task-manager")

    def test_check_mismatches_and_notes(self):
        self.tmux.names.discard("ka-d0000000")
        chk = driven.check(self.conn, alive=self.tmux.alive, listed=lambda: None)
        self.assertEqual(chk["problems"], ["project AFClaude: task-manager f0000000: no live task-manager row "
                                           "(python3 store.py import-driven)"])
        self.assertTrue(any("not checked" in n for n in chk["notes"]))
        self.run_backfill()
        self.tmux.names.discard("ka-f0000000")                                       # not running: a note only
        chk = driven.check(self.conn, alive=self.tmux.alive, listed=self.tmux.listed)
        self.assertTrue(chk["ok"], chk)
        self.assertTrue(any("isn't running now" in n for n in chk["notes"]))
        self.do("project.edit", project="AFClaude", manager_session=FOREIGN)          # the registry moved on
        chk = driven.check(self.conn, alive=self.tmux.alive, listed=self.tmux.listed)
        self.assertFalse(chk["ok"])
        self.assertEqual(len(chk["problems"]), 2)
        self.assertTrue(any("live task-manager row(s) are f0000000" in p for p in chk["problems"]))

    def test_a_failing_tmux_call_stops_it(self):
        bad = mock.Mock(returncode=255, stderr="ssh: connect refused")
        with mock.patch("host.run_on_host", return_value=bad):
            with self.assertRaisesRegex(RuntimeError, "rc 255"):
                driven.backfill(self.conn, listed=lambda: [])
        self.assertEqual(len(store.driven_sessions(self.conn)), 2)    # the own row + the task-manager (no tmux call)
        for rc, want in ((0, True), (1, False)):
            with mock.patch("host.run_on_host", return_value=mock.Mock(returncode=rc, stderr="")):
                self.assertIs(driven.tmux_alive("ka-f0000000"), want)

    def test_cli(self):
        self.tmux.names.discard("ka-d0000000")
        with mock.patch.object(driven, "tmux_alive", self.tmux.alive), \
                mock.patch.object(driven, "tmux_list", self.tmux.listed):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(store.main(["import-driven", "--db", self.db, "--dry-run"]), 0)
            self.assertEqual(len(json.loads(out.getvalue())["added"]), 3)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tasks.main(["--db", self.db, "driven", "check"]), 1)
                self.assertEqual(store.main(["import-driven", "--db", self.db]), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tasks.main(["--db", self.db, "driven", "check"]), 0)
                self.assertEqual(tasks.main(["--db", self.db, "driven", "list", "--json"]), 0)
            text = out.getvalue()
            self.assertIn("driven check: OK", text)
            rows = json.loads(text[text.index("["):])
            self.assertEqual({(r["kind"], r["project"]) for r in rows},
                             {("task-manager", "AFClaude"), ("review", None), ("stall", None), ("own", None)})


if __name__ == "__main__":
    unittest.main()
