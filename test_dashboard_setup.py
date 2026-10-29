#!/usr/bin/env python3
"""Tests for setup, the first-registration race guard and admin reset-auth (phase 6a W7,
docs/phase6a_plan.md §3.7): the setup_codes table and the setup.mint_code / setup.claim_code actions
(single-use, expiring, only the hash stored, audited) run under any python3; dashboard/setup.py's
pure code helpers, SetupFlow (status/begin/confirm against a fake OwnerRegistry, two racing confirms,
one winner) and reset_auth need the venv (Python 3.12, Starlette):
    .venv/bin/python -m unittest test_dashboard_setup
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import store  # noqa: E402

_MISSING = [m for m in ("starlette", "pydantic", "jsonschema", "cryptography") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'the web stack'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_dashboard_setup") if _MISSING or sys.version_info < (3, 12) else ""


def h(n):
    return hashlib.sha256(f"id-{n}".encode()).hexdigest()


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        self.clock = Clock()
        saved = store._utcnow
        store._utcnow = self.clock
        self.addCleanup(setattr, store, "_utcnow", saved)
        self.conn = store.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def act(self, name, params, actor="cli", via="cli"):
        return actions.perform(self.conn, name, params, actor=actor, via=via)

    def mint(self, kind="first_owner", user_id=None, code_hash=None, ttl_hours=24):
        return self.act("setup.mint_code", {"kind": kind, "user_id": user_id,
                                            "code_hash": code_hash or h("code"), "ttl_hours": ttl_hours})

    def row(self, row_id):
        return store.get_setup_code_by_id(self.conn, row_id)

    def audits(self, action):
        return [a for a in store.audit_log(self.conn, 200) if a["action"] == action]


class MintAndClaim(DbCase):
    def test_mint_stores_only_the_hash_and_is_audited(self):
        res = self.mint(code_hash=h("abc"))
        row = self.row(res["id"])
        self.assertEqual(row["code_hash"], h("abc"))
        self.assertIsNone(row["used_at"])
        self.assertEqual(row["kind"], "first_owner")
        self.assertIsNone(row["user_id"])
        audit = self.audits("setup.mint_code")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["after"]["kind"], "first_owner")

    def test_mint_rejects_bad_params(self):
        with self.assertRaises(ValueError):
            self.mint(kind="bogus")
        with self.assertRaises(ValueError):
            self.mint(ttl_hours=0)
        with self.assertRaises(ValueError):
            self.mint(ttl_hours=-1)
        with self.assertRaises(ValueError):
            self.mint(kind="first_owner", user_id=7)   # first_owner takes no user_id
        with self.assertRaises(ValueError):
            self.mint(kind="reset", user_id=None)      # reset needs one

    def test_mint_supersedes_the_previous_open_code_of_the_same_kind_and_user(self):
        first = self.mint(code_hash=h("1"))
        second = self.mint(code_hash=h("2"))
        self.assertIsNotNone(self.row(first["id"])["used_at"])
        self.assertIsNone(self.row(second["id"])["used_at"])
        # a different user's open code is untouched
        third = self.mint(kind="reset", user_id=9, code_hash=h("3"))
        self.mint(code_hash=h("4"))   # another first_owner mint
        self.assertIsNone(self.row(third["id"])["used_at"])

    def test_claim_is_single_use_and_audited(self):
        m = self.mint(code_hash=h("x"))
        res = self.act("setup.claim_code", {"id": m["id"], "kind": "first_owner"}, actor="setup", via="dashboard")
        self.assertEqual(res["id"], m["id"])
        self.assertIsNotNone(self.row(m["id"])["used_at"])
        with self.assertRaisesRegex(store.InvalidTransition, "already been used"):
            self.act("setup.claim_code", {"id": m["id"], "kind": "first_owner"}, actor="setup", via="dashboard")
        self.assertEqual(len(self.audits("setup.claim_code")), 1)

    def test_claim_expires(self):
        m = self.mint(code_hash=h("x"), ttl_hours=1)
        self.clock.advance(hours=1, seconds=1)
        with self.assertRaisesRegex(store.InvalidTransition, "expired"):
            self.act("setup.claim_code", {"id": m["id"], "kind": "first_owner"}, actor="setup", via="dashboard")
        self.assertIsNone(self.row(m["id"])["used_at"])   # an expired code is refused, not silently spent

    def test_claim_checks_kind_and_user_match_the_row(self):
        m = self.mint(kind="reset", user_id=5, code_hash=h("x"))
        with self.assertRaises(store.NotFound):
            self.act("setup.claim_code", {"id": m["id"], "kind": "first_owner"}, actor="setup", via="dashboard")
        with self.assertRaises(store.NotFound):
            self.act("setup.claim_code", {"id": m["id"], "kind": "reset", "user_id": 6}, actor="setup", via="dashboard")
        with self.assertRaises(store.NotFound):
            self.act("setup.claim_code", {"id": 999999, "kind": "reset", "user_id": 5}, actor="setup", via="dashboard")

    def test_two_racing_claims_at_the_actions_level_one_winner(self):
        """The guard store.claim_setup_code relies on (UPDATE ... WHERE used_at IS NULL): each
        connection opens its own transaction, as two browser tabs each with their own connection
        would, and SQLite's writer lock serialises them."""
        m = self.mint(code_hash=h("race"))
        results = []

        def attempt():
            conn = store.connect(self.db)
            try:
                results.append(("ok", actions.perform(conn, "setup.claim_code",
                                                       {"id": m["id"], "kind": "first_owner"},
                                                       actor="setup", via="dashboard")))
            except store.InvalidTransition as e:
                results.append(("refused", str(e)))
            finally:
                conn.close()
        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, _ in results if kind == "refused"), 4)


