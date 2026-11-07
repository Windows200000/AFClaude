#!/usr/bin/env python3
"""Tests for phase 5a A2 (docs/phase5a_plan.md §3.1, §3.3, §5 A2): identity at every entry point. Each
row of the §3.1 table from fixture envs (a shell, an interactive session with and without a token, a
task-manager, a subagent with a stamped id, a driven agent, the guard bypass with an unknown id, a runner
actor claimed inside a session, a revoked token), a driven session with a machine token (D-198), the /clear
chain (D-328/D-329: the MCP server's env keeps the pre-/clear id, the hook's stamp has the new one), the
audit row's session column, the hooks (identity_hook.py), `tasks.py whoami`, and the grep test: no literal
actor="cli" outside identity.py. Nothing is refused in A2: only who acts is recorded.

    python3 -m unittest test_identity
"""
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import identity  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

TM = "aaaaaaaa-0000-4000-8000-00000000000a"        # a project's task-manager
TM2 = "aaaaaaaa-0000-4000-8000-00000000000b"       # another project's
DRV = "dddddddd-0000-4000-8000-00000000000d"       # a session AFClaude drives (a review)
OWN = "eeeeeeee-0000-4000-8000-00000000000e"       # the owner's interactive session
NEW = "ffffffff-0000-4000-8000-00000000000f"       # an id after a /clear
OTHER = "99999999-0000-4000-8000-000000000009"
SESSION = {"CLAUDECODE": "1"}


def env(sid=None, **kw):
    e = dict(SESSION, **kw)
    if sid:
        e["CLAUDE_CODE_SESSION_ID"] = sid
    return e


