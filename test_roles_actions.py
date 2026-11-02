#!/usr/bin/env python3
"""Tests for phase 5a A1 (docs/phase5a_plan.md §3.2, §3.5, §3.6, §5 A1): the additive schema (users, grants,
machine_tokens, machine_registrations, agent_proposals, agent_runs, tasks.blocked_by / forwarded_at), the
grant.* / user.* / machine.* actions (audited through actions.perform), the last-owner guard, group sync,
registration codes and tokens (only hashes stored), the two-hostname alert, and `tasks.py machine|grant`.
Temp DBs only. No caller passes a principal yet and nothing is refused by a matrix: the only refusals here
are the D-192 grant rule and the D-183 guard.

    python3 -m unittest test_roles_actions
"""
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import notify  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

UTC = timezone.utc
NEW_TABLES = ("users", "grants", "machine_tokens", "machine_registrations", "agent_proposals", "agent_runs")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.clock = Clock()
        self._saved = store._utcnow
        store._utcnow = self.clock
        self.conn = store.connect(self.db, create=True)
        self.alerts = []

    def tearDown(self):
        store._utcnow = self._saved
        self.conn.close()
        self.tmp.cleanup()

    def do(self, _action, /, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, _action, params, actor=actor, via=via, autonomous=autonomous)

    def notify(self, subject, body):
        self.alerts.append(subject)

    def audit(self, action=None):
        rows = list(reversed(store.audit_log(self.conn, limit=1000)))
        return [r for r in rows if action is None or r["action"] == action]

    def person(self, label="p"):
        return store.add_user(self.conn, "person", label, "test")["id"]

    def machine(self, label="m"):
        return store.add_user(self.conn, "machine", label, "test")["id"]

    def project(self, name="P"):
        return self.do("project.add", name=name)["id"]

    def owner(self, uid):
        return self.do("grant.set", user_id=uid, scope="owner")

    def register(self, hostname="host-a", user_id=None, code=None, label=None):
        """-> the raw token of a freshly registered (or re-issued) machine."""
        code = code or roles.new_registration_code()
        params = {"code_sha256": roles.code_sha256(code)}
        if user_id is not None:
            params["user_id"] = user_id
        self.do("machine.code", **params)
        return actions.register_machine(self.conn, code, hostname, actor="cli", via="cli", label=label)

    def dump_all(self):
        return "\n".join(self.conn.iterdump())


class Schema(Base):
    def test_tables_and_columns_exist_without_a_version_bump(self):
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(NEW_TABLES) <= have)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        self.assertTrue({"blocked_by", "forwarded_at"} <= cols)
        self.assertEqual(store.SCHEMA_VERSION, 5)
        self.assertEqual(store.get_meta(self.conn, "schema_version"), "5")

    def test_an_existing_database_gains_them_on_connect(self):
        for t in ("agent_proposals", "agent_runs", "machine_registrations", "grants"):
            self.conn.execute(f"DROP TABLE {t}")
        self.conn.execute("DROP TRIGGER machine_tokens_no_delete")
        self.conn.execute("DROP TABLE machine_tokens")
        self.conn.execute("DROP TABLE users")
        self.conn.commit()
        self.conn.close()
        self.conn = store.connect(self.db)
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(NEW_TABLES) <= have)
        self.assertEqual(store.get_meta(self.conn, "schema_version"), "5")

    def test_users_and_grants_are_versioned(self):
        uid = self.person()
        pid = self.project()
        g = self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        self.assertEqual(store.get_user(self.conn, uid)["version"], 0)
        self.do("user.rename", user_id=uid, label="x")
        self.assertEqual(store.get_user(self.conn, uid)["version"], 1)
        self.do("grant.set", user_id=uid, scope="project", project="P", level="editor")
        row = self.conn.execute("SELECT version FROM grants WHERE id=?", (g["id"],)).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(pid, g["project_id"])

    def test_machine_tokens_are_never_deleted(self):
        r = self.register()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "never deleted"):
            self.conn.execute("DELETE FROM machine_tokens WHERE id=?", (r["token_id"],))

    def test_grant_constraints(self):
        uid, pid = self.person(), self.project()
        ins = "INSERT INTO grants(user_id, scope, project_id, level, source, created_at, created_by) VALUES (?,?,?,?,?,?,?)"
        for bad in ((uid, "owner", None, "viewer", "manual"), (uid, "owner", pid, None, "manual"),
                    (uid, "project", None, "viewer", "manual"), (uid, "project", pid, None, "manual"),
                    (uid, "project", pid, "root", "manual"), (uid, "project", pid, "viewer", "ldap:x"),
                    (uid, "project", pid, "viewer", "oidc:")):
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                self.conn.execute(ins, bad + ("t", "t"))
        self.conn.execute(ins, (uid, "owner", None, None, "manual", "t", "t"))
        with self.assertRaises(sqlite3.IntegrityError):         # one owner grant per user and source (NULL project)
            self.conn.execute(ins, (uid, "owner", None, None, "manual", "t", "t"))
        self.conn.execute(ins, (uid, "owner", None, None, "oidc:admins", "t", "t"))     # another source: fine

    def test_a_project_with_grants_is_not_deleted(self):
        uid = self.person()
        self.project()
        self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        with self.assertRaisesRegex(ValueError, "grants"):
            self.do("project.delete", project="P", reason="x")
        self.do("grant.remove", user_id=uid, scope="project", project="P")
        self.do("project.delete", project="P", reason="x")


