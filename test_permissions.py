#!/usr/bin/env python3
"""Tests for phase 5a A3 (docs/phase5a_plan.md §3.4, §5 A3): the permission matrix (permissions.py) and its shadow
mode in actions.perform. Temp DBs only.

  the matrix      every action x every principal (host owner, owner, manager, editor, viewer, none, machine with and
                  without a grant, task-manager of the project / of another / of AFClaude, agent, runner) gives
                  allowed / proposal / refused as the plan's classes say; an action with no entry fails
  rules           viewers read questions but can't answer; only managers and owners force; managers can't touch
                  global settings or the project rank; agents' direct writes and proposals; D-283's pause clear
  shadow          nothing new is refused or proposed: the same writes give the same results, audit rows and errors
                  with the matrix patched away; every would-be refusal is a roles_shadow row and a warning
  guard           roles_enforce=on is refused until this host's machine user holds an owner grant (D-330)
  reads           read_filter shows everything in shadow (logging what it would hide), narrows with `on`

    python3 -m unittest test_permissions
"""
import contextlib
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import failstop  # noqa: E402
import identity  # noqa: E402
import permissions  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

ORDER = ("host", "owner", "manager", "editor", "viewer", "none", "machine_granted", "machine_ungranted",
         "tm_own", "tm_other", "tm_afclaude", "agent", "runner")
WORD = {"A": "allowed", "P": "proposal", "R": "refused"}


def exp(string):
    """'AAARRR...' in ORDER -> {principal: outcome word}."""
    assert len(string) == len(ORDER), string
    return dict(zip(ORDER, (WORD[c] for c in string)))


#                 host owner mgr edit view none mach+ mach- tm-own tm-other tm-afc agent runner
OWNER_GLOBAL = exp("A A R R R R R R R R R R R".replace(" ", ""))
DEPLOY = exp("A A R R R R R R R R A R A".replace(" ", ""))
INTERNAL = exp("A R R R R R R R R R R R A".replace(" ", ""))
PROJECT_WRITE = exp("A A A A R R A R A R R P R".replace(" ", ""))        # an agent proposes
TASK_START = exp("A A A A R R A R A R R A R".replace(" ", ""))           # an agent starts a pending task directly
NO_AGENT_NO_TM = exp("A A A A R R A R R R R R R".replace(" ", ""))       # editors and up only (answers, the box)

CLASSES = [
    (("project.add project.move project.delete project.manager_launch session.decide session.clear rule.add "
      "rule.remove setting.set setting.reset window.set prompt.set prompt.reset user.rename user.revoke "
      "machine.rename machine.revoke machine.code auth.session_revoke setup.mint_code decision.confirm"
      ).split(), OWNER_GLOBAL),
    (("question.import setting.import install.stamp install.restored prompt.defaults_sync prompt_var.migrate "
      "decision.import decision.seed decision.activate_own").split(), DEPLOY),
    (("request.start request.finish driven.set session.cleared handoff.consume inbox.triage inbox.triage_hold "
      "inbox.hold inbox.deliver inbox.include inbox.fail typed.record log.prune runner.prune").split(), INTERNAL),
    (("task.add task.ask task.edit task.priority task.move task.cancel task.reopen task.block task.finish "
      "task.update question.close project.priority prompt_var.set prompt_var.reset notice.post doc.set "
      "decision.add decision.edit decision.delete decision.expire decision.keywords decision.topics "
      "decision.link").split(), PROJECT_WRITE),
    (["task.start"], TASK_START),
    (["task.answer"], exp("AAAARRARARRRR")),
    (["question.answer", "question.decide", "inbox.post", "inbox.retry", "inbox.dismiss", "notice.dismiss"],
     NO_AGENT_NO_TM),
    (["inbox.reply"], exp("AAAARRARARRRR")),
    (["proposal.review"], exp("AAAARRARARRRR")),                      # editors up and the project's task-manager
    (["project.edit"], exp("AAARRRRRARRRA")),
    (["request.add"], exp("AAARRRRRRRRRR")),                          # work_on_now: a forced run
    (["grant.set", "grant.remove"], exp("AAARRRRRRRRRR")),
    (["doc.append"], exp("AAAARRARARRPA")),
    (["doc.sync"], exp("AAAARRARARRRA")),
    (["user_model.set", "cost_model.set"], exp("AARRRRRRRRRRA")),
    (["automation.set"], exp("AARRRRRRRRARA")),                       # paused=False: a pause clear (D-283)
    (["automation.set_project"], exp("AARRRRRRARRRA")),
    (["machine.register", "machine.seen"], exp("AAAAAAAAAAAAA")),
    (["auth.session_login", "auth.session_rotate", "auth.session_logout"], exp("AAAAAARRRRRRR")),
]
SEPARATE = {"handoff.write", "grant.sync_groups", "setup.claim_code"}       # tested on their own below


