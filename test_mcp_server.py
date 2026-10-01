#!/usr/bin/env python3
"""Tests for mcp_server.py: the tool functions called directly (temp DBs), and a
real stdio round trip through the MCP SDK's client (server spawned as a
subprocess on a temp DB via AFCLAUDE_DB).

Needs the project venv (the SDK wants Python >= 3.10):
    .venv/bin/python -m unittest test_mcp_server
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types as pytypes
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcp_server as ms  # noqa: E402
import store  # noqa: E402
from mcp import types  # noqa: E402
from mcp.client import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

SID = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
CALLER = "11111111-2222-4333-8444-555555555555"
EXPECTED_TOOLS = {"afclaude_add_task", "afclaude_list_tasks", "afclaude_get_task", "afclaude_update_task",
                  "afclaude_answer_task", "afclaude_project", "afclaude_inbox", "afclaude_decide_session",
                  "afclaude_rule", "afclaude_ask"}


def run(coro):
    return json.loads(asyncio.run(coro))


class Env(unittest.TestCase):
    """Temp DB, empty transcripts root, a caller cwd, a fake session id."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.db = os.path.join(t, "db", "afclaude.db")
        self.projects_dir = os.path.join(t, "transcripts")
        self.work = os.path.join(t, "work", "Proj")
        os.makedirs(self.projects_dir)
        os.makedirs(os.path.join(self.work, "sub"))
        self._env = dict(os.environ)
        self._cwd = os.getcwd()
        os.environ.update(AFCLAUDE_DB=self.db, AFCLAUDE_PROJECTS_DIR=self.projects_dir,
                          CLAUDE_CODE_SESSION_ID=CALLER)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        os.environ.pop("CLAUDE_GUARD_DISABLE", None)   # the tests act as the owner's session
        os.chdir(self.work)

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def conn(self):
        return store.connect(self.db)