class ClaudePlanSetting(DbCase):
    def test_default_is_unset_not_a_guessed_plan(self):
        """D-229: AFClaude asks for the plan at first startup, so the code default must not look
        like an answered choice -- "unset" is in CLAUDE_PLANS only so the default passes its own
        check (test_actions.SettingActions.test_registry_metadata), never a real plan."""
        s = actions.settings(self.conn)
        self.assertEqual(s["claude_plan"]["value"], "unset")
        self.assertEqual(actions.CLAUDE_PLAN_UNSET, "unset")
        self.assertEqual(s["claude_plan"]["section"], "host")
        self.assertIn("team", s["claude_plan"]["choices"])

    def test_choices(self):
        with self.assertRaises(ValueError):
            self.act("setting.set", {"key": "claude_plan", "value": "bogus"})
        self.act("setting.set", {"key": "claude_plan", "value": "team"})
        self.assertEqual(actions.get_setting(self.conn, "claude_plan"), "team")


if not SKIP:
    from dashboard import setup  # noqa: E402
    from dashboard.auth import totp  # noqa: E402
    from dashboard.routes import Session  # noqa: E402

    class FakeRegistry:
        """An in-memory stand-in for 5a's users/grants, exercised through the same `conn`/transaction
        SetupFlow opens (as the real one will): has_owner() reflects whatever create_owner() wrote."""
        def __init__(self):
            self.owner = None          # {"id", "username", "password_phc", "totp_seed", "recovery_codes"}
            self.cleared = []
            self._next_id = 1

        def has_owner(self, conn):
            return self.owner is not None

        def create_owner(self, conn, username, password_phc, totp_seed, recovery_codes):
            if self.owner is not None:
                raise setup.SetupClosed()
            uid = self._next_id
            self.owner = {"id": uid, "username": username, "password_phc": password_phc,
                          "totp_seed": totp_seed, "recovery_codes": list(recovery_codes)}
            return uid

        def clear_factors(self, conn, user_id):
            self.cleared.append(user_id)

    def _pre(n):
        return Session(id_sha256=h(f"pre{n}"), csrf_token="t", principal=None)

    def run(coro):
        return asyncio.run(coro)


@unittest.skipIf(SKIP, SKIP)
class PureCode(unittest.TestCase):
    def test_round_trip_and_confusables(self):
        code = setup.generate_code()
        self.assertEqual(len(code.replace("-", "")), setup.CODE_CHARS)
        messy = code.lower().replace("-", " ") if "-" in code else code.lower()
        self.assertEqual(setup.hash_code(code), setup.hash_code(messy))

    def test_normalize_rejects_garbage(self):
        self.assertIsNone(setup.normalize_code("too-short"))
        self.assertIsNone(setup.normalize_code("!" * setup.CODE_CHARS))

    def test_check_username(self):
        self.assertEqual(setup.check_username("  alice  "), "alice")
        with self.assertRaises(setup.SetupError):
            setup.check_username("")
        with self.assertRaises(setup.SetupError):
            setup.check_username("x" * 100)