class Base(unittest.TestCase):
    def setUp(self):
        actions._roles_logged.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.conn = store.connect(self.db, create=True)
        self.A = self.project("alpha", os.path.join(self.tmp.name, "a"))
        self.B = self.project("beta", os.path.join(self.tmp.name, "b"))
        self.OWN = self.project("afclaude", HERE)
        self.sess = {k: f"aaaaaaaa-0000-4000-8000-00000000000{i}" for i, k in enumerate(("A", "B", "OWN", "AG", "REV"), 1)}
        for name, pid in (("A", self.A), ("B", self.B), ("OWN", self.OWN)):
            self.cli("project.edit", project=pid, manager_session=self.sess[name])
        self.conn.execute("INSERT INTO driven_sessions(session_id, kind, started_at, project_id) "
                          "VALUES (?, 'request', '2026-11-01T00:00:00.000Z', ?)", (self.sess["AG"], self.A))
        self.conn.commit()
        u = {}
        for key, kind in (("owner", "person"), ("manager", "person"), ("editor", "person"), ("viewer", "person"),
                          ("none", "person"), ("machine_granted", "machine"), ("machine_ungranted", "machine")):
            u[key] = store.add_user(self.conn, kind, key, "test")["id"]
        self.conn.commit()
        self.cli("grant.set", user_id=u["owner"], scope="owner")
        for key, lvl in (("manager", "manager"), ("editor", "editor"), ("viewer", "viewer"),
                         ("machine_granted", "editor")):
            self.cli("grant.set", user_id=u[key], scope="project", project=self.A, level=lvl)
        self.users = u
        self.actor = {"host": "cli", "owner": f"user:{u['owner']}", "manager": f"user:{u['manager']}",
                      "editor": f"user:{u['editor']}", "viewer": f"user:{u['viewer']}", "none": f"user:{u['none']}",
                      "machine_granted": f"machine:{u['machine_granted']}",
                      "machine_ungranted": f"machine:{u['machine_ungranted']}",
                      "tm_own": f"task-manager:{self.sess['A']}", "tm_other": f"task-manager:{self.sess['B']}",
                      "tm_afclaude": f"task-manager:{self.sess['OWN']}", "agent": f"agent:{self.sess['AG']}",
                      "runner": "runner:keepalive"}
        self.task = self.cli("task.add", title="t", project="alpha")["id"]
        self.dec = self.cli("decision.add", title="d", topics=["x"], author="task-manager", project="alpha")["id"]
        self.notice = self.cli("notice.post", title="n", text="x", project=self.A)["id"]
        self.msg = self.cli("inbox.post", text="hi", project="alpha")["id"]
        self.prop = store.add_proposal(self.conn, self.A, None, self.actor["agent"], "doc.set", {"name": "x"}, None,
                                       "doc.set x")["id"]
        self.conn.commit()
        self.params = self.make_params()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def project(self, name, path):
        os.makedirs(path, exist_ok=True)
        return actions.perform(self.conn, "project.add", {"name": name, "path": path}, actor="cli", via="cli")["id"]

    def cli(self, _action, /, **params):
        return actions.perform(self.conn, _action, params, actor="cli", via="cli")

    def make_params(self):
        p = {}
        for n in actions.ACTIONS:
            fam = n.split(".")[0]
            p[n] = {"task": {"task_id": self.task}, "question": {"task_id": self.task},
                    "doc": {"project": "alpha", "name": "progress"}, "decision": {"decision": self.dec},
                    "project": {"project": "alpha"}, "grant": {"scope": "project", "project": "alpha"},
                    "prompt_var": {"project": "alpha"}, "proposal": {"proposal_id": self.prop}}.get(fam, {})
        p["task.add"] = p["task.ask"] = {"project": "alpha"}
        p["decision.add"] = {"project": "alpha"}
        p["notice.post"] = {"project": self.A}
        p["notice.dismiss"] = {"notice_id": self.notice}
        p["request.add"] = {"kind": "work_on_now", "target": "alpha"}
        for n in ("inbox.post",):
            p[n] = {"project": "alpha"}
        for n in ("inbox.reply", "inbox.retry", "inbox.dismiss"):
            p[n] = {"message_id": self.msg}
        p["automation.set"] = {"paused": False}
        p["automation.set_project"] = {"project": "alpha", "paused": False}
        return p

    def check(self, _action, who, **over):
        return permissions.check(self.conn, _action, dict(self.params.get(_action, {}), **over), self.actor[who])

    def outcome(self, _action, who, **over):
        return self.check(_action, who, **over).outcome


class Matrix(Base):
    def test_every_action_has_an_entry_and_every_entry_an_action(self):
        self.assertEqual(permissions.mismatch(actions.ACTIONS), [])

    def test_a_new_action_without_a_perm_fails_and_is_refused(self):
        spec = actions.ACTIONS["task.add"]
        with mock.patch.dict(actions.ACTIONS, {"x.new": spec}):
            self.assertEqual(permissions.mismatch(actions.ACTIONS), ["x.new"])
        with mock.patch.dict(permissions.MATRIX, {"x.new": permissions.Perm()}, clear=False):
            self.assertEqual(permissions.mismatch(actions.ACTIONS), ["x.new"])      # an entry with no action
        v = permissions.check(self.conn, "x.new", {}, "user:1")
        self.assertEqual((v.outcome, "deny by default" in v.reason), ("refused", True))

    def test_every_action_is_in_exactly_one_class(self):
        named = [n for names, _ in CLASSES for n in names]
        self.assertEqual(len(named), len(set(named)))
        self.assertEqual(set(named) | SEPARATE, set(actions.ACTIONS))

    def test_action_by_principal(self):
        bad = []
        for names, want in CLASSES:
            for name in names:
                for who in ORDER:
                    got = self.outcome(name, who)
                    if got != want[who]:
                        bad.append(f"{name} by {who}: {got} (wanted {want[who]}): {self.check(name, who).reason}")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_a_revoked_user_and_an_unknown_actor_are_refused(self):
        self.cli("user.revoke", user_id=self.users["editor"])
        self.assertEqual(self.outcome("task.add", "editor"), "refused")
        for actor in ("mcp:abc", "user:999", "agent:unknown"):
            self.assertEqual(permissions.check(self.conn, "task.add", {"project": "alpha"}, actor).outcome, "refused")

    def test_legacy_runner_actors_are_runners(self):
        for actor in ("keepalive", "dispatcher", "runner"):
            self.assertEqual(permissions.check(self.conn, "driven.set", {}, actor).outcome, "allowed")
        self.assertEqual(permissions.check(self.conn, "setting.set", {}, "keepalive").outcome, "refused")