class Base(unittest.TestCase):
    def setUp(self):
        identity._noted.clear()           # the once-per-process log lines
        host = mock.patch("socket.gethostname", return_value="host-a")   # the host the tokens are issued on
        host.start()
        self.addCleanup(host.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.conn = store.connect(self.db, create=True)
        self.noToken = os.path.join(self.tmp.name, "none.json")   # a token file that does not exist
        actions.perform(self.conn, "project.add", {"name": "P"}, actor="cli", via="cli")
        actions.perform(self.conn, "project.add", {"name": "Q"}, actor="cli", via="cli")
        store.update_project(self.conn, "P", manager_session=TM)
        store.update_project(self.conn, "Q", manager_session=TM2)
        store.upsert_driven_session(self.conn, DRV, kind="review", started_at=store.now_iso())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def resolve(self, e, **kw):
        kw.setdefault("token_file", self.noToken)
        return identity.resolve(self.conn, e, **kw)

    def machine(self):
        """-> (user_id, token) of a registered machine user."""
        code = roles.new_registration_code()
        actions.perform(self.conn, "machine.code", {"code_sha256": roles.code_sha256(code)}, actor="cli", via="cli")
        r = actions.register_machine(self.conn, code, "host-a", actor="cli", via="cli")
        return r["user_id"], r["token"]

    def clear(self, old, new):
        for path, payload in actions.state_flatten("keepalive", {"cleared": {old: {"to": new, "at": "t"}}}).items():
            store.state_set(self.conn, "keepalive", path, payload, "runner:keepalive", "runner")


class Table(Base):
    """The rows of §3.1."""

    def test_a_shell_is_the_host_owner(self):
        w = self.resolve({})
        self.assertEqual((w.actor, w.kind, w.session, w.autonomous), ("cli", "host", None, False))

    def test_a_runner_is_a_runner(self):
        w = self.resolve({}, claim="runner:keepalive")
        self.assertEqual((w.actor, w.kind), ("runner:keepalive", "runner"))
        with self.assertRaises(ValueError):
            self.resolve({}, claim="keepalive")

    def test_a_runner_claimed_inside_a_session_is_the_sessions_principal(self):
        for sid, want in ((TM, f"task-manager:{TM}"), (DRV, f"agent:{DRV}"), (OWN, "cli")):
            w = self.resolve(env(sid), claim="runner:keepalive")
            self.assertEqual(w.actor, want, sid)
            self.assertNotIn("runner", w.actor)

    def test_a_task_manager(self):
        w = self.resolve(env(TM))
        self.assertEqual((w.actor, w.kind, w.session, w.agent_id, w.autonomous),
                         (f"task-manager:{TM}", "task-manager", TM, None, True))

    def test_a_task_managers_subagent_is_an_agent(self):
        w = self.resolve(env(TM, AFCLAUDE_AGENT_ID="a123"))
        self.assertEqual((w.actor, w.kind, w.agent_id, w.autonomous), (f"agent:{TM}:a123", "agent", "a123", True))
        # through MCP the stamp carries the id
        w = self.resolve(env(TM), transport="mcp", stamp={"session": TM, "agent": "a123", "agent_type": "x"})
        self.assertEqual(w.actor, f"agent:{TM}:a123")
        # the agent id is read only together with a session in the env
        self.assertEqual(self.resolve({"AFCLAUDE_AGENT_ID": "a123"}).actor, "cli")

    def test_a_driven_session_is_an_agent(self):
        w = self.resolve(env(DRV))
        self.assertEqual((w.actor, w.kind, w.autonomous), (f"agent:{DRV}", "agent", True))

    def test_the_guard_bypass_with_an_unknown_id_is_an_agent(self):
        w = self.resolve(env(OTHER, CLAUDE_GUARD_DISABLE="1"))
        self.assertEqual((w.actor, w.autonomous), (f"agent:{OTHER}", True))

    def test_the_guard_bypass_does_not_demote_a_task_manager(self):
        self.assertEqual(self.resolve(env(TM, CLAUDE_GUARD_DISABLE="1")).actor, f"task-manager:{TM}")

    def test_an_interactive_session_without_a_token_is_the_host_owner(self):
        """D-330: an interactive session of this host with no valid token is the host owner (cli), its session id
        recorded; a subagent of it stays an agent under its own id (owner-equivalent for the old rule)."""
        w = self.resolve(env(OWN))                                                         # an unknown session, no token
        self.assertEqual((w.actor, w.kind, w.session, w.autonomous, w.token), ("cli", "host", OWN, False, "none"))
        w = self.resolve(env(OWN, AFCLAUDE_AGENT_ID="a7"))
        self.assertEqual((w.actor, w.kind, w.agent_id, w.autonomous), (f"agent:{OWN}:a7", "agent", "a7", False))
        w = self.resolve(env(OWN), transport="mcp", stamp={"session": OWN, "agent": "a7"})
        self.assertEqual(w.actor, f"agent:{OWN}:a7")
        self.assertEqual(self.resolve(env()).actor, "cli")                                 # CLAUDECODE, no id
        self.assertEqual(self.resolve({}, transport="mcp").actor, "cli")                   # the server's env is empty

    def test_an_interactive_session_with_a_valid_token_is_the_machine_user(self):
        uid, token = self.machine()
        for transport in ("cli", "mcp"):
            w = self.resolve(env(OWN, AFCLAUDE_MACHINE_TOKEN=token), transport=transport)
            self.assertEqual((w.actor, w.kind, w.user_id, w.session, w.token, w.autonomous),
                             (f"machine:{uid}", "machine", uid, OWN, "valid", False))
        self.assertEqual(store.get_machine_token(self.conn, 1)["last_transport"], "stdio")

    def test_the_token_file_must_be_private(self):
        uid, token = self.machine()
        path = os.path.join(self.tmp.name, "machine.json")
        with open(path, "w") as fh:
            json.dump({"token": token}, fh)
        os.chmod(path, 0o600)
        self.assertEqual(self.resolve(env(OWN), token_file=path).actor, f"machine:{uid}")
        os.chmod(path, 0o644)
        with self.assertLogs("afclaude.identity", "WARNING") as logs:
            w = self.resolve(env(OWN), token_file=path)
            self.resolve(env(OWN), token_file=path)
        self.assertEqual((w.actor, w.token), ("cli", "insecure-file"))     # D-330; the session is the host's own
        self.assertEqual(len(logs.output), 1)                              # D-235: visible, once
        self.assertIn("mode 0600", logs.output[0])

    def test_a_revoked_or_unknown_token_is_no_machine_identity(self):
        uid, token = self.machine()
        bad = [("unknown", "afcm_999_" + "0" * 64), ("garbage", "not-a-token")]
        actions.perform(self.conn, "machine.revoke", {"user_id": uid}, actor="cli", via="cli")
        bad.append(("revoked", token))
        for what, tok in bad:
            with self.assertLogs("afclaude.identity", "WARNING") as logs:
                w = self.resolve(env(OWN, AFCLAUDE_MACHINE_TOKEN=tok))
            self.assertEqual((w.actor, w.kind, w.token), ("cli", "host", "invalid"), what)   # D-330
            self.assertIn("unknown, revoked or malformed", logs.output[0])
            identity._noted.clear()

    def test_a_driven_session_with_a_machine_token_stays_task_manager_or_agent(self):
        """D-198: a session AFClaude started is task-manager / agent whatever token it carries."""
        uid, token = self.machine()
        tok = {"AFCLAUDE_MACHINE_TOKEN": token}
        self.assertEqual(self.resolve(env(TM, **tok)).actor, f"task-manager:{TM}")
        self.assertEqual(self.resolve(env(TM, AFCLAUDE_AGENT_ID="a1", **tok)).actor, f"agent:{TM}:a1")
        self.assertEqual(self.resolve(env(DRV, **tok)).actor, f"agent:{DRV}")
        self.assertEqual(self.resolve(env(OTHER, CLAUDE_GUARD_DISABLE="1", **tok)).actor, f"agent:{OTHER}")
        self.assertEqual(self.resolve(env(DRV, **tok), transport="mcp", stamp={"session": DRV}).actor, f"agent:{DRV}")
        self.assertIsNone(store.get_machine_token(self.conn, 1)["last_seen_at"])   # the token was never even used


class Clear(Base):
    """D-328/D-329: session ids follow the /clear chain; an id outside it is never accepted."""

    def test_the_pre_clear_id_of_a_known_chain_is_not_unknown(self):
        self.clear(TM, NEW)
        store.update_project(self.conn, "P", manager_session=NEW)          # session.cleared moved it
        w = self.resolve(env(TM), transport="mcp")                          # the MCP server's env: the old id
        self.assertEqual((w.actor, w.session), (f"task-manager:{NEW}", NEW))
        self.assertEqual(self.resolve(env(NEW)).actor, f"task-manager:{NEW}")   # the Bash env follows the /clear
        # the chain known but the registry not yet moved: the old id still classifies
        store.update_project(self.conn, "P", manager_session=TM)
        self.assertEqual(self.resolve(env(NEW)).actor, f"task-manager:{NEW}")

    def test_the_stamp_names_the_new_id_and_must_be_in_the_chain(self):
        self.clear(TM, NEW)
        store.update_project(self.conn, "P", manager_session=NEW)
        w = self.resolve(env(TM), transport="mcp", stamp={"session": NEW})
        self.assertEqual((w.actor, w.source), (f"task-manager:{NEW}", "stamp"))
        # a stamp outside the chain of a session AFClaude drives is ignored: the process's own chain wins
        w = self.resolve(env(TM), transport="mcp", stamp={"session": OTHER})
        self.assertEqual((w.actor, w.source), (f"task-manager:{NEW}", "env"))
        # one naming another AFClaude session: A2's shadow mode logs it once and goes on under the env id
        for other in (TM2, DRV):
            identity._noted.clear()
            with self.assertLogs("afclaude.identity", "WARNING") as logs:
                w = self.resolve(env(OWN), transport="mcp", stamp={"session": other})
            self.assertEqual((w.actor, w.session, w.source), ("cli", OWN, "env"))
            self.assertIn("shadow mode", logs.output[0])
        # roles enforced (A3): the same claim is an error
        with mock.patch.object(identity, "ENFORCE_CONFLICTS", True):
            for other in (TM2, DRV):
                with self.assertRaises(identity.IdentityConflict):
                    self.resolve(env(OWN), transport="mcp", stamp={"session": other})

    def test_an_interactive_sessions_clear_is_taken_from_the_stamp(self):
        """The store follows only driven sessions' /clears: here neither id is known."""
        w = self.resolve(env(OWN), transport="mcp", stamp={"session": NEW})
        self.assertEqual((w.actor, w.session, w.source), ("cli", NEW, "stamp"))
        uid, token = self.machine()
        w = self.resolve(env(OWN, AFCLAUDE_MACHINE_TOKEN=token), transport="mcp", stamp={"session": NEW})
        self.assertEqual((w.actor, w.session), (f"machine:{uid}", NEW))

    def test_a_stamp_alone_never_elevates(self):
        """No session id in the server's env: a stamp naming a task-manager / a driven session is ignored
        (logged once), so a self-written stamp cannot make a caller one."""
        for sid in (TM, DRV):
            identity._noted.clear()
            with self.assertLogs("afclaude.identity", "WARNING") as logs:
                w = self.resolve({}, transport="mcp", stamp={"session": sid})
            self.assertEqual((w.actor, w.autonomous, w.session, w.source), ("cli", False, None, "none"), sid)
            self.assertIn("names none", logs.output[0])
        with mock.patch.object(identity, "ENFORCE_CONFLICTS", True), self.assertRaises(identity.IdentityConflict):
            self.resolve({}, transport="mcp", stamp={"session": TM})
        # a session AFClaude does not know is taken (an interactive session's own id)
        w = self.resolve({}, transport="mcp", stamp={"session": OWN})
        self.assertEqual((w.actor, w.session, w.source), ("cli", OWN, "stamp"))

    def test_a_malformed_stamp_is_dropped_in_shadow_and_refused_when_enforced(self):
        stamps = ("x", {"session": "a b"}, {"session": 5}, {"session": OWN, "agent": "a;b"})
        for stamp in stamps:
            identity._noted.clear()
            with self.assertLogs("afclaude.identity", "WARNING"):
                w = self.resolve(env(OWN), transport="mcp", stamp=stamp)
            self.assertEqual((w.actor, w.session, w.source), ("cli", OWN, "env"), stamp)
            with mock.patch.object(identity, "ENFORCE_CONFLICTS", True), self.assertRaises(identity.IdentityConflict):
                self.resolve(env(OWN), transport="mcp", stamp=stamp)

    def test_an_old_id_of_a_driven_chain_resolves_to_its_newest(self):
        self.clear(DRV, NEW)
        w = self.resolve(env(DRV), transport="mcp")
        self.assertEqual((w.actor, w.session), (f"agent:{NEW}", NEW))


class Audit(Base):
    def test_actor_forms(self):
        for a in ("machine:7", f"task-manager:{TM}", f"agent:{TM}", f"agent:{TM}:a1", "agent:unknown", "runner:keepalive"):
            self.assertTrue(actions.ACTOR_RE.match(a), a)
        for a in ("machine:", "machine:0", "task-manager:", "agent:a:b:c", "agent:", "agent: x"):
            self.assertFalse(actions.ACTOR_RE.match(a), a)

    def rows(self):
        return list(reversed(store.audit_log(self.conn, limit=50)))

    def test_the_session_column(self):
        p = lambda actor, **kw: actions.perform(self.conn, "task.add", {"title": "t"}, actor=actor, via="mcp", **kw)  # noqa: E731
        p(f"task-manager:{TM}")
        p(f"agent:{TM}:a1")
        p("agent:unknown")
        p("machine:3", session=OWN)
        actions.perform(self.conn, "task.add", {"title": "t"}, actor="cli", via="cli")
        got = [(r["actor"], r["session"]) for r in self.rows() if r["action"] == "task.add"]
        self.assertEqual(got, [(f"task-manager:{TM}", TM), (f"agent:{TM}:a1", TM), ("agent:unknown", None),
                               ("machine:3", OWN), ("cli", None)])

    def test_the_old_per_session_rules_see_the_session_only_through_mcp(self):
        self.assertEqual(actions.Ctx(f"agent:{TM}", "mcp").session, TM)
        self.assertEqual(actions.Ctx("machine:3", "mcp", session=OWN).session, OWN)
        self.assertIsNone(actions.Ctx(f"agent:{TM}", "cli").session)           # the CLI's writes carry no own-session rule
        self.assertEqual(actions.Ctx(f"agent:{TM}", "cli").actor_session, TM)   # but the audit row names it
        self.assertEqual(actions.Ctx(f"mcp:{TM}", "cli").session, TM)           # the old form as before

    def test_the_old_rule_still_applies_to_an_autonomous_caller(self):
        """A2 refuses nothing new: an owner-only action is still refused for a task-manager / agent via MCP."""
        for actor in (f"task-manager:{TM}", f"agent:{DRV}"):
            with self.assertRaises(actions.Forbidden):
                actions.perform(self.conn, "setting.set", {"key": "stall_verify_minutes", "value": 20},
                                actor=actor, via="mcp")
        actions.perform(self.conn, "setting.set", {"key": "stall_verify_minutes", "value": 20},
                        actor="agent:" + OWN, via="mcp")       # an interactive session: as before


class Cli(Base):
    def run_cli(self, *argv, **e):
        out = io.StringIO()
        with mock.patch.dict(os.environ, e), contextlib.redirect_stdout(out):
            tasks._WHO = None
            rc = tasks.main(["--db", self.db, *argv])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def audit_of(self, action):
        return [(r["actor"], r["session"]) for r in reversed(store.audit_log(self.conn, limit=50))
                if r["action"] == action]

    def test_whoami(self):
        for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "AFCLAUDE_AGENT_ID"):
            os.environ.pop(var, None)
        self.assertEqual(self.run_cli("whoami").strip(), "cli  (host)")
        d = json.loads(self.run_cli("whoami", "--json", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=TM))
        self.assertEqual((d["actor"], d["kind"], d["session"], d["autonomous"]),
                         (f"task-manager:{TM}", "task-manager", TM, True))
        out = self.run_cli("whoami", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=TM, AFCLAUDE_AGENT_ID="a9")
        self.assertIn(f"agent:{TM}:a9", out)

    def test_cli_writes_are_recorded_as_the_principal(self):
        self.run_cli("add", "from a shell", "--project", "P")
        self.run_cli("add", "from the task-manager", "--project", "P", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=TM)
        self.run_cli("add", "from its subagent", "--project", "P", CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=TM,
                     AFCLAUDE_AGENT_ID="a1")
        self.assertEqual(self.audit_of("task.add"),
                         [("cli", None), (f"task-manager:{TM}", TM), (f"agent:{TM}:a1", TM)])

    def test_a_conflicting_claim_is_logged_in_shadow_and_an_error_when_enforced(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_AGENT_ID": "a b", "CLAUDECODE": "1",
                                          "CLAUDE_CODE_SESSION_ID": TM}):
            tasks._WHO = None
            with self.assertLogs("afclaude.identity", "WARNING") as logs:     # shadow: the call goes on, as its session
                self.assertEqual(tasks.main(["--db", self.db, "add", "x", "--project", "P"]), 0)
            self.assertIn("not an agent id", logs.output[0])
            self.assertEqual(self.audit_of("task.add"), [(f"task-manager:{TM}", TM)])
            tasks._WHO = None
            err = io.StringIO()
            with mock.patch.object(identity, "ENFORCE_CONFLICTS", True), contextlib.redirect_stderr(err):
                self.assertEqual(tasks.main(["--db", self.db, "add", "x"]), 1)
            self.assertIn("not an agent id", err.getvalue())