class Grants(Base):
    def test_owner_grant_is_audited_and_alerts(self):
        uid = self.person("ann")
        r = self.owner(uid)
        self.assertEqual((r["scope"], r["level"], r["source"]), ("owner", None, "manual"))
        self.assertEqual(len(r["alerts"]), 1)
        self.assertNotIn("ann", r["alerts"][0]["subject"])
        self.assertIn("ann", r["alerts"][0]["body"])
        row = self.audit("grant.set")[-1]
        self.assertEqual((row["actor"], row["target_type"]), ("cli", "grant"))
        self.assertNotIn("alerts", json.dumps(row["after"]))
        actions.deliver_alerts(r, self.notify)
        self.assertEqual(len(self.alerts), 1)

    def test_project_grants_by_level_and_source(self):
        uid = self.person()
        self.project()
        self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        self.do("grant.sync_groups", user_id=uid, groups=["ops"], provider="idp",
                group_map=[{"group": "ops", "grant": "project:P:manager"}])
        rows = store.list_grants(self.conn, uid)
        self.assertEqual({(g["source"], g["level"]) for g in rows}, {("manual", "viewer"), ("oidc:ops", "manager")})
        g = actions._grant_rows(rows)
        self.assertEqual(roles.effective_role(g, rows[0]["project_id"]), "manager")

    def test_changing_a_level_updates_and_the_same_level_is_a_noop(self):
        uid = self.person()
        self.project()
        a = self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        n = len(self.audit())
        b = self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        self.assertTrue(b["unchanged"])
        self.assertEqual(len(self.audit()), n)
        c = self.do("grant.set", user_id=uid, scope="project", project="P", level="manager")
        self.assertEqual((c["id"], c["level"]), (a["id"], "manager"))
        self.assertEqual(len(self.audit()), n + 1)
        self.assertEqual(self.audit("grant.set")[-1]["before"]["level"], "viewer")

    def test_validation(self):
        uid = self.person()
        self.project()
        for params, exc in (({"scope": "owner", "level": "viewer"}, ValueError),
                            ({"scope": "project", "project": "P"}, ValueError),
                            ({"scope": "project", "project": "P", "level": "root"}, ValueError),
                            ({"scope": "team"}, ValueError),
                            ({"scope": "project", "project": "Nope", "level": "viewer"}, LookupError),
                            ({"scope": "owner", "project": "P"}, ValueError)):
            with self.assertRaises(exc, msg=params):
                self.do("grant.set", user_id=uid, **params)
        with self.assertRaises(LookupError):
            self.do("grant.set", user_id=999, scope="owner")
        self.assertEqual(store.list_grants(self.conn), [])

    def test_a_revoked_user_gets_no_grant(self):
        m = self.machine()
        self.do("user.revoke", user_id=m)
        with self.assertRaises(store.InvalidTransition):
            self.do("grant.set", user_id=m, scope="owner")

    def test_machine_users_can_be_owners_or_managers_and_start_with_nothing(self):
        r = self.register()
        self.assertEqual(store.list_grants(self.conn, r["user_id"]), [])           # D-195
        self.owner(r["user_id"])                                                    # D-198, D-290
        self.assertTrue(roles.is_owner(actions._grant_rows(store.list_grants(self.conn, r["user_id"]))))

    def test_stale_version_conflicts(self):
        uid = self.person()
        self.project()
        g = self.do("grant.set", user_id=uid, scope="project", project="P", level="viewer")
        self.do("grant.set", user_id=uid, scope="project", project="P", level="editor")
        with self.assertRaises(actions.Conflict):
            self.do("grant.set", user_id=uid, scope="project", project="P", level="manager", version=g["version"])
        with self.assertRaises(actions.Conflict):
            self.do("grant.remove", user_id=uid, scope="project", project="P", version=g["version"])