class Tasks(Env):
    def test_mcp_task_is_like_a_ui_task(self):
        """No approval step: an MCP-added task is pending and in the run queue exactly like
        one added through the CLI (tasks.py, the UI until the dashboard exists)."""
        m = run(ms.afclaude_add_task("via mcp", project="P"))
        conn = self.conn()
        try:
            c = store.add_task(conn, "via cli", None, "P", "high")
            rows = {t["id"]: t for t in store.list_tasks(conn, status="pending")}
        finally:
            conn.close()
        self.assertEqual(m["status"], "pending")
        self.assertEqual(set(rows), {m["id"], c["id"]})           # both in the run queue
        for k in ("status", "kind", "priority"):
            self.assertEqual(rows[m["id"]][k], rows[c["id"]][k], k)
        self.assertFalse(any("approv" in k for k in rows[m["id"]]))

    def test_add_defaults_to_callers_cwd(self):
        t = run(ms.afclaude_add_task("Write the dispatcher"))
        self.assertEqual((t["project"], t["project_source"], t["priority"], t["stage"], t["status"]),
                         ("Proj", "cwd", "high", 1, "pending"))
        os.chdir(os.path.join(self.work, "sub"))                  # a subdir maps to the same project
        t2 = run(ms.afclaude_add_task("second", description="d", priority="low"))
        self.assertEqual((t2["project"], t2["stage"]), ("Proj", 2))
        c = self.conn()
        try:
            full = store.get_task(c, t["id"])
            self.assertEqual(full["created_by_session"], CALLER)
            self.assertEqual(store.get_project(c, "Proj")["path"], self.work)
        finally:
            c.close()
        t3 = run(ms.afclaude_add_task("named", project="Other"))
        self.assertEqual((t3["project"], t3["project_source"]), ("Other", "explicit"))
        t4 = run(ms.afclaude_add_task("dot", project="."))            # '.' = the caller's dir
        self.assertEqual(t4["project"], "Proj")

    def test_no_project_in_home_or_root(self):
        os.chdir(os.path.expanduser("~"))
        t = run(ms.afclaude_add_task("loose"))
        self.assertEqual(t["project_source"], "none")
        self.assertNotIn("project", t)
        os.environ["CLAUDE_PROJECT_DIR"] = self.work                  # env beats cwd
        t = run(ms.afclaude_add_task("from env"))
        self.assertEqual((t["project"], t["project_source"]), ("Proj", "env"))

    def test_list_get_update_answer(self):
        a = run(ms.afclaude_add_task("A1", project="A"))
        b = run(ms.afclaude_add_task("B1", project="B"))
        run(ms.afclaude_add_task("A2", project="A", priority="medium"))
        run(ms.afclaude_add_task("here"))                              # project Proj (rank 3)
        r = run(ms.afclaude_list_tasks())
        self.assertEqual(r["total"], 4)
        self.assertEqual([t["title"] for t in r["tasks"]], ["A1", "B1", "here", "A2"])
        self.assertEqual([t["title"] for t in run(ms.afclaude_list_tasks(limit=2))["tasks"]], ["A1", "B1"])
        self.assertEqual([t["title"] for t in run(ms.afclaude_list_tasks(project="."))["tasks"]], ["here"])
        self.assertEqual([t["title"] for t in run(ms.afclaude_list_tasks(project="A"))["tasks"]], ["A1", "A2"])
        # update: fields + stage + status in one call
        u = run(ms.afclaude_update_task(a["id"], title="A1 renamed", priority="low", stage=2))
        self.assertEqual((u["title"], u["priority"], u["stage"]), ("A1 renamed", "low", 2))
        u = run(ms.afclaude_update_task(b["id"], status="blocked", note="Which port?"))
        self.assertEqual((u["status"], u["question"]), ("blocked", "Which port?"))
        self.assertEqual([t["title"] for t in run(ms.afclaude_list_tasks(status="ready"))["tasks"]],
                         ["here", "A2", "A1 renamed"])
        with self.assertRaisesRegex(ToolError, "answer it with afclaude_answer_task"):
            asyncio.run(ms.afclaude_update_task(b["id"], status="pending"))
        ans = run(ms.afclaude_answer_task(b["id"], "8765"))
        self.assertEqual((ans["status"], ans["question"], ans["answer"]), ("pending", "Which port?", "8765"))
        u = run(ms.afclaude_update_task(b["id"], status="in_progress"))
        self.assertEqual(u["status"], "in_progress")
        u = run(ms.afclaude_update_task(b["id"], status="done", note="shipped"))
        self.assertEqual(u["status"], "done")
        self.assertEqual(run(ms.afclaude_list_tasks(status="done"))["total"], 1)
        self.assertEqual(run(ms.afclaude_list_tasks(status="all"))["total"], 4)
        g = run(ms.afclaude_get_task(b["id"]))
        self.assertEqual([e["event"] for e in g["history"]], ["created", "blocked", "answered", "started", "done"])
        self.assertEqual((g["result"], g["answer"], g["assigned_session"]), ("shipped", "8765", CALLER))
        self.assertRegex(g["created"], r"^\d{4}-\d\d-\d\d \d\d:\d\d CES?T$")       # Berlin time
        u = run(ms.afclaude_update_task(b["id"], status="pending", note="again"))      # reopen
        self.assertEqual(u["status"], "pending")
        u = run(ms.afclaude_update_task(b["id"], project="A"))
        self.assertEqual((u["project"], u["stage"]), ("A", 3))

    def test_update_is_atomic_and_errors_are_clean(self):
        t = run(ms.afclaude_add_task("x", project="A"))
        with self.assertRaisesRegex(ToolError, "invalid transition: .*reopen needs"):
            asyncio.run(ms.afclaude_update_task(t["id"], title="changed", stage=1, status="pending"))
        g = run(ms.afclaude_get_task(t["id"]))
        self.assertEqual((g["title"], [e["event"] for e in g["history"]]), ("x", ["created"]))   # rolled back
        cases = [
            (ms.afclaude_get_task(999), "no task #999"),
            (ms.afclaude_answer_task(t["id"], "a"), "answer needs blocked"),
            (ms.afclaude_add_task("  "), "title must not be empty"),
            (ms.afclaude_add_task("x", priority="urgent"), "priority must be one of"),
            (ms.afclaude_update_task(t["id"]), "nothing to change"),
            (ms.afclaude_update_task(t["id"], status="blocked"), "question is required"),
            (ms.afclaude_list_tasks(project="nope"), "no project 'nope'"),
            (ms.afclaude_list_tasks(status="maybe"), "status must be one of"),
            (ms.afclaude_decide_session("zzzz", "continue"), "no session matches"),
        ]
        for coro, msg in cases:
            with self.assertRaises(ToolError, msg=msg) as cm:
                asyncio.run(coro)
            self.assertIn(msg, str(cm.exception))
            self.assertNotIn("Traceback", str(cm.exception))