class Hook(unittest.TestCase):
    HOOK = os.path.join(HERE, "identity_hook.py")

    def call(self, which, payload, raw=None):
        p = subprocess.run([sys.executable, self.HOOK, which], input=raw if raw is not None else json.dumps(payload),
                           capture_output=True, text=True, timeout=30)
        return p.returncode, (json.loads(p.stdout) if p.stdout else None), p.stderr

    def test_the_mcp_stamp(self):
        pay = {"session_id": OWN, "tool_name": "mcp__afclaude__afclaude_add_task",
               "tool_input": {"title": "t", "priority": "low"}}
        rc, out, _ = self.call("mcp", pay)
        self.assertEqual(rc, 0)
        h = out["hookSpecificOutput"]
        self.assertEqual((h["hookEventName"], "permissionDecision" in h), ("PreToolUse", False))
        self.assertEqual(h["updatedInput"], {"title": "t", "priority": "low", "_afc": {"session": OWN}})
        rc, out, _ = self.call("mcp", dict(pay, agent_id="a1b2", agent_type="afclaude-worker"))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["_afc"],
                         {"session": OWN, "agent": "a1b2", "agent_type": "afclaude-worker"})

    def test_a_forged_stamp_is_overwritten(self):
        pay = {"session_id": OWN, "tool_input": {"title": "t", "_afc": {"session": TM}}}
        self.assertEqual(self.call("mcp", pay)[1]["hookSpecificOutput"]["updatedInput"]["_afc"], {"session": OWN})

    def test_a_model_written_stamp_never_reaches_the_server(self):
        """Every path that stamps nothing still strips `_afc` from the arguments."""
        forged = {"title": "t", "_afc": {"session": TM}}
        for pay in ({"tool_input": dict(forged)},                                    # no session id
                    {"session_id": "a b", "tool_input": dict(forged)},               # an odd session id
                    {"session_id": OWN, "agent_id": "a;b", "tool_input": dict(forged)}):   # an odd agent id
            rc, out, _ = self.call("mcp", pay)
            self.assertEqual((rc, out["hookSpecificOutput"]["updatedInput"]), (0, {"title": "t"}), pay)
        self.assertEqual(self.call("mcp", {"session_id": OWN, "tool_input": {"title": "t"}})[1]["hookSpecificOutput"]
                         ["updatedInput"], {"title": "t", "_afc": {"session": OWN}})

    def test_the_bash_export_only_in_a_subagent_and_only_for_our_clis(self):
        cmd = "cd /x && python3 tasks.py add t"
        pay = {"session_id": OWN, "permission_mode": "auto", "tool_input": {"command": cmd, "description": "d"}}
        self.assertEqual(self.call("bash", pay), (0, None, ""))                    # the main thread: not rewritten
        rc, out, _ = self.call("bash", dict(pay, agent_id="a1b2"))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"],
                         {"command": "export AFCLAUDE_AGENT_ID=a1b2; " + cmd, "description": "d"})
        other = dict(pay, agent_id="a1b2", tool_input={"command": "ls"})
        self.assertEqual(self.call("bash", other), (0, None, ""))                  # not one of our CLIs
        odd = dict(pay, agent_id="a;rm -rf")
        self.assertEqual(self.call("bash", odd), (0, None, ""))                    # an odd id never reaches the shell
        for mode in ("bypassPermissions",):
            self.assertIsNotNone(self.call("bash", dict(pay, agent_id="a1b2", permission_mode=mode))[1], mode)
        # in any other mode the rewritten command would miss a prefix allow rule (refused / asks): untouched
        for mode in ("default", "dontAsk", "acceptEdits", "plan", None):
            p = dict(pay, agent_id="a1b2", permission_mode=mode)
            if mode is None:
                del p["permission_mode"]
            self.assertEqual(self.call("bash", p), (0, None, ""), mode)
        for name in ("questions.py check", "dashboard/setup.py x", "runner_state.py y"):   # entry points of identity.py
            rc, out, _ = self.call("bash", dict(pay, agent_id="a1b2", tool_input={"command": "python3 " + name}))
            self.assertIsNotNone(out, name)

    def test_it_fails_open(self):
        rc, out, err = self.call("mcp", None, raw="not json")
        self.assertEqual((rc, out), (1, None))                                     # a non-blocking error, no output
        self.assertIn("identity_hook", err)
        self.assertEqual(self.call("nope", {})[0], 1)
        self.assertEqual(self.call("mcp", {"tool_input": {"a": 1}}), (0, None, ""))    # no session id: left unstamped

    def test_it_stays_off_the_database(self):
        with open(self.HOOK) as fh:
            src = fh.read()
        imports = set(re.findall(r"^\s*(?:import|from)\s+(\w+)", src, re.M))
        self.assertTrue(imports <= {"json", "re", "sys", "__future__"}, imports)

    def test_the_settings_wire_it(self):
        with open(os.path.join(HERE, ".claude", "settings.json")) as fh:
            pre = json.load(fh)["hooks"]["PreToolUse"]
        got = {h["matcher"]: h["hooks"][0]["command"] for h in pre}
        self.assertIn("identity_hook.py\" mcp", got["mcp__afclaude__.*"])
        self.assertIn("identity_hook.py\" bash", got["Bash"])

    def test_a_missing_hook_script_does_not_block_the_call(self):
        """python3 exits 2 for a missing script, and exit 2 from a hook BLOCKS the tool call: the settings'
        commands end in `|| exit 1` (a non-blocking error)."""
        with open(os.path.join(HERE, ".claude", "settings.json")) as fh:
            pre = json.load(fh)["hooks"]["PreToolUse"]
        with tempfile.TemporaryDirectory() as empty:
            for h in pre:
                cmd = h["hooks"][0]["command"]
                self.assertTrue(cmd.endswith("|| exit 1"), cmd)
                p = subprocess.run(["sh", "-c", cmd], env=dict(os.environ, CLAUDE_PROJECT_DIR=empty),
                                   input="{}", capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 1, cmd)
                bare = subprocess.run(["sh", "-c", cmd.removesuffix(" || exit 1")],
                                      env=dict(os.environ, CLAUDE_PROJECT_DIR=empty), input="{}",
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(bare.returncode, 2)                              # what the suffix prevents


class Literals(unittest.TestCase):
    def test_no_literal_cli_actor_outside_identity(self):
        """A CLI path that keeps a literal "cli" would write as the host owner whoever runs it."""
        rx = re.compile(r"""\bactor\s*(?::\s*[\w\[\] |.]+)?=\s*['"](cli|keepalive|dispatcher)['"]""")
        bad = []
        for dirpath, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in (".venv", ".git", "__pycache__", "node_modules", "testdata")]
            for f in files:
                if not f.endswith(".py") or f.startswith("test_") or f in ("testenv.py", "identity.py"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path) as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line) and not line.lstrip().startswith("#"):
                            bad.append(f"{os.path.relpath(path, HERE)}:{n}: {line.strip()}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
