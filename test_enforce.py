#!/usr/bin/env python3
"""Tests for phase 5a A7 (docs/phase5a_plan.md §5 A7): the cut-over to enforcement. With `roles_enforce=on` the
permission matrix decides alone (the old owner_only / is_autonomous rule is not consulted), a refusal leaves an
audit row and, once per actor and action per hour, an alert; with `shadow` nothing changes (the old rule, no
refusal rows, no alerts). Also the host owner's registration command (`tasks.py machine register-host`).
Temp DBs only; notify and failstop are patched.

    python3 -m unittest test_enforce
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import failstop  # noqa: E402
import identity  # noqa: E402
import proposals  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
from test_permissions import Base  # noqa: E402


class Enforcing(Base):
    def setUp(self):
        super().setUp()
        self.alerts = []
        n = mock.patch("notify.notify", side_effect=lambda s, b="", do_push=True: self.alerts.append((s, b)))
        n.start()
        self.addCleanup(n.stop)
        self.m = f"machine:{self.users['machine_ungranted']}"

    def go_on(self):
        code = roles.new_registration_code()
        self.cli("machine.code", code_sha256=roles.code_sha256(code))
        r = actions.register_machine(self.conn, code, socket.gethostname(), actor="cli", via="cli")
        self.cli("grant.set", user_id=r["user_id"], scope="owner")
        self.cli("setting.set", key="roles_enforce", value="on")
        self.alerts.clear()                       # the owner-grant alert is not what these tests count
        return f"machine:{r['user_id']}"

    def mcp(self, name, actor, session=None, **params):
        return actions.perform(self.conn, name, params, actor=actor, via="mcp", session=session, autonomous=actor.startswith(("agent:", "task-manager:")))

    def refused_rows(self, actor=None, action=None):
        rows = [r for r in store.audit_log(self.conn, 200) if isinstance(r["after"], dict) and "refused" in r["after"]]
        return [r for r in rows if (actor is None or r["actor"] == actor) and (action is None or r["action"] == action)]

    def refuse(self, actor=None, name="setting.set", session=None):
        with self.assertRaises(actions.RoleRefusal) as e:
            self.mcp(name, actor or self.m, session, key="session_hours", value=4.0)
        return e.exception


class Refusals(Enforcing):
    def test_a_refusal_is_a_forbidden_with_an_audit_row_and_an_alert(self):
        self.go_on()
        before = actions.get_setting(self.conn, "session_hours")
        e = self.refuse(session="sess-1")
        self.assertIsInstance(e, actions.Forbidden)               # the entry points' handlers take it as before
        rows = self.refused_rows(self.m, "setting.set")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["via"], rows[0]["session"], rows[0]["target_type"]), ("mcp", "sess-1", "setting"))
        self.assertIn("owner only", rows[0]["after"]["refused"])
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), before)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("setting.set", self.alerts[0][0])
        self.assertIn(self.m, self.alerts[0][1])

    def test_one_alert_per_actor_and_action_and_hour_but_every_refusal_is_audited(self):
        self.go_on()
        for _ in range(3):
            self.refuse()
        self.assertEqual((len(self.refused_rows(self.m)), len(self.alerts)), (3, 1))
        with self.assertRaises(actions.RoleRefusal):
            self.mcp("window.set", self.m, day="2026-11-10", start="09:00")
        self.assertEqual(len(self.alerts), 2)                       # another action
        self.refuse(actor=self.actor["viewer"])
        self.assertEqual(len(self.alerts), 3)                       # another actor
        later = store._utcnow() + timedelta(minutes=61)
        with mock.patch.object(store, "_utcnow", return_value=later):
            self.refuse()
        self.assertEqual(len(self.alerts), 4)                       # an hour on, the same pair again
        with mock.patch.object(store, "_utcnow", return_value=later + timedelta(minutes=5)):
            self.refuse()
        self.assertEqual(len(self.alerts), 4)

    def test_the_alert_dedupe_holds_across_processes(self):
        """It reads the audit log, not process memory: a fresh process (cleared hour gate) stays quiet."""
        self.go_on()
        self.refuse()
        actions._roles_logged.clear()
        self.refuse()
        self.assertEqual(len(self.alerts), 1)

    def test_a_task_managers_and_agents_refusals_are_audited_too(self):
        self.go_on()
        tm = self.actor["tm_own"]
        with self.assertRaises(actions.RoleRefusal):
            self.mcp("setting.set", tm, key="session_hours", value=4.0)
        agent = "agent:unknown"                                     # no project to propose to
        with self.assertRaises(actions.RoleRefusal):
            self.mcp("task.add", agent, title="x", project="alpha")
        self.assertEqual(len(self.refused_rows(tm)), 1)
        self.assertEqual(len(self.refused_rows(agent)), 1)

    def test_a_proposal_is_no_refusal(self):
        self.go_on()
        with self.assertRaises(actions.Proposed):
            self.mcp("task.add", self.actor["agent"], title="x", project="alpha")
        self.assertEqual(self.refused_rows(), [])
        self.assertEqual(self.alerts, [])

    def test_allowed_writes_leave_no_refusal(self):
        owner = self.go_on()
        self.mcp("setting.set", owner, key="session_hours", value=4.0)
        self.mcp("task.add", self.actor["manager"], title="x", project="alpha")
        self.assertEqual((self.refused_rows(), self.alerts), ([], []))

    def test_the_run_summary_lists_a_refusal_as_refused(self):
        self.go_on()
        self.refuse(actor=self.actor["agent"] + ":a1", session=self.sess["AG"])
        row = self.refused_rows()[0]
        self.assertIn("REFUSED", proposals._write_line(row)["line"])

    def test_a_failing_audit_write_is_an_error_and_an_alert(self):
        self.go_on()
        with mock.patch.object(store, "add_audit", side_effect=RuntimeError("boom")), \
                mock.patch.object(failstop, "fail") as fail:
            with self.assertRaises(actions.RolesFailure) as e:
                self.refuse()
        self.assertIn("the write itself was refused", str(e.exception))
        self.assertIsInstance(e.exception.__cause__, actions.RoleRefusal)
        self.assertEqual(fail.call_args.args[:2], ("roles", "a refusal could not be audited"))

    def test_a_failing_alert_is_an_error_and_an_alert_but_the_row_stays(self):
        self.go_on()
        with mock.patch("notify.notify", side_effect=RuntimeError("no push")), \
                mock.patch.object(failstop, "fail") as fail:
            with self.assertRaises(actions.RolesFailure) as e:
                self.refuse()
        self.assertIn("audited", str(e.exception))
        self.assertEqual(len(self.refused_rows(self.m)), 1)
        self.assertEqual(fail.call_args.args[:2], ("roles", "a refusal alert could not be sent"))


class MatrixDecidesAlone(Enforcing):
    """Under `on` the old owner_only / is_autonomous rule is not consulted; under shadow it still is."""

    def pause(self):
        self.cli("automation.set_project", project="alpha", paused=True, reason="r")

    def test_the_task_managers_pause_clear_is_allowed_through_mcp_under_on(self):
        self.go_on()
        self.pause()
        self.mcp("automation.set_project", self.actor["tm_own"], project="alpha", paused=False)   # D-283
        self.assertFalse(store.get_project_pause(self.conn, self.A)["paused"])

    def test_the_old_rule_still_refuses_the_same_call_in_shadow(self):
        self.pause()
        with self.assertRaises(actions.Forbidden) as e, self.assertLogs("afclaude.roles", "WARNING"):
            self.mcp("automation.set_project", self.actor["tm_own"], project="alpha", paused=False)
        self.assertNotIsInstance(e.exception, actions.RoleRefusal)
        self.assertIn("autonomous", str(e.exception))


class ShadowIsUnchanged(Enforcing):
    def test_no_refusal_row_no_alert_no_new_rule_in_shadow(self):
        with mock.patch.object(actions, "_refusal_after", side_effect=AssertionError("called in shadow")), \
                self.assertLogs("afclaude.roles", "WARNING"):
            for who in (self.m, self.actor["viewer"]):                      # would be refused: applied, as before
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0}, actor=who, via="mcp")
            with self.assertRaises(actions.Forbidden) as e:               # the old rule's own refusal, as before
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 5.0},
                                actor=self.actor["tm_own"], via="mcp", autonomous=True)
            self.assertNotIsInstance(e.exception, actions.RoleRefusal)
        self.assertEqual((self.refused_rows(), self.alerts), ([], []))

    def test_matrix_decides_is_not_asked_unless_the_old_rule_would_refuse(self):
        with mock.patch.object(actions, "_matrix_decides", side_effect=AssertionError("asked")):
            self.cli("setting.set", key="session_hours", value=4.0)                          # the host owner
            self.mcp("task.add", self.actor["manager"], title="x", project="alpha")           # not autonomous
        with mock.patch.object(actions, "_matrix_decides", return_value=False) as asked:
            with self.assertRaises(actions.Forbidden), self.assertLogs("afclaude.roles", "WARNING"):
                self.mcp("setting.set", self.actor["tm_own"], key="session_hours", value=5.0)
        asked.assert_called_once()                                  # only for an autonomous caller of an owner_only action


class HostRegistration(Base):
    """`tasks.py machine register-host`: one plain-shell command for the owner (A7)."""

    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.tmp.name, "machine.json")
        self.alerts = []
        n = mock.patch("notify.notify", side_effect=lambda s, b="", do_push=True: self.alerts.append((s, b)))
        n.start()
        self.addCleanup(n.stop)
        self.run_cmd = lambda *a, env=None: self._run(a, env or {})

    def _run(self, args, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for k in (identity.SESSION_VAR, identity.CLAUDE_VAR, identity.AGENT_VAR, identity.GUARD_VAR,
                      identity.TOKEN_VAR):
                if k not in env:
                    os.environ.pop(k, None)
            try:
                rc = tasks.main(["--db", self.db, "machine", "register-host", "--file", self.file, *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_it_registers_this_host_grants_owner_and_writes_the_token_file(self):
        rc, out, err = self.run_cmd()
        self.assertEqual((rc, err), (0, ""), out)
        with open(self.file) as fh:
            rec = json.load(fh)
        self.assertEqual(oct(os.stat(self.file).st_mode & 0o777), "0o600")
        self.assertTrue(store.host_owner_machine(self.conn, socket.gethostname()))
        user = store.get_user(self.conn, rec["user_id"])
        self.assertEqual(user["kind"], "machine")
        self.assertEqual([g["scope"] for g in store.list_grants(self.conn, rec["user_id"])], ["owner"])
        # the token now identifies the owner's sessions as that machine user
        w = identity.resolve(self.conn, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1"}, token_file=self.file)
        self.assertEqual(w.actor, f"machine:{rec['user_id']}")
        self.assertIn("roles mode --set on", out)

    def test_it_refuses_inside_a_claude_session_and_changes_nothing(self):
        for env in ({"CLAUDE_CODE_SESSION_ID": "s-1"}, {"CLAUDECODE": "1"}, {"CLAUDE_GUARD_DISABLE": "1"},
                    {"AFCLAUDE_AGENT_ID": "a1"}):
            rc, out, err = self.run_cmd(env=env)
            self.assertNotEqual(rc, 0, env)
            self.assertIn("plain shell", err + out)
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(store.host_machine_users(self.conn, socket.gethostname()), [])

    def test_it_refuses_a_second_time_and_when_the_file_exists(self):
        self.assertEqual(self.run_cmd()[0], 0)
        self.assertEqual(len(self.alerts), 1)                      # the new owner is alerted (D-172)
        rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(store.host_machine_users(self.conn, socket.gethostname())), 1)
        os.unlink(self.file)                                      # the token lost: re-issue is `machine code --user`
        rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertIn("already", err + out)
        self.assertEqual(len(store.host_machine_users(self.conn, socket.gethostname())), 1)

    def test_a_blocked_token_file_is_refused_before_a_code_is_spent(self):
        open(self.file, "w").close()
        rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertEqual(store.host_machine_users(self.conn, socket.gethostname()), [])


if __name__ == "__main__":
    unittest.main()