class GrantorRules(Base):
    """A `user:<id>` actor is held to D-192; the host's writers are not (the matrix is step A3)."""
    def setUp(self):
        super().setUp()
        self.project("P")
        self.project("Q")
        self.boss, self.mgr, self.edt, self.target = self.person("boss"), self.person("mgr"), self.person("edt"), self.person("t")
        self.owner(self.boss)
        self.do("grant.set", user_id=self.mgr, scope="project", project="P", level="manager")
        self.do("grant.set", user_id=self.edt, scope="project", project="P", level="editor")

    def as_user(self, uid, name, **params):
        return self.do(name, actor=f"user:{uid}", via="dashboard", **params)

    def test_manager_grants_and_revokes_editor_and_viewer_on_its_project(self):
        self.as_user(self.mgr, "grant.set", user_id=self.target, scope="project", project="P", level="editor")
        self.as_user(self.mgr, "grant.set", user_id=self.target, scope="project", project="P", level="viewer")
        self.as_user(self.mgr, "grant.remove", user_id=self.target, scope="project", project="P")
        self.assertEqual(store.list_grants(self.conn, self.target), [])
        self.assertEqual(self.audit("grant.set")[-1]["actor"], f"user:{self.mgr}")

    def test_manager_does_not_grant_more_or_elsewhere(self):
        for params in ({"scope": "owner"}, {"scope": "project", "project": "P", "level": "manager"},
                       {"scope": "project", "project": "Q", "level": "viewer"}):
            with self.assertRaises(actions.Forbidden, msg=params):
                self.as_user(self.mgr, "grant.set", user_id=self.target, **params)
        with self.assertRaises(actions.Forbidden):      # another manager's grant is not the manager's to remove
            self.do("grant.set", user_id=self.target, scope="project", project="P", level="manager")
            self.as_user(self.mgr, "grant.remove", user_id=self.target, scope="project", project="P")
        self.assertEqual(len(store.list_grants(self.conn, self.target)), 1)

    def test_editors_viewers_strangers_and_revoked_users_grant_nothing(self):
        for uid in (self.edt, self.target):
            with self.assertRaises(actions.Forbidden):
                self.as_user(uid, "grant.set", user_id=self.target, scope="project", project="P", level="viewer")
        self.do("user.revoke", user_id=self.mgr)
        with self.assertRaises(actions.Forbidden):
            self.as_user(self.mgr, "grant.set", user_id=self.target, scope="project", project="P", level="viewer")
        with self.assertRaises(actions.Forbidden):
            self.as_user(999, "grant.set", user_id=self.target, scope="project", project="P", level="viewer")

    def test_an_owner_user_grants_anything(self):
        self.as_user(self.boss, "grant.set", user_id=self.target, scope="owner")
        self.as_user(self.boss, "grant.set", user_id=self.target, scope="project", project="Q", level="manager")

    def test_a_refusal_changes_nothing(self):
        before = self.dump_all()
        with self.assertRaises(actions.Forbidden):
            self.as_user(self.edt, "grant.set", user_id=self.target, scope="owner")
        self.assertEqual(self.dump_all(), before)


class LastOwner(Base):
    def test_the_last_manual_person_owner_stays(self):
        a = self.person("a")
        self.owner(a)
        with self.assertRaisesRegex(actions.Forbidden, "last owner"):
            self.do("grant.remove", user_id=a, scope="owner")
        with self.assertRaisesRegex(actions.Forbidden, "last owner"):
            self.do("user.revoke", user_id=a)
        self.assertTrue(roles.is_owner(actions._grant_rows(store.list_grants(self.conn, a))))
        self.assertIsNone(store.get_user(self.conn, a)["revoked_at"])

    def test_a_second_manual_person_owner_lets_the_first_go(self):
        a, b = self.person("a"), self.person("b")
        self.owner(a)
        self.owner(b)
        self.do("grant.remove", user_id=a, scope="owner")
        with self.assertRaises(actions.Forbidden):
            self.do("user.revoke", user_id=b)
        self.assertEqual(store.list_grants(self.conn, a), [])

    def test_group_derived_and_machine_owners_do_not_stand_in(self):
        a, b, m = self.person("a"), self.person("b"), self.machine()
        self.owner(a)
        self.do("grant.sync_groups", user_id=b, groups=["admins"], provider="idp",
                group_map=[{"group": "admins", "grant": "owner"}])
        self.owner(m)
        with self.assertRaisesRegex(actions.Forbidden, "last owner"):
            self.do("grant.remove", user_id=a, scope="owner")
        self.do("grant.remove", user_id=m, scope="owner")           # a machine owner can go: it was never counted

    def test_a_group_losing_its_owner_grant_is_not_guarded(self):
        a, b = self.person("a"), self.person("b")
        self.owner(a)
        self.do("grant.sync_groups", user_id=b, groups=["admins"], provider="idp",
                group_map=[{"group": "admins", "grant": "owner"}])
        r = self.do("grant.sync_groups", user_id=b, groups=[], provider="idp",
                    group_map=[{"group": "admins", "grant": "owner"}])
        self.assertEqual(len(r["removed"]), 1)

    def test_no_person_owner_yet_protects_nothing(self):
        m = self.machine()
        self.owner(m)
        self.do("grant.remove", user_id=m, scope="owner")           # before 6a the host owner (cli) is the owner of record
        self.do("user.revoke", user_id=m)


