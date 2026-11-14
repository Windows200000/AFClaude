#!/usr/bin/env python3
"""Tests for phase 5b K5 part 1 (docs/phase5b_plan.md §3.3, §5 K5): the MCP server's HTTP transport with
machine-token identity (mcp_http.py, mcp_server.py serve), over a real uvicorn server on a loopback port and
the SDK's streamable HTTP client, plus the stdio form with a token (the container's `docker exec -i -e ...
afclaude mcp stdio`, as a subprocess with the identity env). No token, a wrong, a malformed and a revoked one,
a missing host name header: refused (401) and audited; a valid token: the tools are listed and the actor is
`machine:<id>`; the two-hostname alert (D-198) through the transport; a remote caller's stamp never names an
AFClaude session and the server's own env never counts; a session AFClaude drives still resolves as
task-manager / agent whatever token its env carries (D-198, D-330, D-336).

Needs the project venv (the SDK wants Python >= 3.10):
    .venv/bin/python -m unittest test_mcp_http
"""
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (before any AFClaude module: runner_state.guard_tests)
import actions  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402

SKIP = "" if importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("uvicorn") is not None else (
    f"the MCP SDK / uvicorn is not installed for {sys.executable}: run with .venv/bin/python -m unittest test_mcp_http")
if not SKIP:
    import httpx2  # noqa: E402
    import uvicorn  # noqa: E402
    import mcp_http  # noqa: E402
    import mcp_server as ms  # noqa: E402
    from mcp.client import Client  # noqa: E402
    from mcp.client.stdio import StdioServerParameters  # noqa: E402
    from mcp.client.streamable_http import streamable_http_client  # noqa: E402
    from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

TM = "aaaaaaaa-0000-4000-8000-00000000000a"        # a project's task-manager
DRV = "dddddddd-0000-4000-8000-00000000000d"       # a session AFClaude drives
OWN = "eeeeeeee-0000-4000-8000-00000000000e"       # an interactive session
NEW = "ffffffff-0000-4000-8000-00000000000f"       # an id the server has never seen
IDENTITY = {"AFCLAUDE_MACHINE_TOKEN", "AFCLAUDE_MACHINE_HOST", "AFCLAUDE_MACHINE_FILE", "AFCLAUDE_AGENT_ID"}