class Projects(Env):
    def test_actions(self):
        for p, title in (("A", "a1"), ("B", "b1"), ("C", "c1")):
            run(ms.afclaude_add_task(title, project=p))
        r = run(ms.afclaude_project("add", name="Top", rank=1, description="first"))
        self.assertEqual((r["name"], r["rank"]), ("Top", 1))
        r = run(ms.afclaude_project("move", name="C", rank=2))
        self.assertEqual(r["rank"], 2)
        r = run(ms.afclaude_project("prio", name="A", priority="medium"))
        self.assertEqual((r["project"], r["priority"], len(r["changed_tasks"])), ("A", "medium", 1))
        lst = run(ms.afclaude_project("list"))["projects"]
        self.assertEqual([(p["rank"], p["name"]) for p in lst], [(1, "Top"), (2, "C"), (3, "A"), (4, "B")])
        self.assertEqual(lst[2]["ready"], {"high": 0, "medium": 1, "low": 0})
        self.assertEqual([t["title"] for t in run(ms.afclaude_list_tasks(status="ready"))["tasks"]],
                         ["c1", "b1", "a1"])
        r = run(ms.afclaude_project("edit", name="B", new_name="Bee", path="."))
        self.assertEqual((r["name"], r["path"]), ("Bee", self.work))
        r = run(ms.afclaude_project("edit", name=".", description="the caller's"))   # '.' = caller's project
        self.assertEqual((r["name"], r["description"]), ("Bee", "the caller's"))
        mgr = "f0000000-0000-4000-8000-000000000001"
        r = run(ms.afclaude_project("edit", name="A", manager_session=mgr))       # full uuid, not scanned yet
        self.assertEqual(r["manager_session"], mgr)
        lst = run(ms.afclaude_project("list"))["projects"]
        self.assertEqual([p.get("manager_session") for p in lst if p["name"] == "A"], [mgr])
        r = run(ms.afclaude_project("edit", name="A", manager_session=""))        # '' = unmanaged
        self.assertNotIn("manager_session", r)
        with self.assertRaisesRegex(ToolError, "no session matches"):
            asyncio.run(ms.afclaude_project("edit", name="A", manager_session="zzzz"))
        for coro, msg in ((ms.afclaude_project("move", name="A"), "move needs rank"),
                          (ms.afclaude_project("prio", name="A"), "prio needs priority"),
                          (ms.afclaude_project("add"), "add needs name"),
                          (ms.afclaude_project("add", name="A"), "already exists"),
                          (ms.afclaude_project("move", name="Z", rank=1), "no project 'Z'")):
            with self.assertRaisesRegex(ToolError, msg):
                asyncio.run(coro)


