#!/usr/bin/env python3
"""Tests for roles.py, the pure part of phase 5a A1 (docs/phase5a_plan.md §3.1, §3.5, §3.6): principals from
actor strings, effective roles from grants, who may grant what (D-192), the last-owner rule (D-183), the
group-sync plan, registration codes and machine tokens. No database.

    python3 -m unittest test_roles
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import roles  # noqa: E402
from roles import GrantRow, UserRow  # noqa: E402


def g(user, scope="project", project=1, level="viewer", source="manual"):
    return GrantRow(user, scope, None if scope == "owner" else project, None if scope == "owner" else level, source)


OWNER = g(1, "owner")


class Principals(unittest.TestCase):
    def test_forms(self):
        for actor, kind in (("cli", "host"), ("owner", "host"), ("user:12", "user"), ("machine:3", "machine"),
                            ("task-manager:abc-123", "task-manager"), ("agent:abc-123", "agent"),
                            ("agent:abc-123:a9f", "agent"), ("runner", "runner"), ("runner:keepalive", "runner"),
                            ("dispatcher", "other"), ("mcp:xyz", "other"), ("machine:0", "other"),
                            ("user:1;drop", "other")):
            self.assertEqual(roles.parse_actor(actor).kind, kind, actor)

    def test_ids_and_sessions(self):
        self.assertEqual(roles.parse_actor("machine:7").user_id, 7)
        self.assertTrue(roles.parse_actor("user:7").has_grants)
        self.assertFalse(roles.parse_actor("cli").has_grants)
        p = roles.parse_actor("agent:s1:ag2")
        self.assertEqual((p.session, p.user_id), ("s1", None))


class EffectiveRole(unittest.TestCase):
    def test_none_without_grants(self):
        self.assertIsNone(roles.effective_role([], 1))
        self.assertIsNone(roles.effective_role([g(5, project=2)], 1))      # another project's grant

    def test_highest_level_on_the_project(self):
        grants = [g(5, level="viewer"), g(5, level="manager", source="oidc:ops"), g(5, project=2, level="editor")]
        self.assertEqual(roles.effective_role(grants, 1), "manager")
        self.assertEqual(roles.effective_role(grants, 2), "editor")

    def test_owner_everywhere_and_for_global(self):
        self.assertEqual(roles.effective_role([OWNER], 99), "owner")
        self.assertEqual(roles.effective_role([OWNER], None), "owner")
        self.assertIsNone(roles.effective_role([g(5, level="manager")], None))      # global = owner only (D-149)

    def test_at_least(self):
        self.assertTrue(roles.at_least("manager", "editor"))
        self.assertFalse(roles.at_least("viewer", "editor"))
        self.assertFalse(roles.at_least(None, "viewer"))
        self.assertTrue(roles.at_least("owner", "manager"))

    def test_visible_projects(self):
        self.assertEqual(roles.visible_projects([OWNER], [1, 2, 3]), {1, 2, 3})
        self.assertEqual(roles.visible_projects([g(5, project=2), g(5, project=3, level="manager")], [1, 2, 3]), {2, 3})
        self.assertEqual(roles.visible_projects([], [1, 2]), frozenset())      # D-195: no grants, no access


class GrantShape(unittest.TestCase):
    def test_valid(self):
        roles.check_grant_shape("owner", None, None, "manual")
        roles.check_grant_shape("project", 3, "editor", "oidc:eng")

    def test_invalid(self):
        for args in (("owner", 1, None, "manual"), ("owner", None, "viewer", "manual"),
                     ("project", None, "viewer", "manual"), ("project", 1, None, "manual"),
                     ("project", 1, "root", "manual"), ("team", 1, "viewer", "manual"),
                     ("project", 1, "viewer", "oidc:"), ("project", 1, "viewer", "ldap:x")):
            with self.assertRaises(ValueError, msg=args):
                roles.check_grant_shape(*args)


class GrantRules(unittest.TestCase):
    MANAGER = [g(2, level="manager")]

    def test_owner_may_do_anything(self):
        roles.check_grant_change([OWNER], "owner", None, None, None)
        roles.check_grant_change([OWNER], "project", 1, "manager", "viewer")

    def test_manager_grants_editor_and_viewer_on_its_project(self):
        roles.check_grant_change(self.MANAGER, "project", 1, "editor")
        roles.check_grant_change(self.MANAGER, "project", 1, "viewer", "editor")      # a demotion between the two
        roles.check_grant_change(self.MANAGER, "project", 1, None, "viewer")           # a removal

    def test_manager_does_not_make_managers_or_owners(self):
        for new, old in (("manager", None), ("manager", "viewer"), (None, "manager"), ("viewer", "manager")):
            with self.assertRaises(roles.Denied, msg=(new, old)):
                roles.check_grant_change(self.MANAGER, "project", 1, new, old)
        with self.assertRaises(roles.Denied):
            roles.check_grant_change(self.MANAGER, "owner", None, None)

    def test_manager_of_another_project_and_lower_roles_do_nothing(self):
        with self.assertRaises(roles.Denied):
            roles.check_grant_change(self.MANAGER, "project", 2, "viewer")
        for level in ("editor", "viewer"):
            with self.assertRaises(roles.Denied):
                roles.check_grant_change([g(2, level=level)], "project", 1, "viewer")
        with self.assertRaises(roles.Denied):
            roles.check_grant_change([], "project", 1, "viewer")


class LastOwner(unittest.TestCase):
    P = UserRow(1, "person")

    def test_only_a_manual_unrevoked_person_counts(self):
        users = [UserRow(1, "person"), UserRow(2, "person"), UserRow(3, "machine"), UserRow(4, "person", revoked=True),
                 UserRow(5, "person")]
        grants = [g(1, "owner"), g(2, "owner", source="oidc:admins"), g(3, "owner"), g(4, "owner"),
                  g(5, level="manager")]
        self.assertEqual(roles.manual_person_owner_ids(users, grants), {1})

    def test_refuses_to_take_the_last_one(self):
        with self.assertRaises(roles.Denied):
            roles.check_last_owner(frozenset({1}), frozenset())
        roles.check_last_owner(frozenset({1, 2}), frozenset({2}))
        roles.check_last_owner(frozenset({1}), frozenset({1}))

    def test_nothing_to_protect_before_there_is_a_person_owner(self):
        roles.check_last_owner(frozenset(), frozenset())          # before 6a: the guard protects nothing


class GroupSync(unittest.TestCase):
    ENTRIES = roles.og.parse_group_map([{"group": "admins", "grant": "owner"},
                                        {"group": "eng", "grant": "project:Alpha:editor"},
                                        {"group": "ops", "grant": "project:Alpha:viewer"}])

    def test_adds_and_removes_oidc_grants_only(self):
        og = roles.og
        current = [og.Grant("project", "Alpha", "editor", "oidc:eng"), og.Grant("project", "Beta", "viewer", "oidc:gone")]
        plan = roles.plan_group_sync(current, ["eng", "ops", "unmapped"], self.ENTRIES)
        self.assertEqual(plan.add, {og.Grant("project", "Alpha", "viewer", "oidc:ops")})
        self.assertEqual(plan.remove, {og.Grant("project", "Beta", "viewer", "oidc:gone")})
        self.assertFalse(plan.owner_granted)

    def test_owner_group(self):
        plan = roles.plan_group_sync([], ["admins"], self.ENTRIES)
        self.assertTrue(plan.owner_granted)
        again = roles.plan_group_sync(list(plan.add), ["admins"], self.ENTRIES)
        self.assertFalse(again.add or again.remove or again.owner_granted)

    def test_manual_grants_are_refused_as_input(self):
        with self.assertRaises(ValueError):
            roles.plan_group_sync([roles.og.Grant("owner", None, None, "manual")], [], self.ENTRIES)


class Credentials(unittest.TestCase):
    def test_code_form_and_normalising(self):
        code = roles.new_registration_code()
        self.assertRegex(code, r"^([A-HJ-NP-Z2-9]{5}-){3}[A-HJ-NP-Z2-9]{5}$")
        self.assertNotEqual(code, roles.new_registration_code())
        self.assertEqual(roles.code_sha256(code), roles.code_sha256(code.lower().replace("-", " ")))
        self.assertRegex(roles.code_sha256(code), r"^[0-9a-f]{64}$")

    def test_token_roundtrip(self):
        secret = roles.new_token_secret()
        self.assertRegex(secret, r"^[0-9a-f]{64}$")
        token = roles.format_token(17, secret)
        self.assertTrue(token.startswith("afcm_17_"))
        self.assertEqual(roles.parse_token(token), (17, secret))
        self.assertEqual(roles.parse_token(f"  {token}\n"), (17, secret))

    def test_malformed_tokens(self):
        secret = roles.new_token_secret()
        for bad in ("", "afcm_x_" + secret, "afcm_0_" + secret, "afcm_1_" + secret[:-1], "xfcm_1_" + secret,
                    "afcm_1_" + secret.upper(), None):
            with self.assertRaises(ValueError, msg=bad):
                roles.parse_token(bad)         # type: ignore[arg-type]

    def test_secret_compare(self):
        secret = roles.new_token_secret()
        stored = roles.secret_sha256(secret)
        self.assertTrue(roles.secret_matches(stored, secret))
        self.assertFalse(roles.secret_matches(stored, roles.new_token_secret()))
        self.assertFalse(roles.secret_matches("not a hash", secret))


if __name__ == "__main__":
    unittest.main()