class Base(unittest.TestCase):
    """A temp DB with one registered machine user (issued for host-a), a project with a task-manager and a driven
    session."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.db = os.path.join(t, "afclaude.db")
        self.conn = store.connect(self.db, create=True)
        actions.perform(self.conn, "project.add", {"name": "P"}, actor="cli", via="cli")
        store.update_project(self.conn, "P", manager_session=TM)
        store.upsert_driven_session(self.conn, DRV, kind="review", started_at=store.now_iso())
        self.conn.commit()
        code = roles.new_registration_code()
        actions.perform(self.conn, "machine.code", {"code_sha256": roles.code_sha256(code)}, actor="cli", via="cli")
        r = actions.register_machine(self.conn, code, "host-a", actor="cli", via="cli")
        self.user_id, self.token, self.token_id = r["user_id"], r["token"], r["token_id"]
        config = os.path.join(t, "afclaude.json")
        with open(config, "w") as fh:
            json.dump({"manager_session": TM}, fh)
        self._env = dict(os.environ)
        os.environ.update(AFCLAUDE_DB=self.db, AFCLAUDE_PROJECTS_DIR=os.path.join(t, "transcripts"),
                          AFCLAUDE_CONFIG=config)
        os.makedirs(os.environ["AFCLAUDE_PROJECTS_DIR"])

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def audit(self, action):
        return [r for r in store.audit_log(self.conn, limit=100) if r["action"] == action]


@unittest.skipIf(bool(SKIP), SKIP)
class Http(Base):
    """The streamable HTTP app under a real uvicorn server on an ephemeral loopback port."""
    def setUp(self):
        super().setUp()
        self.notified = []
        patch = mock.patch("notify.notify", side_effect=lambda subject, body: self.notified.append((subject, body)))
        patch.start()
        self.addCleanup(patch.stop)
        app = mcp_http.build_app(ms.server, "127.0.0.1")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve([sock])), daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        self.assertTrue(self.server.started, "the HTTP server did not start")
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(10)
        super().tearDown()

    def headers(self, token="", host="host-a"):
        h = {}
        if token is not None:
            h["Authorization"] = f"Bearer {token or self.token}"
        if host:
            h["X-AFClaude-Hostname"] = host
        return h

    def client(self, **kw):
        http = create_mcp_http_client(headers=self.headers(**kw))
        return Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=30)

    def status(self, **kw):
        """The HTTP status of a bare POST with these headers."""
        r = httpx2.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=10,
                        headers={"Accept": "application/json, text/event-stream", **self.headers(**kw)})
        return r

    def call(self, name, args, **kw):
        async def go():
            async with self.client(**kw) as c:
                return json.loads((await c.call_tool(name, args)).content[0].text)
        return asyncio.run(go())

    def task_row(self, task):
        return next(r for r in self.audit("task.add") if r["target_id"] == str(task["id"]))

    # ------------------------------------------------------------ refused

    def test_no_token_is_refused_with_the_bearer_challenge(self):
        r = self.status(token=None)
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.headers["www-authenticate"].startswith("Bearer"))

    def test_a_wrong_or_malformed_token_is_refused_and_audited(self):
        wrong = self.token[:-3] + ("aaa" if not self.token.endswith("aaa") else "bbb")
        for bad in (wrong, "not-a-token", f"{self.token}x"):
            self.assertEqual(self.status(token=bad).status_code, 401, bad)
        rows = self.audit("machine.token_refused")
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual((r["actor"], r["via"]), ("mcp", "mcp"))
            self.assertNotIn(self.token, json.dumps(r))
            self.assertNotIn("not-a-token", json.dumps(r))
        self.assertLessEqual({r["after"]["refused"] for r in rows}, {"malformed", "unknown or wrong secret"})
        self.assertEqual(self.audit("machine.seen"), [])             # a refusal records no use

    def test_a_revoked_token_and_a_revoked_machine_are_refused(self):
        self.call("afclaude_list_tasks", {})                         # valid before
        actions.perform(self.conn, "machine.revoke", {"user_id": self.user_id}, actor="cli", via="cli")
        self.assertEqual(self.status().status_code, 401)
        rows = self.audit("machine.token_refused")
        self.assertEqual([r["after"]["refused"] for r in rows], ["token revoked"])
        self.assertEqual(rows[0]["target_id"], str(self.token_id))

    def test_an_unauthenticated_client_cannot_make_new_audit_rows_with_new_ids_or_hosts(self):
        for i in range(5):
            self.assertEqual(self.status(token="garbage", host=f"host-{i}").status_code, 401)
        self.assertEqual(len(self.audit("machine.token_refused")), 1)

    def test_a_bind_beyond_loopback_needs_roles_on_and_a_bad_public_url_does_not_stop_stdio(self):
        with self.assertRaises(mcp_http.Refused) as cm:
            mcp_http.check_bind("0.0.0.0", "shadow")
        self.assertIn("roles_enforce", str(cm.exception))
        mcp_http.check_bind("0.0.0.0", "on")
        for host in ("127.0.0.1", "localhost", "::1"):
            mcp_http.check_bind(host, "shadow")
        with self.assertRaises(mcp_http.Refused):
            mcp_http.serve(ms.server, "0.0.0.0")                     # the live check reads the DB's setting: shadow
        env = dict(os.environ, AFCLAUDE_PUBLIC_URL="not a url")
        r = subprocess.run([sys.executable, "-c", "import mcp_server"], cwd=HERE, env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])

    def test_remote_machine_with_no_request_context_is_no_machine(self):
        class NoContext:
            @property
            def request_context(self):
                raise ValueError("Context is not available outside of a request")
        self.assertIsNone(mcp_http.remote_machine(NoContext()))
        self.assertIsNone(mcp_http.remote_machine(None))

    def test_a_container_side_token_is_refused_for_stdio(self):
        env = {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_MACHINE_TOKEN": self.token}
        with self.assertRaises(mcp_http.Refused):
            mcp_http.check_stdio_env(env)
        mcp_http.check_stdio_env({**env, "AFCLAUDE_MACHINE_HOST": "h"})        # the wrapper's call
        mcp_http.check_stdio_env({"AFCLAUDE_MACHINE_TOKEN": self.token})       # on the host, as before

    def test_a_missing_or_bad_host_name_header_is_refused(self):
        self.assertEqual(self.status(host=None).status_code, 401)
        self.assertEqual(self.status(host="bad host name").status_code, 401)
        self.assertEqual({r["after"]["refused"] for r in self.audit("machine.token_refused")},
                         {"no host name given", "bad host name"})
        self.assertEqual(self.audit("machine.seen"), [])

    # ------------------------------------------------------------ valid

    def test_a_valid_token_lists_the_tools_and_writes_as_the_machine_user(self):
        async def go():
            async with self.client() as c:
                tools = (await c.list_tools()).tools
                added = json.loads((await c.call_tool("afclaude_add_task", {"title": "remote", "project": "P"})).content[0].text)
                listed = json.loads((await c.call_tool("afclaude_list_tasks", {})).content[0].text)
                return tools, added, listed
        tools, added, listed = asyncio.run(go())
        from test_mcp_server import EXPECTED_TOOLS
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)
        row = self.task_row(added)
        self.assertEqual((row["actor"], row["via"], row["session"]), (f"machine:{self.user_id}", "mcp", None))
        self.assertIn("remote", json.dumps(listed))
        seen = store.get_machine_token(self.conn, self.token_id)
        self.assertEqual((seen["last_hostname"], seen["last_transport"]), ("host-a", "http"))
        self.assertEqual(self.notified, [])

    def test_the_stamp_gives_the_session_but_never_an_afclaude_session(self):
        t = self.call("afclaude_add_task", {"title": "s1", "project": "P", "_afc": {"session": NEW}})
        self.assertEqual((self.task_row(t)["actor"], self.task_row(t)["session"]), (f"machine:{self.user_id}", NEW))
        # a stamp naming the task-manager's or a driven session: ignored (shadow mode), the caller stays the machine
        for sid in (TM, DRV):
            t = self.call("afclaude_add_task", {"title": "s2", "project": "P", "_afc": {"session": sid}})
            row = self.task_row(t)
            self.assertEqual((row["actor"], row["session"]), (f"machine:{self.user_id}", None), sid)

    def test_a_stamp_naming_an_afclaude_session_is_refused_once_roles_are_enforced(self):
        with mock.patch("identity.ENFORCE_CONFLICTS", True):
            async def go():
                async with self.client() as c:
                    return await c.call_tool("afclaude_add_task", {"title": "x", "project": "P", "_afc": {"session": TM}})
            res = asyncio.run(go())
        self.assertTrue(res.is_error)
        self.assertEqual(self.audit("task.add"), [])

    def test_the_server_processs_own_env_never_counts_for_a_remote_caller(self):
        os.environ.update(CLAUDE_GUARD_DISABLE="1", CLAUDE_CODE_SESSION_ID=TM, CLAUDECODE="1",
                          AFCLAUDE_MACHINE_TOKEN="garbage")
        t = self.call("afclaude_add_task", {"title": "env", "project": "P"})
        row = self.task_row(t)
        self.assertEqual((row["actor"], row["session"]), (f"machine:{self.user_id}", None))

    def test_the_server_has_no_project_directory_for_a_remote_caller(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        t = self.call("afclaude_add_task", {"title": "no project"})
        self.assertEqual(t["project_source"], "none")                # neither $CLAUDE_PROJECT_DIR nor the cwd of this process

    # ------------------------------------------------------------ two hostnames

    def test_one_token_from_two_hostnames_alerts_once(self):
        self.call("afclaude_list_tasks", {})                         # from host-a, where it was issued
        self.assertEqual(self.notified, [])
        self.call("afclaude_list_tasks", {}, host="host-b")
        self.assertEqual(len(self.notified), 1)
        subject, body = self.notified[0]
        self.assertIn("second host", subject)
        self.assertIn("host-a", body)
        self.assertIn("host-b", body)
        self.assertNotIn(self.token, body)
        self.call("afclaude_list_tasks", {}, host="host-a")          # back and forth within the day: no second alert
        self.call("afclaude_list_tasks", {}, host="host-b")
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(len([r for r in self.audit("machine.seen")]), 1)


@unittest.skipIf(bool(SKIP), SKIP)
class Stdio(Base):
    """`docker exec -i -e AFCLAUDE_MACHINE_TOKEN -e CLAUDE_CODE_SESSION_ID ... afclaude mcp stdio`: the same
    server process as a subprocess with the wrapper's identity env."""
    def params(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k not in IDENTITY}
        # the host name the token was issued for: a clone alert would run notify.py in the subprocess (a real push)
        base.update(AFCLAUDE_MACHINE_FILE=os.path.join(self.tmp.name, "none.json"), AFCLAUDE_MACHINE_HOST="host-a")
        base.update(env)
        return StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "mcp_server.py"), "stdio"],
                                     env=base, cwd=self.tmp.name)

    def add(self, **env):
        async def go():
            async with Client(self.params(**env), mode="legacy", read_timeout_seconds=30) as c:
                tools = (await c.list_tools()).tools
                t = json.loads((await c.call_tool("afclaude_add_task", {"title": "stdio", "project": "P"})).content[0].text)
                return tools, t
        tools, t = asyncio.run(go())
        self.assertTrue(tools)
        return next(r for r in self.audit("task.add") if r["target_id"] == str(t["id"]))

    def test_a_session_with_a_token_acts_as_the_machine_user_with_its_host_name(self):
        row = self.add(AFCLAUDE_MACHINE_TOKEN=self.token, CLAUDE_CODE_SESSION_ID=OWN, CLAUDECODE="1",
                       AFCLAUDE_MACHINE_HOST="Host-A")      # same host (case-folded): no clone alert, which would push for real
        self.assertEqual((row["actor"], row["session"]), (f"machine:{self.user_id}", OWN))
        seen = store.get_machine_token(self.conn, self.token_id)
        self.assertEqual((seen["last_hostname"], seen["last_transport"]), ("Host-A", "stdio"))

    def test_a_revoked_token_is_the_host_owner_not_the_machine(self):
        actions.perform(self.conn, "machine.revoke", {"user_id": self.user_id}, actor="cli", via="cli")
        row = self.add(AFCLAUDE_MACHINE_TOKEN=self.token, CLAUDE_CODE_SESSION_ID=OWN, CLAUDECODE="1")
        self.assertEqual((row["actor"], row["session"]), ("cli", OWN))      # D-330

    def test_a_session_afclaude_started_stays_task_manager_or_agent_whatever_token_it_carries(self):
        """D-198: the session id the wrapper passes (CLAUDE_CODE_SESSION_ID, which Claude Code gives its MCP
        children, plan §3.3 K0 finding) decides, not the token."""
        tm = self.add(AFCLAUDE_MACHINE_TOKEN=self.token, CLAUDE_CODE_SESSION_ID=TM, CLAUDECODE="1")
        drv = self.add(AFCLAUDE_MACHINE_TOKEN=self.token, CLAUDE_CODE_SESSION_ID=DRV, CLAUDECODE="1")
        guard = self.add(AFCLAUDE_MACHINE_TOKEN=self.token, CLAUDE_CODE_SESSION_ID=NEW, CLAUDE_GUARD_DISABLE="1")
        self.assertEqual((tm["actor"], tm["session"]), (f"task-manager:{TM}", TM))
        self.assertEqual((drv["actor"], drv["session"]), (f"agent:{DRV}", DRV))
        self.assertEqual((guard["actor"], guard["session"]), (f"agent:{NEW}", NEW))