@unittest.skipIf(SKIP, SKIP)
class SetupFlowCase(DbCase):
    def setUp(self):
        super().setUp()
        self.registry = FakeRegistry()
        self.flow = setup.SetupFlow(self.db, self.registry)

    def mint_raw(self, kind="first_owner", user_id=None, ttl_hours=24):
        code = setup.generate_code()
        self.act("setup.mint_code", {"kind": kind, "user_id": user_id, "code_hash": setup.hash_code(code),
                                     "ttl_hours": ttl_hours})
        return code

    def test_status_open_then_closed(self):
        self.assertEqual(run(self.flow.status()), "open")
        self.registry.owner = {"id": 1}
        self.assertEqual(run(self.flow.status()), "closed")

    def totp_code(self, seed):
        import time
        return totp.code_at(seed, totp.step_at(time.time()))

    def test_happy_path_registers_exactly_once(self):
        code = self.mint_raw()
        sess = _pre(1)
        prov = run(self.flow.begin(sess, code, "alice", "a very good passphrase"))
        self.assertIn("otpauth://", prov.provisioning_uri)
        pending = self.flow._pending[sess.id_sha256]
        user_id, codes = run(self.flow.confirm(sess, self.totp_code(pending.totp_seed)))
        self.assertEqual(len(codes), 10)
        self.assertEqual(self.registry.owner["id"], user_id)
        self.assertEqual(self.registry.owner["username"], "alice")
        self.assertNotIn(sess.id_sha256, self.flow._pending)   # pending cleared after success
        row = store.open_setup_code(self.conn, "first_owner", None)
        self.assertIsNone(row)   # the one code is now used, none open

    def test_wrong_code_spends_nothing(self):
        self.mint_raw()
        sess = _pre(1)
        with self.assertRaises(setup.BadCode):
            run(self.flow.begin(sess, "0000-0000-0000-0000-0000-0000-0000-0000", "alice", "a very good passphrase"))
        self.assertIsNone(self.registry.owner)

    def test_bad_password_rejected_before_anything_is_kept(self):
        code = self.mint_raw()
        sess = _pre(1)
        with self.assertRaises(Exception):
            run(self.flow.begin(sess, code, "alice", "short"))
        self.assertNotIn(sess.id_sha256, self.flow._pending)

    def test_wrong_totp_code_leaves_the_setup_code_unspent(self):
        code = self.mint_raw()
        sess = _pre(1)
        run(self.flow.begin(sess, code, "alice", "a very good passphrase"))
        with self.assertRaises(setup.BadTotpCode):
            run(self.flow.confirm(sess, "000000"))
        self.assertIsNone(self.registry.owner)
        row = store.open_setup_code(self.conn, "first_owner", None)
        self.assertIsNotNone(row)
        self.assertIsNone(row["used_at"])

    def test_confirm_without_begin(self):
        with self.assertRaises(setup.NoPendingRegistration):
            run(self.flow.confirm(_pre(9), "000000"))

    def test_setup_closes_once_an_owner_exists(self):
        code = self.mint_raw()
        self.registry.owner = {"id": 1}   # someone else won in the meantime
        with self.assertRaises(setup.SetupClosed):
            run(self.flow.begin(_pre(1), code, "bob", "a very good passphrase"))

    def test_two_racing_confirms_one_winner(self):
        """Two browsers, same global code, different usernames and TOTP seeds: only one call to
        create_owner, the loser's setup code claim or has_owner re-check fails cleanly."""
        code = self.mint_raw()
        a, b = _pre("a"), _pre("b")
        run(self.flow.begin(a, code, "alice", "a very good passphrase"))
        run(self.flow.begin(b, code, "bob", "another good passphrase"))
        pa, pb = self.flow._pending[a.id_sha256], self.flow._pending[b.id_sha256]
        results = []

        def attempt(sess, seed):
            try:
                results.append(("ok", run(self.flow.confirm(sess, self.totp_code(seed)))))
            except setup.SetupError as e:
                results.append(("refused", e.code))
        threads = [threading.Thread(target=attempt, args=(a, pa.totp_seed)),
                  threading.Thread(target=attempt, args=(b, pb.totp_seed))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for kind, r in results if kind == "ok"]
        refusals = [r for kind, r in results if kind == "refused"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(refusals), 1)
        self.assertIn(refusals[0], ("setup_closed", "bad_code"))
        self.assertIn(self.registry.owner["username"], ("alice", "bob"))


@unittest.skipIf(SKIP, SKIP)
class ResetAuthCase(DbCase):
    def test_ends_sessions_clears_factors_and_mints_one_fresh_code(self):
        self.registry = FakeRegistry()
        self.registry.owner = {"id": 7}
        self.act("auth.session_login", {"user_id": 7, "id_sha256": h(1), "csrf_sha256": h("c1"),
                                        "factors": ["password", "totp"]}, actor="user:7", via="dashboard")
        self.act("auth.session_login", {"user_id": 7, "id_sha256": h(2), "csrf_sha256": h("c2"),
                                        "factors": ["password", "totp"]}, actor="user:7", via="dashboard")
        old_code = setup.mint_first_owner_code(self.conn)   # an unrelated open code is untouched
        raw = setup.reset_auth(self.conn, 7, self.registry)
        self.assertEqual(self.registry.cleared, [7])
        self.assertEqual(store.open_auth_sessions(self.conn, 7), [])
        row = store.open_setup_code(self.conn, "reset", 7)
        self.assertEqual(row["code_hash"], setup.hash_code(raw))
        self.assertIsNone(row["used_at"])
        first_owner_row = store.open_setup_code(self.conn, "first_owner", None)
        self.assertIsNotNone(first_owner_row)   # a different (kind, user_id): untouched
        self.assertEqual(setup.hash_code(old_code), first_owner_row["code_hash"])

    def test_reset_auth_called_again_supersedes_the_earlier_code(self):
        self.registry = FakeRegistry()
        first = setup.reset_auth(self.conn, 3, self.registry)
        second = setup.reset_auth(self.conn, 3, self.registry)
        row = store.open_setup_code(self.conn, "reset", 3)
        self.assertEqual(row["code_hash"], setup.hash_code(second))
        self.assertNotEqual(setup.hash_code(first), setup.hash_code(second))


if __name__ == "__main__":
    unittest.main()
