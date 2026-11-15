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
    def sub(self, n):
        """A subagent id of the agent session: refusals of it all count as one caller (M1)."""
        return self.actor["agent"] + f":a{n}"

    def test_a_refusal_is_a_forbidden_with_an_audit_row_and_an_alert(self):
        self.go_on()
        before = actions.get_setting(self.conn, "session_hours")
        e = self.refuse(actor=self.sub(1), session=self.sess["AG"])
        self.assertIsInstance(e, actions.Forbidden)               # the entry points' handlers take it as before
        rows = self.refused_rows(self.sub(1), "setting.set")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["via"], rows[0]["session"], rows[0]["target_type"], rows[0]["target_id"]),
                         ("mcp", self.sess["AG"], "setting", "session_hours"))
        self.assertIn("not an agent", rows[0]["after"]["refused"])
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), before)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("setting.set", self.alerts[0][0])
        self.assertIn(self.sub(1), self.alerts[0][1])

    def test_the_alert_never_pushes_inside_the_refused_call(self):
        """M1: the entry goes to the alerts doc; a headless push would hold the refused call for minutes."""
        self.go_on()
        with mock.patch("notify.notify") as n, mock.patch("notify.push", side_effect=AssertionError("pushed")):
            self.refuse(actor=self.sub(1), session=self.sess["AG"])
        self.assertEqual(n.call_args.kwargs, {"do_push": False})

    def test_a_person_gets_the_audit_row_only(self):
        self.go_on()
        for who in (self.m, self.actor["viewer"]):
            self.refuse(actor=who)
            self.assertEqual(len(self.refused_rows(who)), 1)
        self.assertEqual(self.alerts, [])

    def test_one_alert_per_actor_kind_action_and_hour_but_every_refusal_is_audited(self):
        self.go_on()
        for n in (1, 2, 3):                                         # three subagents of one session: one alert
            self.refuse(actor=self.sub(n), session=self.sess["AG"])
        self.assertEqual((len(self.refused_rows()), len(self.alerts)), (3, 1))
        with self.assertRaises(actions.RoleRefusal):
            self.mcp("window.set", self.sub(4), self.sess["AG"], day="2026-11-10", start="09:00")
        self.assertEqual(len(self.alerts), 2)                       # another action
        self.refuse(actor=self.actor["tm_own"], session=self.sess["OWN"])
        self.assertEqual(len(self.alerts), 3)                       # another actor kind
        later = store._utcnow() + timedelta(minutes=61)
        with mock.patch.object(store, "_utcnow", return_value=later):
            self.refuse(actor=self.sub(5), session=self.sess["AG"])
        self.assertEqual(len(self.alerts), 4)                       # an hour on, the same key again
        with mock.patch.object(store, "_utcnow", return_value=later + timedelta(minutes=5)):
            self.refuse(actor=self.sub(6), session=self.sess["AG"])
        self.assertEqual(len(self.alerts), 4)

    def test_another_agent_session_is_another_key_without_a_project(self):
        self.go_on()
        self.refuse(actor=self.sub(1), session=self.sess["AG"])
        self.refuse(actor="agent:other-session:a1", session="other-session")
        self.assertEqual(len(self.alerts), 2)

    def test_the_alert_dedupe_holds_across_processes(self):
        """It reads the audit log, not process memory: a fresh process (cleared hour gate) stays quiet."""
        self.go_on()
        self.refuse(actor=self.sub(1), session=self.sess["AG"])
        actions._roles_logged.clear()
        self.refuse(actor=self.sub(2), session=self.sess["AG"])
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

    def test_the_target_is_the_one_the_params_name_else_a_marker(self):
        self.go_on()
        with self.assertRaises(actions.RoleRefusal):
            self.mcp("setting.set", self.sub(1), self.sess["AG"], key="session_hours", value=4.0)
        with self.assertRaises(actions.Forbidden):
            self.mcp("prompt.defaults_sync", self.sub(1), self.sess["AG"])
        rows = {r["action"]: r for r in self.refused_rows()}
        self.assertEqual(rows["setting.set"]["target_id"], "session_hours")
        self.assertEqual(rows["prompt.defaults_sync"]["target_id"], actions.NO_TARGET)
        self.assertNotIn("None", proposals._write_line(rows["prompt.defaults_sync"])["line"])

    def test_an_actions_own_check_is_audited_under_on(self):
        """L2: _refuse_autonomous / the reviewer path / ... are not matrix verdicts, but a refusal all the same."""
        owner = self.go_on()
        with self.assertRaises(actions.Forbidden) as e:             # the matrix allows the owner, the action says CLI only
            self.mcp("decision.activate_own", owner, dry_run=True, owner_entries=[])
        self.assertNotIsInstance(e.exception, actions.RoleRefusal)
        rows = self.refused_rows(owner, "decision.activate_own")
        self.assertEqual(len(rows), 1)
        self.assertIn("CLI only", rows[0]["after"]["refused"])
        self.assertEqual(self.alerts, [])                           # a person: the audit row only
        actions.perform(self.conn, "setting.set", {"key": "roles_enforce", "value": "shadow"}, actor=owner, via="cli")
        with self.assertRaises(actions.Forbidden):                  # shadow: no refusal row, as before
            self.mcp("decision.activate_own", owner, dry_run=True, owner_entries=[])
        self.assertEqual(len(self.refused_rows(owner, "decision.activate_own")), 1)

    def test_the_reviewer_path_refusal_is_audited_and_alerted_under_on(self):
        self.go_on()
        prop = store.add_proposal(self.conn, self.A, None, self.actor["agent"], "setting.set",
                                  {"key": "session_hours", "value": 4.0}, None, "set")
        self.conn.commit()
        tm = self.actor["tm_own"]
        with self.assertRaises(actions.Forbidden) as e:             # the task-manager may review, not set settings
            self.mcp("proposal.review", tm, self.sess["A"], proposal_id=prop["id"], decision="approve")
        self.assertIn("as the reviewer", str(e.exception))
        rows = self.refused_rows(tm, "proposal.review")
        self.assertEqual((len(rows), rows[0]["target_id"]), (1, str(prop["id"])))
        self.assertEqual(len(self.alerts), 1)

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

    def test_the_run_summary_lists_a_refusal_as_refused_but_not_as_a_write(self):
        self.go_on()
        self.refuse(actor=self.sub(1), session=self.sess["AG"])
        row = self.refused_rows()[0]
        self.assertIn("REFUSED", proposals._write_line(row)["line"])
        run = {"id": 1, "kind": "subagent", "session_id": "s", "started_at": "a", "ended_at": "b"}
        s = proposals.summary(run, [row], [])
        self.assertEqual((s["write_count"], s["refused"]), (0, 1))
        self.assertFalse(proposals.nothing_to_deliver(s))            # the task-manager still hears of it
        self.assertIn("1 refused", proposals.summary_text(s))

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
                self.refuse(actor=self.sub(1), session=self.sess["AG"])
        self.assertIn("audited", str(e.exception))
        self.assertEqual(len(self.refused_rows(self.sub(1))), 1)
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
                      identity.TOKEN_VAR, "AFCLAUDE_IN_CONTAINER"):
                if k not in env:
                    os.environ.pop(k, None)
            try:
                rc = tasks.main(["--db", self.db, "machine", "register-host", "--file", self.file, *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = tasks.main(["--db", self.db, *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def nothing_left(self):
        self.assertEqual(store.host_machine_users(self.conn, socket.gethostname()), [])
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual([p for p in os.listdir(self.tmp.name) if p.startswith("machine.json")], [])

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

    def test_an_existing_group_writable_directory_is_fixed_so_the_token_is_not_ignored(self):
        os.chmod(self.tmp.name, 0o775)                                     # a umask 002 host's ~/.config/afclaude
        rc, out, err = self.run_cmd()
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(os.stat(self.tmp.name).st_mode & 0o022, 0)
        self.assertIn(f"note: {self.tmp.name} could be written by others; made it go-w", out)
        rec = json.load(open(self.file))
        w = identity.resolve(self.conn, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1"}, token_file=self.file)
        self.assertEqual(w.actor, f"machine:{rec['user_id']}")

    def test_the_cwd_the_home_or_a_symlinked_directory_is_not_chmodded_but_refused(self):
        """A bare --file name (the cwd), $HOME or a symlink with group write: refused naming the fix, mode kept."""
        target = os.path.join(self.tmp.name, "shared")
        os.mkdir(target)
        os.chmod(target, 0o775)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(target, link)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        for args, env, why in ((("--file", "machine.json"), {}, "the current directory"),
                               (("--file", os.path.join("..", "shared", "machine.json")), {}, "the current directory"),
                               (("--file", os.path.join(target, "machine.json")), {}, "the current directory"),
                               (("--file", os.path.join(target, "machine.json")), {"HOME": target}, "home directory"),
                               (("--file", os.path.join(link, "machine.json")), {}, f"a symlink (to {target})"),
                               (("--file", os.path.join(link, ".", "machine.json")), {}, f"a symlink (to {target})")):
            # the cwd cases run in the target; the home and symlink ones elsewhere, so their own check answers
            os.chdir(target if why == "the current directory" else self.tmp.name)
            rc, out, err = self.run_cmd(*args, env=env)
            os.chdir(cwd)
            self.assertNotEqual(rc, 0, args)
            self.assertIn(why, err + out)
            self.assertIn("--file <dir>/machine.json", err + out)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o775)
            self.assertEqual(os.listdir(target), [])
            self.assertEqual(store.host_machine_users(self.conn, socket.gethostname()), [])

    def test_a_directory_of_another_user_is_refused_before_a_code_is_spent(self):
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertIn("not owned by this user", err + out)
        self.nothing_left()

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

    def test_it_refuses_inside_the_container(self):
        rc, out, err = self.run_cmd(env={"AFCLAUDE_IN_CONTAINER": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("not the container", err + out)
        self.nothing_left()

    def test_a_failing_owner_grant_undoes_the_registration(self):
        real = tasks.act

        def act(conn, name, **kw):
            if name == "grant.set":
                raise ValueError("no grant today")
            return real(conn, name, **kw)
        with mock.patch.object(tasks, "act", act):
            rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertIn("no grant today", err + out)
        self.assertIn("undone", err + out)
        self.nothing_left()
        self.assertEqual(self.run_cmd()[0], 0)                      # and the command can simply be run again

    def test_a_failing_token_file_write_undoes_the_registration_after_the_grant(self):
        with mock.patch.object(tasks, "write_machine_file", side_effect=OSError("disk full")):
            rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertIn("disk full", err + out)
        self.assertIn("undone", err + out)
        self.nothing_left()
        self.assertEqual(self.run_cmd()[0], 0)

    def test_when_even_the_undo_fails_the_message_has_the_token_and_the_fix(self):
        real = tasks.act

        def act(conn, name, **kw):
            if name in ("grant.set", "machine.revoke"):
                raise ValueError(f"{name} down")
            return real(conn, name, **kw)
        with mock.patch.object(tasks, "act", act):
            rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        text = err + out
        self.assertIn("grant set", text)
        self.assertIn("machine revoke", text)
        self.assertRegex(text, r"token file: afcm_\S+")
        self.assertFalse(os.path.exists(self.file))

    def test_a_failing_alert_after_a_complete_registration_says_so(self):
        with mock.patch.object(tasks, "deliver", side_effect=ValueError("alert failed")):
            rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertIn("registration itself is complete", err + out)
        self.assertTrue(store.host_owner_machine(self.conn, socket.gethostname()))
        self.assertTrue(os.path.exists(self.file))

    def test_the_token_file_is_never_overwritten_by_a_second_writer(self):
        tasks.write_machine_file(self.file, {"token": "first"}, replace=False)
        with self.assertRaises(FileExistsError):
            tasks.write_machine_file(self.file, {"token": "second"}, replace=False)
        with open(self.file) as fh:
            self.assertEqual(json.load(fh), {"token": "first"})
        self.assertEqual(oct(os.stat(self.file).st_mode & 0o777), "0o600")
        self.assertEqual([p for p in os.listdir(self.tmp.name) if p.endswith(".tmp")], [])

    def test_roles_on_waits_a_night_after_the_registration(self):
        self.assertEqual(self.run_cmd()[0], 0)
        rc, out, err = self.main("roles", "mode", "--set", "on")
        self.assertNotEqual(rc, 0)
        self.assertIn("less than 20 h", err + out)
        self.assertIn("roles shadow --since", err + out)
        self.assertEqual(actions.get_setting(self.conn, "roles_enforce"), "shadow")
        with mock.patch.object(store, "_utcnow", return_value=store._utcnow() + timedelta(hours=21)):
            rc, out, err = self.main("roles", "mode", "--set", "on")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(actions.get_setting(self.conn, "roles_enforce"), "on")

    def test_roles_on_without_a_machine_user_keeps_the_settings_own_message(self):
        rc, out, err = self.main("roles", "mode", "--set", "on")
        self.assertNotEqual(rc, 0)
        self.assertIn("register this host's machine user", err + out)

    def test_a_blocked_token_file_is_refused_before_a_code_is_spent(self):
        open(self.file, "w").close()
        rc, out, err = self.run_cmd()
        self.assertNotEqual(rc, 0)
        self.assertEqual(store.host_machine_users(self.conn, socket.gethostname()), [])


if __name__ == "__main__":
    unittest.main()