class Rules(Base):
    def test_viewers_see_questions_but_cannot_answer(self):
        q = self.cli("task.ask", question="which?", project="alpha")["id"]
        for name in ("task.answer", "question.answer", "question.decide"):
            self.assertEqual(self.outcome(name, "viewer", task_id=q), "refused")
            self.assertEqual(self.outcome(name, "editor", task_id=q), "allowed")
        seen = actions.read_filter(self.conn, self.actor["viewer"], None, "cli", "tasks",
                                   store.list_tasks(self.conn, kind=store.QUESTION_KIND), lambda t: t["project_id"])
        self.assertEqual([t["id"] for t in seen], [q])
        vis = permissions.visible_projects(self.conn, self.actor["viewer"])
        self.assertEqual(vis, frozenset({self.A}))

    def test_only_managers_and_owners_force(self):
        for who, want in (("owner", "allowed"), ("manager", "allowed"), ("editor", "refused"),
                          ("viewer", "refused"), ("machine_granted", "refused")):
            self.assertEqual(self.outcome("request.add", who), want, who)
        # continue_now (a stalled session) and review_now are global: a manager of the project is not enough
        for kind, target in (("continue_now", "abc"), ("review_now", None)):
            self.assertEqual(self.outcome("request.add", "manager", kind=kind, target=target), "refused")
            self.assertEqual(self.outcome("request.add", "owner", kind=kind, target=target), "allowed")
        # a manager of another project can't force this one
        self.cli("grant.set", user_id=self.users["none"], scope="project", project=self.B, level="manager")
        self.assertEqual(self.outcome("request.add", "none"), "refused")

    def test_managers_cannot_touch_global_settings_or_project_rank(self):
        for name in ("setting.set", "setting.reset", "window.set", "project.move", "project.add", "prompt.set",
                     "rule.add", "session.decide", "user.revoke", "machine.code", "automation.set",
                     "install.stamp", "project.delete"):
            self.assertEqual(self.outcome(name, "manager"), "refused", name)
        self.assertEqual(self.outcome("project.edit", "manager"), "allowed")        # a project's own fields
        self.assertEqual(self.outcome("project.priority", "editor"), "allowed")     # stage priorities: editors

    def test_managers_grant_editor_and_viewer_on_their_project_only(self):
        self.assertEqual(self.outcome("grant.set", "manager"), "allowed")
        self.assertEqual(self.outcome("grant.set", "manager", scope="owner", project=None), "refused")
        self.assertEqual(self.outcome("grant.set", "manager", project="beta"), "refused")
        self.assertEqual(self.outcome("grant.set", "editor"), "refused")

    def test_a_task_moved_to_another_project_needs_both(self):
        self.assertEqual(self.outcome("task.edit", "editor", project="alpha"), "allowed")
        self.assertEqual(self.outcome("task.edit", "editor", project="beta"), "refused")
        self.assertEqual(self.outcome("task.edit", "editor", project="brand-new"), "refused")   # creates a project

    def test_task_with_no_project_is_global(self):
        t = self.cli("task.add", title="loose")["id"]
        self.assertEqual(self.outcome("task.edit", "manager", task_id=t), "refused")
        self.assertEqual(self.outcome("task.edit", "owner", task_id=t), "allowed")
        self.assertEqual(self.outcome("task.edit", "agent", task_id=t), "refused")

    def test_install_wide_decisions_belong_to_afclaudes_project(self):
        d = self.cli("decision.add", title="wide", topics=["x"], author="task-manager")["id"]
        self.assertEqual(self.outcome("decision.edit", "tm_afclaude", decision=d), "allowed")
        self.assertEqual(self.outcome("decision.edit", "tm_own", decision=d), "refused")
        self.assertEqual(self.outcome("decision.edit", "editor", decision=d), "refused")
        self.assertEqual(self.outcome("decision.add", "tm_afclaude", project=None), "allowed")
        self.assertEqual(self.outcome("decision.edit", "owner", decision=d), "allowed")
        self.assertEqual(self.outcome("decision.edit", "tm_other", decision=d), "refused")

    def test_install_wide_decisions_are_owner_only_for_humans(self):
        """D-184 / plan §0 "Global = owner only": a manager or editor of AFClaude's own project doesn't write
        install-wide decisions; AFClaude's task-manager does, and an agent of that project proposes."""
        mgr = store.add_user(self.conn, "person", "afc-manager", "test")["id"]
        self.cli("grant.set", user_id=mgr, scope="project", project=self.OWN, level="manager")
        ed = store.add_user(self.conn, "person", "afc-editor", "test")["id"]
        self.cli("grant.set", user_id=ed, scope="project", project=self.OWN, level="editor")
        d = self.cli("decision.add", title="wide", topics=["x"], author="task-manager")["id"]
        for who in (f"user:{mgr}", f"user:{ed}"):
            for act, over in (("decision.edit", {"decision": d}), ("decision.add", {"project": None})):
                v = permissions.check(self.conn, act, dict(self.params[act], **over), who)
                self.assertEqual(v.outcome, "refused", (who, act))
                self.assertIn("owner only", v.reason)
            # the same user still writes the project's own decisions
            own = self.cli("decision.add", title="own", topics=["x"], author="task-manager", project=self.OWN)["id"]
            self.assertEqual(permissions.check(self.conn, "decision.edit", {"decision": own}, who).outcome, "allowed")
        self.conn.execute("INSERT INTO driven_sessions(session_id, kind, started_at, project_id) "
                          "VALUES (?, 'request', '2026-11-01T00:00:00.000Z', ?)", (self.sess["REV"], self.OWN))
        self.conn.commit()
        self.assertEqual(permissions.check(self.conn, "decision.edit", {"decision": d},
                                           f"agent:{self.sess['REV']}").outcome, "proposal")
        self.assertEqual(self.outcome("decision.edit", "agent", decision=d), "refused")       # works in alpha

    def test_pause_clear_is_the_task_managers_one_owner_only_exception(self):
        self.assertEqual(self.outcome("automation.set", "tm_afclaude", paused=True), "refused")   # only a clear
        self.assertEqual(self.outcome("automation.set", "tm_own", paused=False), "refused")       # install-wide
        self.assertEqual(self.outcome("automation.set_project", "tm_own", paused=False), "allowed")
        self.assertEqual(self.outcome("automation.set_project", "tm_own", paused=True), "refused")
        self.assertEqual(self.outcome("automation.set_project", "tm_other", paused=False), "refused")

    def test_agent_direct_writes_and_proposals(self):
        t = self.task
        self.assertEqual(self.outcome("task.finish", "agent"), "proposal")           # not assigned to it
        self.cli("task.start", task_id=t, session=self.sess["AG"])
        for name in ("task.update", "task.block", "task.finish"):
            self.assertEqual(self.outcome(name, "agent"), "allowed", name)           # its own task
        other = self.cli("task.add", title="o", project="beta")["id"]
        self.assertEqual(self.outcome("task.start", "agent", task_id=other), "refused")   # not its project
        self.assertEqual(self.outcome("task.add", "agent", project="beta"), "refused")
        self.assertEqual(self.outcome("doc.append", "agent", name="alerts"), "allowed")
        self.assertEqual(self.outcome("doc.append", "agent", name="progress"), "proposal")
        self.assertEqual(self.outcome("doc.append", "agent", name="alerts", project="beta"), "refused")
        self.assertEqual(self.outcome("decision.add", "agent"), "proposal")
        self.assertEqual(self.outcome("setting.set", "agent"), "refused")
        # a subagent of the task-manager works in the task-manager's project
        sub = permissions.check(self.conn, "task.edit", {"task_id": t}, f"agent:{self.sess['A']}:sub1")
        self.assertEqual(sub.outcome, "proposal")

    def test_handoff_is_the_agents_and_task_managers_own_only(self):
        for who, sid, want in (("agent", self.sess["AG"], "allowed"), ("agent", self.sess["A"], "refused"),
                               ("tm_own", self.sess["A"], "allowed"), ("tm_own", self.sess["B"], "refused"),
                               ("editor", self.sess["AG"], "allowed"), ("viewer", self.sess["AG"], "refused")):
            self.assertEqual(self.outcome("handoff.write", who, session_id=sid, project="alpha"), want, (who, sid))

    def test_usage_models_are_the_review_sessions_direct_write(self):                  # D-291
        self.conn.execute("INSERT INTO driven_sessions(session_id, kind, started_at) "
                          "VALUES (?, 'review', '2026-11-01T00:00:00.000Z')", (self.sess["REV"],))
        self.conn.commit()
        for name in ("user_model.set", "cost_model.set"):
            self.assertEqual(permissions.check(self.conn, name, {}, f"agent:{self.sess['REV']}").outcome, "allowed")
            self.assertEqual(self.outcome(name, "agent"), "refused")

    def test_login_flow_and_group_sync(self):
        own = self.users["editor"]
        self.assertEqual(self.outcome("grant.sync_groups", "editor", user_id=own), "allowed")
        self.assertEqual(self.outcome("grant.sync_groups", "editor", user_id=self.users["owner"]), "refused")
        self.assertEqual(self.outcome("grant.sync_groups", "host", user_id=own), "allowed")
        self.assertEqual(self.outcome("grant.sync_groups", "machine_granted", user_id=own), "refused")
        self.assertEqual(self.outcome("grant.sync_groups", "agent", user_id=own), "refused")
        self.assertEqual(permissions.check(self.conn, "setup.claim_code", {}, "setup").outcome, "allowed")
        self.assertEqual(permissions.check(self.conn, "setup.mint_code", {}, "setup").outcome, "refused")
        self.assertEqual(self.outcome("setup.claim_code", "editor"), "refused")

    def test_an_unfound_target_is_left_to_the_action(self):
        v = self.check("task.edit", "viewer", task_id=99999)
        self.assertEqual(v.outcome, "allowed")
        self.assertIn("target not found", v.reason)

    def test_afclaude_project_is_the_one_at_this_repository(self):
        self.assertEqual(permissions.afclaude_project_id(self.conn), self.OWN)