class GroupSync(Base):
    MAP = [{"group": "eng", "grant": "project:P:editor"}, {"group": "ops", "grant": "project:Q:viewer"},
           {"group": "admins", "grant": "owner"}]

    def setUp(self):
        super().setUp()
        self.project("P")
        self.project("Q")
        self.uid = self.person()

    def sync(self, groups, **kw):
        return self.do("grant.sync_groups", user_id=self.uid, groups=groups, provider="idp",
                       group_map=kw.get("group_map", self.MAP))

    def test_adds_then_removes_only_oidc_rows_and_keeps_manual_ones(self):
        self.do("grant.set", user_id=self.uid, scope="project", project="P", level="viewer")
        r = self.sync(["eng", "ops"])
        self.assertEqual(len(r["added"]), 2)
        self.assertEqual(len(store.list_grants(self.conn, self.uid)), 3)
        r = self.sync(["ops"])                                          # left eng
        self.assertEqual([x["source"] for x in r["removed"]], ["oidc:eng"])
        r = self.sync([])
        self.assertEqual([x["source"] for x in r["removed"]], ["oidc:ops"])
        self.assertEqual([g["source"] for g in store.list_grants(self.conn, self.uid)], ["manual"])

    def test_unchanged_is_a_noop_without_an_audit_row(self):
        self.sync(["eng"])
        n = len(self.audit())
        r = self.sync(["eng"])
        self.assertEqual((r["added"], r["removed"]), ([], []))
        self.assertEqual(len(self.audit()), n)

    def test_a_map_entry_gone_or_changed_follows_at_the_next_login(self):
        self.sync(["eng"])
        r = self.sync(["eng"], group_map=[{"group": "eng", "grant": "project:P:manager"}])
        self.assertEqual((len(r["added"]), len(r["removed"])), (1, 1))
        self.assertEqual([g["level"] for g in store.list_grants(self.conn, self.uid)], ["manager"])
        self.sync(["eng"], group_map=[])
        self.assertEqual(store.list_grants(self.conn, self.uid), [])

    def test_a_group_mapped_to_owner(self):
        r = self.sync(["admins"])
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(self.sync(["admins"])["alerts"], [])
        row = store.list_grants(self.conn, self.uid)[0]
        self.assertEqual((row["scope"], row["source"]), ("owner", "oidc:admins"))
        self.assertEqual(self.audit("grant.sync_groups")[-1]["after"]["provider"], "idp")

    def test_an_unknown_project_fails_and_applies_nothing(self):
        with self.assertRaisesRegex(LookupError, "Nope"):
            self.sync(["eng", "x"], group_map=self.MAP + [{"group": "x", "grant": "project:Nope:viewer"}])
        self.assertEqual(store.list_grants(self.conn, self.uid), [])

    def test_bad_input(self):
        for kw in ({"group_map": "owner"}, {"group_map": [{"group": "a"}]}):
            with self.assertRaises(ValueError):
                self.sync(["eng"], **kw)
        with self.assertRaises(ValueError):
            self.do("grant.sync_groups", user_id=self.uid, groups="eng", provider="idp", group_map=self.MAP)
        with self.assertRaises(ValueError):             # machine users have no OIDC login
            self.do("grant.sync_groups", user_id=self.machine(), groups=[], provider="idp", group_map=self.MAP)


class Users(Base):
    def test_rename_and_stale_version(self):
        m = self.machine("old")
        r = self.do("machine.rename", user_id=m, label="new")
        self.assertEqual(r["label"], "new")
        with self.assertRaises(actions.Conflict):
            self.do("user.rename", user_id=m, label="x", version=0)
        for bad in ("", "  ", "x" * 101, "a\nb"):
            with self.assertRaises(ValueError, msg=bad):
                self.do("user.rename", user_id=m, label=bad)

    def test_machine_actions_refuse_a_person(self):
        p = self.person()
        for name, extra in (("machine.rename", {"label": "x"}), ("machine.revoke", {})):
            with self.assertRaises(ValueError):
                self.do(name, user_id=p, **extra)
        self.do("user.rename", user_id=p, label="x")

    def test_revoking_a_machine_revokes_its_tokens_and_keeps_its_grants(self):
        self.project()
        r = self.register()
        self.do("grant.set", user_id=r["user_id"], scope="project", project="P", level="viewer")
        out = self.do("machine.revoke", user_id=r["user_id"])
        self.assertEqual(out["revoked_tokens"], [r["token_id"]])
        self.assertIsNone(actions.authenticate_machine(self.conn, r["token"]))
        self.assertEqual(len(store.list_grants(self.conn, r["user_id"])), 1)
        self.assertTrue(self.do("user.revoke", user_id=r["user_id"])["unchanged"])

    def test_revoking_a_person_ends_their_sessions(self):
        p = self.person()
        store.add_auth_session(self.conn, "1" * 64, p, "2" * 64, factors=["password"])
        out = self.do("user.revoke", user_id=p)
        self.assertEqual(len(out["ended_sessions"]), 1)
        self.assertEqual(store.open_auth_sessions(self.conn, p), [])