class Sessions(Env):
    def stall(self, sid, cwd):
        c = self.conn()
        try:
            store.upsert_session(c, sid, cwd=cwd, project_dir="-x", stalled=1, title="long build",
                                 stalled_since="2026-09-29T09:00:00.000Z", stall_kind="session",
                                 stall_reset_at="2026-09-29T12:00:00Z", stall_uuid="n1", stall_text="hit")
            c.commit()
        finally:
            c.close()

    def test_inbox_decide_rules(self):
        # fixed paths: temp dirs may contain "AFClaude", which marks a session as AFClaude's own
        os.environ["CLAUDE_PROJECT_DIR"] = "/home/x/proj"
        box = run(ms.afclaude_inbox())
        self.assertEqual(box, {"blocked_tasks": [], "undecided_sessions": []})
        t = run(ms.afclaude_add_task("needs input"))
        run(ms.afclaude_update_task(t["id"], status="blocked", note="Which DB?"))
        self.stall(SID, "/home/x/elsewhere")
        self.stall(SID2, "/home/x/proj/sub")
        box = run(ms.afclaude_inbox())
        self.assertEqual([(b["id"], b["question"]) for b in box["blocked_tasks"]], [(t["id"], "Which DB?")])
        us = {s["session_id"]: s for s in box["undecided_sessions"]}
        self.assertEqual(set(us), {SID, SID2})
        self.assertEqual((us[SID]["resets"], us[SID]["limit"]), ("2026-09-29 14:00 CEST", "session"))
        self.assertNotIn("scan_error", box)
        with self.assertRaisesRegex(ToolError, "2 sessions match"):
            asyncio.run(ms.afclaude_decide_session("00000000", "continue"))
        d = run(ms.afclaude_decide_session(SID[:34] + "01", "continue", note="yes"))
        self.assertEqual(d, {"session_id": SID, "decision": "continue", "source": "session"})
        r = run(ms.afclaude_rule("add", scope="project", match=".", decision="ignore"))   # caller's dir
        self.assertEqual((r["scope"], r["match"], r["decision"]), ("project", "/home/x/proj", "ignore"))
        box = run(ms.afclaude_inbox())
        self.assertEqual(box["undecided_sessions"], [])
        self.assertEqual(len(box["blocked_tasks"]), 1)
        rs = run(ms.afclaude_rule("add", scope="session", match=SID2[:36], decision="continue"))
        self.assertEqual(rs["match"], SID2)
        self.assertEqual(len(run(ms.afclaude_rule("list"))["rules"]), 2)
        self.assertEqual(run(ms.afclaude_rule("rm", id=r["id"])), {"removed": r["id"]})
        d = run(ms.afclaude_decide_session(SID, "clear"))
        self.assertEqual(d["decision"], "undecided")
        for coro, msg in ((ms.afclaude_rule("rm"), "rm needs id"), (ms.afclaude_rule("add"), "add needs"),
                          (ms.afclaude_rule("rm", id=999), "no rule #999")):
            with self.assertRaisesRegex(ToolError, msg):
                asyncio.run(coro)

    def test_scan_error_is_reported_not_raised(self):
        os.environ["AFCLAUDE_PROJECTS_DIR"] = os.path.join(self.tmp.name, "missing", "dir")
        orig = ms._scan
        ms._scan = lambda: "OSError: boom"
        try:
            box = run(ms.afclaude_inbox())
        finally:
            ms._scan = orig
        self.assertEqual(box["scan_error"], "OSError: boom")


class CallerDir(Env):
    def ctx(self, roots=None, fail=False, caps=True):
        async def list_roots():
            if fail:
                raise RuntimeError("no back-channel")
            return types.ListRootsResult(roots=[types.Root(uri=u) for u in roots])
        return pytypes.SimpleNamespace(
            client_capabilities=types.ClientCapabilities(roots=types.RootsCapability() if caps else None),
            session=pytypes.SimpleNamespace(list_roots=list_roots))

    def test_sources(self):
        d = lambda c: asyncio.run(ms.caller_dir(c))  # noqa: E731
        self.assertEqual(d(self.ctx(["file:///srv/My%20Repo/"])), ("/srv/My Repo", "roots"))
        self.assertEqual(d(self.ctx(["file:///srv/a", "file:///srv/b"])), ("/srv/a", "roots"))   # first root
        self.assertEqual(d(self.ctx(fail=True)), (self.work, "cwd"))           # falls back
        self.assertEqual(d(self.ctx(["file:///srv/b"], caps=False)), (self.work, "cwd"))
        self.assertEqual(d(None), (self.work, "cwd"))
        self.assertEqual(d(self.ctx(["file:///"])), (None, "roots"))
        t = run(ms.afclaude_add_task("via roots", ctx=self.ctx(["file:///srv/RootsProj"])))
        self.assertEqual((t["project"], t["project_source"]), ("RootsProj", "roots"))