class Shadow(Base):
    def shadow_rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM roles_shadow ORDER BY id")]

    def test_a_refused_write_goes_on_and_is_logged(self):
        m = self.users["machine_ungranted"]
        with self.assertLogs("afclaude.roles", "WARNING") as lg:
            r = actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0},
                                actor=f"machine:{m}", via="mcp")
        self.assertEqual(r["value"], 4.0)                                        # the write happened
        rows = self.shadow_rows()
        self.assertEqual([(x["action"], x["outcome"], x["actor"]) for x in rows],
                         [("setting.set", "refused", f"machine:{m}")])
        self.assertIn("would be refused", lg.output[0])
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), 4.0)

    def test_an_agent_write_that_would_be_a_proposal_is_applied(self):
        r = actions.perform(self.conn, "task.add", {"title": "from an agent", "project": "alpha"},
                            actor=self.actor["agent"], via="mcp", autonomous=True)
        self.assertEqual(r["title"], "from an agent")
        self.assertEqual([x["outcome"] for x in self.shadow_rows()], ["proposal"])

    def test_the_old_rule_still_refuses_and_the_shadow_row_stays(self):
        with self.assertRaises(actions.Forbidden) as e, self.assertLogs("afclaude.roles", "WARNING"):
            actions.perform(self.conn, "rule.add", {"scope": "project", "match": "/x", "decision": "ignore"},
                            actor=self.actor["agent"], via="mcp", autonomous=True)
        self.assertIn("not allowed for an autonomous", str(e.exception))
        self.assertEqual([x["action"] for x in self.shadow_rows()], ["rule.add"])    # outlived the rollback
        self.assertEqual(store.list_rules(self.conn), [])

    def test_a_crashing_check_never_fails_a_shadow_write_and_is_logged(self):
        with mock.patch.object(permissions, "check", side_effect=KeyError("boom")), \
                self.assertLogs("afclaude.roles", "WARNING") as lg:
            r = self.cli("task.add", title="still written", project="alpha")
        self.assertEqual(r["title"], "still written")
        self.assertIn("check itself failed", lg.output[0])
        self.assertEqual([x["outcome"] for x in self.shadow_rows()], ["refused"])

    def test_allowed_writes_leave_no_shadow_row(self):
        self.cli("task.add", title="x", project="alpha")
        actions.perform(self.conn, "task.add", {"title": "y", "project": "alpha"}, actor=self.actor["editor"],
                        via="mcp")
        self.assertEqual(self.shadow_rows(), [])

    def test_a_replayed_idempotency_key_is_not_checked_again(self):
        kw = dict(actor=self.actor["machine_ungranted"], via="mcp", key="replay-key-1")
        actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 3.0}, **kw)
        actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 3.0}, **kw)
        self.assertEqual(len(self.shadow_rows()), 1)

    def test_the_warning_is_once_per_actor_action_and_hour(self):
        actions._roles_logged.clear()
        m = f"machine:{self.users['machine_ungranted']}"
        with self.assertLogs("afclaude.roles", "WARNING") as lg:
            for v in (3.0, 4.0, 5.0):
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": v}, actor=m, via="mcp")
        self.assertEqual(len(lg.output), 1)
        self.assertEqual(len(self.shadow_rows()), 3)             # the table keeps every one

    def test_a_failing_shadow_insert_is_an_error_and_an_alert_saying_the_write_went_through(self):
        m = f"machine:{self.users['machine_ungranted']}"
        with mock.patch.object(actions, "roles_shadow_add", side_effect=RuntimeError("boom")), \
                mock.patch.object(failstop, "fail") as fail:
            with self.assertRaises(actions.RolesFailure) as e:
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 2.0}, actor=m, via="mcp")
        self.assertIn("WAS carried out", str(e.exception))
        self.assertIn("boom", str(e.exception))
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), 2.0)      # the write is committed
        fail.assert_called_once()
        self.assertEqual(fail.call_args.args[:2], ("roles", "the shadow log could not be written"))
        self.assertIn("WAS carried out and is committed", fail.call_args.args[2])

    def test_a_failing_shadow_insert_after_a_refusal_says_the_action_was_not_carried_out(self):
        actions._roles_logged.clear()
        m = f"machine:{self.users['machine_ungranted']}"
        with mock.patch.object(actions, "roles_shadow_add", side_effect=RuntimeError("boom")), \
                mock.patch.object(failstop, "fail") as fail:
            with self.assertRaises(actions.RolesFailure) as e:
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": -5.0}, actor=m, via="mcp")
        self.assertIsInstance(e.exception.__cause__, ValueError)                   # the old rule's own error, chained
        self.assertIn("NOT carried out", fail.call_args.args[2])

    def test_the_failure_alert_goes_through_failstop_once_per_cause(self):
        """Real failstop: pause set (actor runner:roles), one alert, the same cause not alerted again."""
        actions._roles_logged.clear()
        m = f"machine:{self.users['machine_ungranted']}"
        alerts = []
        with mock.patch.object(actions, "roles_shadow_add", side_effect=RuntimeError("boom")), \
                mock.patch.object(failstop, "_notify", side_effect=lambda s, b: alerts.append((s, b))), \
                mock.patch.object(failstop, "_stderr_log"), \
                mock.patch.object(failstop, "_current", return_value={"paused": False}), \
                mock.patch.object(failstop, "_set_pause", return_value=None) as pause, \
                self.assertLogs("afclaude.roles", "ERROR"):
            for v in (3.0, 4.0, 5.0):
                with self.assertRaises(actions.RolesFailure):
                    actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": v}, actor=m, via="mcp")
        self.assertEqual(len(alerts), 1)
        self.assertIn("the shadow log could not be written", alerts[0][0])
        self.assertIn("WAS carried out", alerts[0][1])
        pause.assert_called_once()                                   # the install-wide pause, reason names the cause
        self.assertEqual(pause.call_args.args[0], "roles")
        self.assertIn("the shadow log could not be written", pause.call_args.args[1])

    def test_no_write_changes_outcome_with_the_matrix_patched_away(self):
        """The same scenarios with and without the matrix: equal results, errors and audit rows."""
        agent, m = self.actor["agent"], f"machine:{self.users['machine_ungranted']}"
        scenarios = [("task.add", {"title": "a", "project": "alpha"}, agent, True),
                     ("task.add", {"title": "b", "project": "beta"}, agent, True),
                     ("rule.add", {"scope": "project", "match": "/x", "decision": "ignore"}, agent, True),
                     ("setting.set", {"key": "session_hours", "value": 4.0}, m, False),
                     ("project.move", {"project": "beta", "rank": 1}, self.actor["editor"], False),
                     ("task.update", {"task_id": self.task, "note": "n"}, self.actor["viewer"], False),
                     ("doc.append", {"project": "alpha", "name": "progress", "text": "x\n"}, agent, True),
                     ("task.finish", {"task_id": 424242}, self.actor["tm_other"], False),
                     ("decision.add", {"title": "z", "topics": ["x"], "author": "task-manager"}, agent, True)]

        def run(patched):
            out = []
            ctx = mock.patch.object(permissions, "check", return_value=permissions.Verdict("allowed", "x")) \
                if patched else contextlib.nullcontext()
            with ctx, contextlib.redirect_stderr(io.StringIO()):
                for name, params, actor, auto in scenarios:
                    try:
                        r = actions.perform(self.conn, name, params, actor=actor, via="mcp", autonomous=auto)
                        out.append(("ok", json.dumps(r, sort_keys=True, default=str)))
                    except Exception as e:  # noqa: BLE001 - the comparison is the point
                        out.append((type(e).__name__, str(e)))
            return out

        def audit():
            return [(r["actor"], r["action"], r["target_id"], r["before"], r["after"])
                    for r in reversed(store.audit_log(self.conn, limit=1000))]

        before = len(audit())
        with_matrix = run(False)
        a1 = audit()[before:]
        self.tearDown()
        self.setUp()
        before = len(audit())
        without = run(True)
        a2 = audit()[before:]
        scrub = lambda xs: json.dumps(xs, default=str).replace(  # noqa: E731 - ids/times differ per DB
            "T", "").replace("Z", "")
        self.assertEqual([x[0] for x in with_matrix], [x[0] for x in without])
        self.assertEqual([x[1] for x in with_matrix if x[0] != "ok"], [x[1] for x in without if x[0] != "ok"])
        self.assertEqual([(a[0], a[1]) for a in a1], [(a[0], a[1]) for a in a2])
        self.assertTrue(any(x[0] != "ok" for x in with_matrix) and any(x[0] == "ok" for x in with_matrix))
        self.assertEqual(len(scrub(a1)), len(scrub(a2)))

    def test_shadow_list_groups_by_day_actor_action_outcome_reason(self):
        m = f"machine:{self.users['machine_ungranted']}"
        with self.assertLogs("afclaude.roles", "WARNING"):
            for v in (3.0, 4.0):
                actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": v}, actor=m, via="mcp")
            actions.perform(self.conn, "task.add", {"title": "p", "project": "alpha"}, actor=self.actor["agent"],
                            via="mcp", autonomous=True)
        rows = store.roles_shadow_list(self.conn)
        by = {(r["action"], r["outcome"]): r["n"] for r in rows}
        self.assertEqual(by, {("setting.set", "refused"): 2, ("task.add", "proposal"): 1})
        today = rows[0]["day"]
        self.assertEqual(len(store.roles_shadow_list(self.conn, day=today)), 2)
        self.assertEqual(store.roles_shadow_list(self.conn, day="2000-01-01"), [])

    def test_the_cli_prints_the_daily_list(self):
        m = f"machine:{self.users['machine_ungranted']}"
        with self.assertLogs("afclaude.roles", "WARNING"):
            actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0}, actor=m, via="mcp")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tasks.main(["--db", self.db, "roles", "shadow"]), 0)
        self.assertIn("setting.set", out.getvalue())
        self.assertIn("refused", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tasks.main(["--db", self.db, "roles", "mode"])
        self.assertIn("shadow", out.getvalue())


class Guard(Base):
    def register_host_machine(self, owner=True, revoke=False):
        code = roles.new_registration_code()
        self.cli("machine.code", code_sha256=roles.code_sha256(code))
        r = actions.register_machine(self.conn, code, socket.gethostname(), actor="cli", via="cli")
        if owner:
            self.cli("grant.set", user_id=r["user_id"], scope="owner")
        if revoke:
            self.cli("machine.revoke", user_id=r["user_id"])
        return r

    def test_on_is_refused_until_the_host_has_a_machine_owner(self):
        with self.assertRaises(ValueError) as e:
            self.cli("setting.set", key="roles_enforce", value="on")
        self.assertIn("D-330", str(e.exception))
        self.assertEqual(actions.roles_mode(self.conn), "shadow")
        self.register_host_machine(owner=False)                    # a machine without the owner grant: still no
        with self.assertRaises(ValueError):
            self.cli("setting.set", key="roles_enforce", value="on")

    def test_a_machine_owner_of_another_host_does_not_count(self):
        code = roles.new_registration_code()
        self.cli("machine.code", code_sha256=roles.code_sha256(code))
        r = actions.register_machine(self.conn, code, "some-other-host", actor="cli", via="cli")
        self.cli("grant.set", user_id=r["user_id"], scope="owner")
        with self.assertRaises(ValueError):
            self.cli("setting.set", key="roles_enforce", value="on")

    def test_a_revoked_host_machine_does_not_count(self):
        self.register_host_machine(revoke=True)
        with self.assertRaises(ValueError):
            self.cli("setting.set", key="roles_enforce", value="on")

    def test_with_a_host_machine_owner_on_is_allowed_and_refuses(self):
        r = self.register_host_machine()
        self.cli("setting.set", key="roles_enforce", value="on")
        self.assertEqual(actions.roles_mode(self.conn), "on")
        with self.assertRaises(actions.Forbidden) as e:
            actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0},
                            actor=self.actor["machine_ungranted"], via="mcp")
        self.assertIn("refused", str(e.exception))
        with self.assertRaises(actions.Proposed) as e:                     # A4: stored, not refused
            actions.perform(self.conn, "task.add", {"title": "x", "project": "alpha"}, actor=self.actor["agent"],
                            via="mcp", autonomous=True)
        self.assertIn("proposed as P-", str(e.exception))
        actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0},
                        actor=f"machine:{r['user_id']}", via="mcp")                       # the owner machine: fine
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), 4.0)

    def test_on_makes_an_identity_conflict_a_refusal(self):
        self.register_host_machine()
        self.cli("setting.set", key="roles_enforce", value="on")
        self.assertRaises(identity.IdentityConflict, identity.conflict, "x", self.conn)
        self.cli("setting.set", key="roles_enforce", value="shadow")
        with self.assertLogs("afclaude.identity", "WARNING"):
            identity.conflict("x again", self.conn)

    def bad_mode(self, how):
        """An unusable saved roles_enforce: an unknown value, or `on` after the host's machine owner went away."""
        if how == "unknown":
            store.put_setting(self.conn, "roles_enforce", "maybe", "test")
            self.conn.commit()
            return "maybe"
        r = self.register_host_machine()
        self.cli("setting.set", key="roles_enforce", value="on")
        self.cli("machine.revoke", user_id=r["user_id"])
        return "D-330"

    def test_an_unusable_saved_mode_raises_and_alerts_once_per_cause(self):
        for how in ("unknown", "gone"):
            with self.subTest(how):
                self.setUp()
                needle = self.bad_mode(how)
                with mock.patch.object(failstop, "fail") as fail, self.assertRaises(actions.RolesFailure) as e:
                    actions.roles_mode(self.conn)
                self.assertIn(needle, str(e.exception))
                with mock.patch.object(failstop, "fail") as fail2, self.assertRaises(actions.RolesFailure):
                    actions.roles_mode(self.conn)                 # the same cause again: raised, not alerted again
                fail.assert_called_once()
                self.assertEqual(fail.call_args.args[:2], ("roles", "the saved roles_enforce setting can't be used"))
                self.assertIn(needle, fail.call_args.args[2])
                fail2.assert_not_called()
                self.assertIn(needle, actions.roles_mode_problem(self.conn))

    def test_a_write_the_matrix_would_not_allow_fails_while_the_mode_is_unusable(self):
        self.bad_mode("unknown")
        with mock.patch.object(failstop, "fail") as fail, self.assertRaises(actions.RolesFailure):
            actions.perform(self.conn, "setting.set", {"key": "session_hours", "value": 4.0},
                            actor=self.actor["machine_ungranted"], via="mcp")
        fail.assert_called_once()                                  # reported after the transaction
        self.assertNotEqual(actions.get_setting(self.conn, "session_hours"), 4.0)
        self.cli("setting.set", key="session_hours", value=3.0)    # an allowed write does not consult the mode
        self.assertEqual(actions.get_setting(self.conn, "session_hours"), 3.0)

    def test_the_owner_can_repair_an_unusable_mode_but_only_as_the_matrix_allows(self):
        for how in ("unknown", "gone"):
            with self.subTest(how):
                self.setUp()
                self.bad_mode(how)
                with mock.patch.object(failstop, "fail"):
                    with self.assertRaises(actions.Forbidden):    # a machine user the matrix refuses: no repair
                        actions.perform(self.conn, "setting.set", {"key": "roles_enforce", "value": "shadow"},
                                        actor=self.actor["machine_ungranted"], via="mcp")
                    self.cli("setting.set", key="roles_enforce", value="shadow")        # the owner repairs it
                self.assertEqual(actions.roles_mode(self.conn), "shadow")
                self.assertIsNone(actions.roles_mode_problem(self.conn))
        self.setUp()
        self.bad_mode("unknown")
        with mock.patch.object(failstop, "fail"):
            self.cli("setting.reset", key="roles_enforce")                              # reset repairs it too
        self.assertEqual(actions.roles_mode(self.conn), "shadow")

    def test_roles_mode_cli_shows_the_problem_and_repairs(self):
        self.bad_mode("unknown")
        def run(*a):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                tasks.main(["--db", self.db, "roles", "mode", *a])
            return out.getvalue()
        self.assertIn("UNUSABLE", run())
        self.assertIn("maybe", run())
        self.assertIsNone(json.loads(run("--json"))["roles_enforce"])
        self.assertIn("roles_enforce: shadow", run("--set", "shadow"))
        self.assertEqual(json.loads(run("--json")), {"roles_enforce": "shadow", "problem": None})