class Registration(Base):
    def test_register_makes_a_machine_user_with_no_grants(self):
        r = self.register("build-box")
        user = store.get_user(self.conn, r["user_id"])
        self.assertEqual((user["kind"], user["label"], user["revoked_at"]), ("machine", "build-box", None))
        self.assertEqual(store.list_grants(self.conn, r["user_id"]), [])
        self.assertFalse(r["reissued"])
        tok = store.get_machine_token(self.conn, r["token_id"])
        self.assertEqual((tok["hostname_at_issue"], tok["user_id"]), ("build-box", r["user_id"]))
        self.assertTrue(r["token"].startswith(f"afcm_{r['token_id']}_"))

    def test_only_hashes_are_stored(self):
        code = roles.new_registration_code()
        r = self.register(code=code)
        secret = roles.parse_token(r["token"])[1]
        dump = self.dump_all()
        for raw in (code, code.replace("-", ""), secret, r["token"]):
            self.assertNotIn(raw, dump)
        self.assertIn(roles.secret_sha256(secret), dump)
        self.assertIn(roles.code_sha256(code), dump)

    def test_codes_are_single_use(self):
        code = roles.new_registration_code()
        self.register(code=code)
        with self.assertRaises(actions.RegistrationRefused):
            actions.register_machine(self.conn, code, "other", actor="cli", via="cli")
        self.assertEqual(len(store.list_users(self.conn, "machine")), 1)

    def test_codes_expire_after_15_minutes(self):
        code = roles.new_registration_code()
        self.do("machine.code", code_sha256=roles.code_sha256(code))
        self.clock.advance(minutes=14, seconds=59)
        probe = actions.register_machine(self.conn, code, "h", actor="cli", via="cli")
        self.assertTrue(probe["token"])
        late = roles.new_registration_code()
        self.do("machine.code", code_sha256=roles.code_sha256(late))
        self.clock.advance(minutes=15, seconds=1)
        with self.assertRaises(actions.RegistrationRefused):
            actions.register_machine(self.conn, late, "h", actor="cli", via="cli")

    def test_one_message_for_unknown_used_and_expired(self):
        used = roles.new_registration_code()
        self.register(code=used)
        old = roles.new_registration_code()
        self.do("machine.code", code_sha256=roles.code_sha256(old), ttl_minutes=1)
        self.clock.advance(minutes=2)
        msgs = set()
        for c in (used, old, roles.new_registration_code()):
            with self.assertRaises(actions.RegistrationRefused) as cm:
                actions.register_machine(self.conn, c, "h", actor="cli", via="cli")
            msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)

    def test_reissue_revokes_the_old_token_and_keeps_the_user_and_grants(self):
        first = self.register("box")
        self.owner(first["user_id"])
        second = self.register("box-2", user_id=first["user_id"])
        self.assertTrue(second["reissued"])
        self.assertEqual(second["user_id"], first["user_id"])
        self.assertEqual(second["revoked_tokens"], [first["token_id"]])
        self.assertIsNone(actions.authenticate_machine(self.conn, first["token"]))
        self.assertEqual(actions.authenticate_machine(self.conn, second["token"])["user_id"], first["user_id"])
        self.assertEqual(len(store.list_grants(self.conn, first["user_id"])), 1)
        self.assertEqual(store.get_user(self.conn, first["user_id"])["label"], "box")
        self.assertEqual(len(store.list_machine_tokens(self.conn, first["user_id"])), 2)      # revoked, not deleted

    def test_reissue_codes(self):
        m = self.register()["user_id"]
        a, b = roles.new_registration_code(), roles.new_registration_code()
        self.do("machine.code", code_sha256=roles.code_sha256(a), user_id=m)
        self.do("machine.code", code_sha256=roles.code_sha256(b), user_id=m)        # supersedes a
        with self.assertRaises(actions.RegistrationRefused):
            actions.register_machine(self.conn, a, "h", actor="cli", via="cli")
        self.assertTrue(actions.register_machine(self.conn, b, "h", actor="cli", via="cli")["reissued"])
        with self.assertRaises(ValueError):
            self.do("machine.code", code_sha256="c" * 64, user_id=self.person())    # a person has no machine token
        self.do("machine.revoke", user_id=m)
        with self.assertRaises(store.InvalidTransition):
            self.do("machine.code", code_sha256="d" * 64, user_id=m)

    def test_code_input_checks(self):
        for kw in ({"code_sha256": "short"}, {"code_sha256": "e" * 64, "ttl_minutes": 0},
                   {"code_sha256": "e" * 64, "ttl_minutes": 61}, {"code_sha256": "e" * 64, "ttl_minutes": "15"}):
            with self.assertRaises(ValueError, msg=kw):
                self.do("machine.code", **kw)
        for host in ("", "a b", "x" * 129):
            with self.assertRaises(ValueError, msg=host):
                self.do("machine.register", code_sha256="e" * 64, token_sha256="f" * 64, hostname=host)

    def test_rate_limit_on_refused_registrations(self):
        for _ in range(actions.REGISTER_MAX_FAILURES):
            with self.assertRaises(actions.RegistrationRefused):
                actions.register_machine(self.conn, roles.new_registration_code(), "h", actor="cli", via="cli")
        good = roles.new_registration_code()
        self.do("machine.code", code_sha256=roles.code_sha256(good))
        with self.assertRaises(actions.RateLimited):
            actions.register_machine(self.conn, good, "h", actor="cli", via="cli")
        self.assertEqual(len(self.audit("machine.register_failed")), actions.REGISTER_MAX_FAILURES)   # the limit isn't recorded
        self.clock.advance(minutes=16)
        fresh = roles.new_registration_code()               # the first one expired meanwhile
        self.do("machine.code", code_sha256=roles.code_sha256(fresh))
        self.assertTrue(actions.register_machine(self.conn, fresh, "h", actor="cli", via="cli")["token"])

    def test_audit_rows_hold_no_secret(self):
        code = roles.new_registration_code()
        r = self.register(code=code)
        with self.assertRaises(actions.RegistrationRefused):
            actions.register_machine(self.conn, "AAAAA-BBBBB-CCCCC-DDDDD", "h", actor="cli", via="cli")
        text = json.dumps(self.audit(), default=str)
        self.assertNotIn(code, text)
        self.assertNotIn(roles.parse_token(r["token"])[1], text)
        self.assertEqual([x["action"] for x in self.audit()][:3], ["machine.code", "machine.register", "machine.register_failed"])