class Stdio(unittest.TestCase):
    """Spawn the server like Claude Code does (stdio, initialize handshake) on a temp DB."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.db = os.path.join(t, "afclaude.db")
        self.work = os.path.join(t, "StdioProj")
        os.makedirs(self.work)
        os.makedirs(os.path.join(t, "transcripts"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AFCLAUDE"))}
        env.update(AFCLAUDE_DB=self.db, AFCLAUDE_PROJECTS_DIR=os.path.join(t, "transcripts"),
                   CLAUDE_CODE_SESSION_ID=CALLER)
        self.params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "mcp_server.py")],
                                            env=env, cwd=self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        async def go():
            t0 = time.monotonic()
            async with Client(self.params, mode="legacy", read_timeout_seconds=30) as client:
                started = time.monotonic() - t0
                self.instructions = client.instructions
                tools = (await client.list_tools()).tools
                call = lambda n, a=None: client.call_tool(n, a or {})  # noqa: E731
                added = await call("afclaude_add_task", {"title": "via stdio", "description": "rt"})
                listed = await call("afclaude_list_tasks", {})
                inbox = await call("afclaude_inbox")
                bad = await call("afclaude_get_task", {"id": 999})
                badarg = await call("afclaude_add_task", {"title": "x", "priority": "urgent"})
                return started, tools, added, listed, inbox, bad, badarg
        started, tools, added, listed, inbox, bad, badarg = asyncio.run(go())
        self.assertLess(started, 15)
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)
        # owner decision Q3: only on the user's explicit request (no approval step behind it)
        self.assertEqual(self.instructions, ms.INSTRUCTIONS)
        self.assertIn("ONLY when the user explicitly asks for AFClaude", self.instructions)
        self.assertIn("no approval step", self.instructions)
        self.assertLess(len(ms.USE_ONLY), 100)                      # the per-tool prefix stays short
        for t in tools:
            self.assertTrue(t.description)
            self.assertTrue(t.description.startswith(ms.USE_ONLY), t.name)
            self.assertIsNone(t.output_schema)                       # plain text results, no schema cost
            self.assertNotIn("ctx", t.input_schema.get("properties", {}))
        schema = {t.name: t.input_schema for t in tools}
        self.assertEqual(schema["afclaude_add_task"]["required"], ["title"])
        size = sum(len(json.dumps(t.model_dump(by_alias=True, exclude_none=True))) for t in tools)
        self.assertLess(size, 12000, f"tool list is {size} bytes")
        a = json.loads(added.content[0].text)
        self.assertFalse(added.is_error)
        self.assertEqual((a["project"], a["project_source"], a["title"]), ("StdioProj", "cwd", "via stdio"))
        lst = json.loads(listed.content[0].text)
        self.assertEqual([t["id"] for t in lst["tasks"]], [a["id"]])
        self.assertEqual(json.loads(inbox.content[0].text), {"blocked_tasks": [], "undecided_sessions": []})
        self.assertTrue(bad.is_error)
        self.assertTrue(bad.content[0].text.endswith("no task #999"), bad.content[0].text)  # SDK adds a prefix
        self.assertNotIn("Traceback", bad.content[0].text)
        self.assertTrue(badarg.is_error)                                # rejected by the input schema
        c = store.connect(self.db)
        try:
            self.assertEqual(store.get_task(c, a["id"])["created_by_session"], CALLER)
        finally:
            c.close()

    def test_roots_win_over_cwd(self):
        root = os.path.join(self.tmp.name, "RootsProj")

        async def roots(_ctx):
            return types.ListRootsResult(roots=[types.Root(uri="file://" + root)])

        async def go():
            async with Client(self.params, mode="legacy", list_roots_callback=roots,
                              read_timeout_seconds=30) as client:
                r = await client.call_tool("afclaude_add_task", {"title": "rooted"})
                return json.loads(r.content[0].text)
        a = asyncio.run(go())
        self.assertEqual((a["project"], a["project_source"]), ("RootsProj", "roots"))


if __name__ == "__main__":
    unittest.main()