@unittest.skipIf(bool(SKIP), SKIP)
class Wrapper(Base):
    """The whole stdio path of K5: Claude Code starts the host wrapper (`afclaude mcp stdio`), the wrapper runs
    `docker exec -i -e ...` (a stub `docker` here: it starts afclaude.py mcp stdio with the container's own env plus
    exactly the variables named by -e, as docker does), the server resolves the caller."""
    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        container_env = {k: os.environ[k] for k in ("AFCLAUDE_DB", "AFCLAUDE_PROJECTS_DIR", "AFCLAUDE_CONFIG")}
        container_env.update(PATH=os.environ.get("PATH", ""), HOME=self.tmp.name)   # a home with no token file of its own
        with open(os.path.join(self.tmp.name, "container_env.json"), "w") as fh:
            json.dump(container_env, fh)
        with open(os.path.join(bin_dir, "docker"), "w") as fh:
            fh.write(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
assert a[:2] == ["exec", "-i"], a
names = [a[i + 1] for i, x in enumerate(a) if x == "-e"]
rest = a[a.index("afclaude-mcp") + 1:]
assert rest[0] == "afclaude", rest
env = json.load(open({os.path.join(self.tmp.name, "container_env.json")!r}))
env.update({{n: os.environ[n] for n in names if n in os.environ}})
os.execve({sys.executable!r}, [{sys.executable!r}, {os.path.join(HERE, "afclaude.py")!r}] + rest[1:], env)
""")
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.bin = bin_dir
        # a machine registered for this very host: the wrapper sends socket.gethostname() (no clone alert)
        code = roles.new_registration_code()
        actions.perform(self.conn, "machine.code", {"code_sha256": roles.code_sha256(code)}, actor="cli", via="cli")
        r = actions.register_machine(self.conn, code, socket.gethostname(), actor="cli", via="cli")
        self.machine_user = r["user_id"]
        self.file = os.path.join(self.tmp.name, "machine.json")
        with open(os.open(self.file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
            json.dump({"token": r["token"]}, fh)

    def add(self, token_file=True, **env):
        base = {"PATH": self.bin + ":" + os.environ.get("PATH", ""), "HOME": self.tmp.name,
                "AFCLAUDE_MACHINE_FILE": self.file if token_file else os.path.join(self.tmp.name, "none.json")}
        params = StdioServerParameters(command=sys.executable,
                                       args=[os.path.join(HERE, "docker", "host-kit", "afclaude_host.py"), "mcp", "stdio"],
                                       env={**base, **env}, cwd=self.tmp.name)

        async def go():
            async with Client(params, mode="legacy", read_timeout_seconds=30) as c:
                return json.loads((await c.call_tool("afclaude_add_task", {"title": "w", "project": "P"})).content[0].text)
        t = asyncio.run(go())
        return next(r for r in self.audit("task.add") if r["target_id"] == str(t["id"]))

    def test_a_session_resolves_as_the_machine_user_with_its_session_id(self):
        row = self.add(CLAUDE_CODE_SESSION_ID=OWN, CLAUDECODE="1")
        self.assertEqual((row["actor"], row["session"]), (f"machine:{self.machine_user}", OWN))

    def test_no_token_file_is_the_host_owner(self):
        row = self.add(token_file=False, CLAUDE_CODE_SESSION_ID=OWN, CLAUDECODE="1")
        self.assertEqual((row["actor"], row["session"]), ("cli", OWN))             # D-330

    def test_a_driven_session_still_resolves_as_task_manager_or_agent_through_the_wrapper(self):
        """D-198: whatever token the file holds, the session id Claude Code gives its MCP child decides."""
        tm = self.add(CLAUDE_CODE_SESSION_ID=TM, CLAUDECODE="1")
        drv = self.add(CLAUDE_CODE_SESSION_ID=DRV, CLAUDECODE="1")
        guard = self.add(CLAUDE_CODE_SESSION_ID=NEW, CLAUDECODE="1", CLAUDE_GUARD_DISABLE="1")
        self.assertEqual((tm["actor"], tm["session"]), (f"task-manager:{TM}", TM))
        self.assertEqual((drv["actor"], drv["session"]), (f"agent:{DRV}", DRV))
        self.assertEqual((guard["actor"], guard["session"]), (f"agent:{NEW}", NEW))

    def test_the_wrapper_refuses_an_env_the_container_must_not_get(self):
        with self.assertRaises(Exception):
            self.add(CLAUDE_CODE_SESSION_ID=OWN, AFCLAUDE_DB="/elsewhere.db")       # refused: the server never starts
        self.assertEqual(self.audit("task.add"), [])

    def test_the_project_dir_reaches_the_server(self):
        """The container's cwd is not the caller's: the wrapper passes the caller's directory as CLAUDE_PROJECT_DIR,
        which is the server's second source for "project" and ".", after the client's roots."""
        proj = os.path.join(self.tmp.name, "Work", "Proj")
        os.makedirs(proj)
        params_env = {"PATH": self.bin + ":" + os.environ.get("PATH", ""), "HOME": self.tmp.name,
                      "AFCLAUDE_MACHINE_FILE": self.file, "CLAUDE_CODE_SESSION_ID": OWN, "CLAUDECODE": "1"}
        params = StdioServerParameters(command=sys.executable,
                                       args=[os.path.join(HERE, "docker", "host-kit", "afclaude_host.py"), "mcp", "stdio"],
                                       env=params_env, cwd=proj)

        async def go():
            async with Client(params, mode="legacy", read_timeout_seconds=30) as c:
                return json.loads((await c.call_tool("afclaude_add_task", {"title": "dir"})).content[0].text)
        self.assertEqual(asyncio.run(go()).get("project_source"), "env")


if __name__ == "__main__":
    unittest.main()