class Alerts(Base):
    """The subject of an alert reaches the progress doc and the public PROGRESS.md: ids only."""
    def test_no_label_or_hostname_in_any_subject(self):
        r = self.register("secret-host.example", label="secret-label")
        out = self.owner(r["user_id"])
        sync_user = self.person("secret-person")
        sync = self.do("grant.sync_groups", user_id=sync_user, groups=["a"], provider="idp",
                       group_map=[{"group": "a", "grant": "owner"}])
        tok = actions.authenticate_machine(self.conn, r["token"], "secret-host.example", "stdio", notify=self.notify)
        self.assertIsNotNone(tok)
        actions.authenticate_machine(self.conn, r["token"], "other-secret-host", "stdio", notify=self.notify)
        self.assertEqual(len(self.alerts), 1)                                   # the clone alert
        seen = self.alerts + [a["subject"] for a in out["alerts"] + sync["alerts"]]
        self.assertEqual(len(seen), 3)
        for subject in seen:
            for secret in ("secret", "example", "other-secret"):
                self.assertNotIn(secret, subject)
        bodies = [a["body"] for a in out["alerts"]]
        self.assertIn("secret-label", bodies[0])                                # the body (alerts doc) keeps them

    def test_clone_alert_body_names_the_hosts(self):
        r = self.register("box-1")
        got = []
        actions.authenticate_machine(self.conn, r["token"], "box-2", "stdio", notify=lambda s, b: got.append(b))
        self.assertIn("box-1", got[0])
        self.assertIn("box-2", got[0])


class Autonomous(Base):
    """A session AFClaude launched may not hand out roles or machine credentials, through the CLI either."""
    def test_refused_for_an_autonomous_caller_on_every_path(self):
        uid = self.person()
        m = self.register()["user_id"]
        self.owner(self.person("boss"))
        for name, params in (("grant.set", {"user_id": uid, "scope": "owner"}),
                             ("grant.remove", {"user_id": uid, "scope": "owner"}),
                             ("grant.sync_groups", {"user_id": uid, "groups": [], "provider": "i", "group_map": []}),
                             ("machine.code", {"code_sha256": "a" * 64}),
                             ("machine.revoke", {"user_id": m}), ("user.revoke", {"user_id": m})):
            for via in ("cli", "mcp", "dashboard"):
                with self.assertRaises(actions.Forbidden, msg=(name, via)):
                    self.do(name, via=via, autonomous=True, **params)
        self.assertEqual(store.list_grants(self.conn, uid), [])
        self.assertIsNone(store.get_user(self.conn, m)["revoked_at"])
        self.assertEqual(store.count_audit_since(self.conn, "machine.code", "0"), 1)       # only the setup's own
        self.owner(uid)                                                                      # the owner still can

    def test_the_cli_passes_the_guard_flag(self):
        old = os.environ.get("CLAUDE_GUARD_DISABLE")
        os.environ["CLAUDE_GUARD_DISABLE"] = "1"
        try:
            self.conn.close()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = tasks.main(["--db", self.db, "machine", "code"])
            self.assertEqual(rc, 1)
            self.assertIn("autonomous", err.getvalue())
        finally:
            if old is None:
                os.environ.pop("CLAUDE_GUARD_DISABLE")
            else:
                os.environ["CLAUDE_GUARD_DISABLE"] = old
            self.conn = store.connect(self.db)