class Reads(Base):
    def rows(self):
        return store.list_tasks(self.conn, status=None)

    def filtered(self, who, rows=None):
        return actions.read_filter(self.conn, self.actor[who], None, "mcp", "tasks",
                                   self.rows() if rows is None else rows, lambda t: t["project_id"])

    def test_shadow_shows_everything_and_logs_what_it_would_hide(self):
        self.cli("task.add", title="b-task", project="beta")
        self.cli("task.add", title="loose")
        all_rows = self.rows()
        self.assertEqual(len(self.filtered("machine_ungranted", all_rows)), len(all_rows))
        self.assertEqual(len(self.filtered("editor", all_rows)), len(all_rows))
        got = [(r["actor"], r["action"], r["outcome"]) for r in self.conn.execute("SELECT * FROM roles_shadow")]
        self.assertEqual(got, [(self.actor["machine_ungranted"], "read:tasks", "filtered"),
                               (self.actor["editor"], "read:tasks", "filtered")])

    def test_on_narrows_the_listing(self):
        self.cli("task.add", title="b-task", project="beta")
        self.cli("task.add", title="loose")
        with mock.patch.object(actions, "roles_mode", return_value="on"):
            ids = lambda who: {t["title"] for t in self.filtered(who)}  # noqa: E731
            self.assertEqual(ids("editor"), {"t"})
            self.assertEqual(ids("machine_ungranted"), set())
            self.assertEqual(ids("tm_other"), {"b-task"})
            self.assertEqual(ids("agent"), {"t"})
            self.assertEqual(ids("owner"), {"t", "b-task", "loose"})
            self.assertEqual(ids("host"), {"t", "b-task", "loose"})
            self.assertEqual(ids("runner"), {"t", "b-task", "loose"})

    def test_install_wide_decisions_stay_public(self):
        d = self.cli("decision.add", title="wide", topics=["x"], author="task-manager")["id"]
        rows = store.find_decision_entries(self.conn, ["x"], None)
        with mock.patch.object(actions, "roles_mode", return_value="on"):
            got = actions.read_filter(self.conn, self.actor["machine_ungranted"], None, "mcp", "decisions", rows,
                                      lambda x: x.get("project_id"), public_global=True)
        self.assertEqual([x["id"] for x in got], [d])

    def test_a_failing_filter_is_an_error_and_an_alert_not_an_unfiltered_listing(self):
        rows = self.rows()
        actions._roles_logged.clear()
        for mode in ("shadow", "on"):
            with mock.patch.object(actions, "roles_mode", return_value=mode), \
                    mock.patch.object(permissions, "visible_projects", side_effect=RuntimeError("boom")), \
                    mock.patch.object(failstop, "fail") as fail:
                with self.assertRaises(actions.RolesFailure) as e:
                    self.filtered("editor", rows)
                with self.assertRaises(actions.RolesFailure):
                    self.filtered("editor", rows)
            self.assertIn("boom", str(e.exception))
            self.assertIn("not returned", str(e.exception))
            self.assertEqual(fail.call_count, 1 if mode == "shadow" else 0)    # once an hour per cause
        with mock.patch.object(permissions, "visible_projects", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(failstop, "fail") as fail:
            with self.assertRaises(sqlite3.OperationalError):             # the DB error path, not a roles failure
                self.filtered("editor", rows)
        fail.assert_not_called()

    def test_a_bad_project_of_raises_and_alerts(self):
        rows = self.rows()
        actions._roles_logged.clear()
        with mock.patch.object(failstop, "fail") as fail, self.assertRaises(actions.RolesFailure):
            actions.read_filter(self.conn, self.actor["editor"], None, "mcp", "tasks", rows, lambda t: t["nope"])
        self.assertEqual(fail.call_args.args[:2], ("roles", "the read filter failed"))

    def test_an_unusable_mode_fails_a_listing_that_would_hide_rows(self):
        self.cli("task.add", title="b-task", project="beta")
        rows = self.rows()
        store.put_setting(self.conn, "roles_enforce", "maybe", "test")
        self.conn.commit()
        actions._roles_logged.clear()
        with mock.patch.object(failstop, "fail") as fail, self.assertRaises(actions.RolesFailure):
            self.filtered("editor", rows)
        self.assertEqual(fail.call_args.args[:2], ("roles", "the saved roles_enforce setting can't be used"))
        self.assertEqual(self.filtered("owner", rows), rows)           # nothing to hide: the mode isn't consulted

    def test_a_read_hit_writes_one_row_per_actor_listing_and_hour(self):
        self.cli("task.add", title="b-task", project="beta")
        rows = self.rows()
        for _ in range(3):
            self.filtered("editor", rows)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles_shadow").fetchone()[0], 1)
        self.filtered("viewer", rows)                                # another actor: its own first hit
        with mock.patch.object(store, "now_iso", return_value="2999-01-01T00:00:00.000Z"):
            self.filtered("editor", rows)                            # the next hour
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles_shadow").fetchone()[0], 3)

    def test_a_read_hit_never_retries_or_waits_for_the_write_lock(self):
        import time
        self.cli("task.add", title="b-task", project="beta")
        rows = self.rows()
        actions._roles_logged.clear()
        other = store.connect(self.db)
        other.execute("BEGIN IMMEDIATE")                             # somebody holds the write lock
        try:
            with mock.patch.object(store, "retrying", side_effect=AssertionError("retried")), \
                    mock.patch.object(actions, "SHADOW_BUSY_MS", 50), \
                    mock.patch.object(failstop, "fail") as fail:
                t0 = time.monotonic()
                with self.assertRaises(actions.RolesFailure) as e:
                    self.filtered("editor", rows)
                took = time.monotonic() - t0
        finally:
            other.rollback()
            other.close()
        self.assertLess(took, 3)
        self.assertIn("locked", str(e.exception))
        self.assertIn("not returned", str(e.exception))
        fail.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles_shadow").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], store.BUSY_TIMEOUT * 1000)

    def test_a_revoked_user_sees_nothing(self):
        self.cli("user.revoke", user_id=self.users["editor"])
        self.assertEqual(permissions.visible_projects(self.conn, self.actor["editor"]), frozenset())


if __name__ == "__main__":
    unittest.main()