class Authenticate(Base):
    def setUp(self):
        super().setUp()
        self.r = self.register("host-a")

    def auth(self, token=None, host="host-a", transport="stdio"):
        token = self.r["token"] if token is None else token
        return actions.authenticate_machine(self.conn, token, host, transport, notify=self.notify)

    def test_accepts_the_token_and_records_the_use(self):
        out = self.auth()
        self.assertEqual((out["user_id"], out["token_id"], out["label"]), (self.r["user_id"], self.r["token_id"], "host-a"))
        tok = store.get_machine_token(self.conn, self.r["token_id"])
        self.assertEqual((tok["last_hostname"], tok["last_transport"]), ("host-a", "stdio"))
        self.assertEqual(tok["last_seen_at"], store.now_iso())

    def test_refuses_what_is_not_a_valid_token(self):
        secret = roles.parse_token(self.r["token"])[1]
        for bad in ("", "garbage", roles.format_token(self.r["token_id"], roles.new_token_secret()),
                    roles.format_token(self.r["token_id"] + 5, secret)):
            self.assertIsNone(self.auth(bad), bad)

    def test_a_revoked_token_or_user_is_refused(self):
        self.do("machine.revoke", user_id=self.r["user_id"])
        self.assertIsNone(self.auth())

    def test_last_seen_is_written_at_most_once_a_minute_for_one_host(self):
        self.auth()
        t0 = store.get_machine_token(self.conn, self.r["token_id"])["last_seen_at"]
        self.clock.advance(seconds=30)
        self.auth()
        self.assertEqual(store.get_machine_token(self.conn, self.r["token_id"])["last_seen_at"], t0)
        self.clock.advance(seconds=31)
        self.auth()
        self.assertNotEqual(store.get_machine_token(self.conn, self.r["token_id"])["last_seen_at"], t0)

    def test_a_second_hostname_within_a_day_alerts_once(self):
        self.auth()
        self.assertEqual(self.alerts, [])
        self.assertIsNotNone(self.auth(host="HOST-A"))            # same host, other case
        self.clock.advance(hours=1)
        self.auth(host="host-b")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("host-a", store.audit_log(self.conn, limit=1)[0]["before"]["last_hostname"])
        self.clock.advance(minutes=5)
        self.auth(host="host-a")                                   # flips back: still the same day's alert
        self.auth(host="host-b")
        self.assertEqual(len(self.alerts), 1)
        self.clock.advance(hours=25)
        self.auth(host="host-b")                                   # idle for over a day: not a clone sighting
        self.assertEqual(len(self.alerts), 1)

    def test_the_first_use_from_another_host_than_issued_alerts_within_a_day(self):
        self.auth(host="elsewhere")
        self.assertEqual(len(self.alerts), 1)

    def test_a_token_first_used_over_a_day_after_issue_from_another_host_does_not(self):
        self.clock.advance(hours=30)
        self.auth(host="renamed-box")
        self.assertEqual(self.alerts, [])

    def test_a_failing_alert_raises_after_the_write(self):
        def boom(subject, body):
            raise RuntimeError("push down")
        with self.assertRaisesRegex(RuntimeError, "push down"):
            actions.authenticate_machine(self.conn, self.r["token"], "host-z", "stdio", notify=boom)
        self.assertEqual(store.get_machine_token(self.conn, self.r["token_id"])["last_hostname"], "host-z")


class CLI(Base):
    def run_cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.main(["--db", self.db, *argv])
        self.assertEqual(rc, expect, err.getvalue())
        return out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        self.conn.close()
        self.file = os.path.join(self.tmp.name, "cfg", "machine.json")
        self._notify = notify.notify
        notify.notify = lambda subject, body="", do_push=True: self.alerts.append(subject) or "test"

    def tearDown(self):
        notify.notify = self._notify
        self.conn = store.connect(self.db)
        super().tearDown()

    def code(self):
        out, _ = self.run_cli("machine", "code", "--json")
        return json.loads(out)["code"]

    def code_from(self, text):
        import re
        return re.search(r"[A-Z2-9]{5}(-[A-Z2-9]{5}){3}", text).group(0)

    def read_file(self):
        with open(self.file) as fh:
            return json.load(fh)

    def test_register_writes_the_token_file_with_mode_0600(self):
        out, _ = self.run_cli("machine", "register", self.code(), "--label", "box", "--file", self.file)
        self.assertIn("no access until an owner grants", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        rec = self.read_file()
        conn = store.connect(self.db)
        try:
            tok = store.get_machine_token(conn, rec["token_id"])
            self.assertTrue(roles.secret_matches(tok["token_sha256"], roles.parse_token(rec["token"])[1]))
            self.assertNotIn(rec["token"], "\n".join(conn.iterdump()))
        finally:
            conn.close()
        self.assertNotIn(rec["token"], out)

    def test_an_existing_token_file_is_kept_and_the_code_stays_unspent(self):
        code = self.code()
        os.makedirs(os.path.dirname(self.file))
        with open(self.file, "w") as fh:
            fh.write("{}")
        _, err = self.run_cli("machine", "register", code, "--file", self.file, expect=1)
        self.assertIn("exists", err)
        self.assertEqual(self.read_file(), {})
        self.run_cli("machine", "register", code, "--file", self.file, "--force")
        self.assertIn("token", self.read_file())

    def test_print_shows_the_token_once_and_writes_no_file(self):
        out, _ = self.run_cli("machine", "register", self.code(), "--print", "--label", "box")
        self.assertIn("afcm_", out)
        self.assertFalse(os.path.exists(self.file))

    def test_a_bad_code_is_an_error_and_writes_no_file(self):
        _, err = self.run_cli("machine", "register", "AAAAA-BBBBB-CCCCC-DDDDD", "--file", self.file, expect=1)
        self.assertIn("not valid", err)
        self.assertFalse(os.path.exists(self.file))

    def test_machine_list_rename_revoke_and_grants(self):
        self.run_cli("machine", "register", self.code(), "--label", "box", "--file", self.file)
        self.run_cli("grant", "set", "1", "--owner")
        out, _ = self.run_cli("machine", "list")
        self.assertIn("box", out)
        self.assertIn("owner", out)
        self.assertNotIn("token_sha256", self.run_cli("machine", "list", "--json")[0])
        self.assertEqual(self.alerts and len(self.alerts), 1)               # making an owner alerts
        self.run_cli("machine", "rename", "1", "ci")
        self.assertIn("ci", self.run_cli("machine", "list")[0])
        out, _ = self.run_cli("grant", "list")
        self.assertIn("owner", out)
        self.run_cli("grant", "remove", "1", "--owner")
        self.assertIn("no grants", self.run_cli("grant", "list")[0])
        self.run_cli("machine", "revoke", "1")
        self.assertIn("REVOKED", self.run_cli("machine", "list")[0])

    def test_project_grants(self):
        self.run_cli("project", "add", "P")
        self.run_cli("machine", "register", self.code(), "--label", "box", "--file", self.file)
        self.run_cli("grant", "set", "1", "--project", "P", "--level", "editor")
        self.assertIn("editor of P", self.run_cli("grant", "list", "--user", "1")[0])
        _, err = self.run_cli("grant", "set", "1", "--project", "P", expect=1)
        self.assertIn("level", err)
        self.run_cli("grant", "remove", "1", "--project", "P")

    def test_a_label_is_not_the_hostname_and_causes_no_clone_alert(self):
        import socket
        self.run_cli("machine", "register", self.code(), "--label", "build box", "--file", self.file)
        rec = self.read_file()
        self.assertEqual(rec["label"], "build box")
        conn = store.connect(self.db)
        try:
            self.assertEqual(store.get_machine_token(conn, rec["token_id"])["hostname_at_issue"], socket.gethostname())
            actions.authenticate_machine(conn, rec["token"], socket.gethostname(), "stdio", notify=self.notify)
        finally:
            conn.close()
        self.assertEqual(self.alerts, [])

    def test_a_failed_file_write_shows_the_token_once_in_the_error(self):
        saved = tasks.write_machine_file

        def broken(path, rec):
            raise OSError("disk full")
        tasks.write_machine_file = broken
        try:
            _, err = self.run_cli("machine", "register", self.code(), "--file", self.file, expect=1)
        finally:
            tasks.write_machine_file = saved
        self.assertIn("disk full", err)
        self.assertRegex(err, r"afcm_\d+_[0-9a-f]{64}")

    def test_the_ttl_is_capped_at_60_minutes(self):
        _, err = self.run_cli("machine", "code", "--ttl", "61", expect=1)
        self.assertIn("60", err)
        self.run_cli("machine", "code", "--ttl", "60")

    def test_the_reissue_flow(self):
        self.run_cli("machine", "register", self.code(), "--label", "box", "--file", self.file)
        old = self.read_file()["token"]
        out, _ = self.run_cli("machine", "code", "--user", "1")
        self.assertIn("re-issues machine 1", out)
        code = self.code_from(out)
        self.run_cli("machine", "register", code, "--file", self.file, "--force")
        new = self.read_file()
        self.assertNotEqual(old, new["token"])
        self.assertEqual(new["user_id"], 1)
        conn = store.connect(self.db)
        try:
            self.assertIsNone(actions.authenticate_machine(conn, old))
            self.assertEqual(actions.authenticate_machine(conn, new["token"])["user_id"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
