#!/usr/bin/env python3
"""Tests for the dashboard's browser sessions (phase 6a W5, docs/phase6a_plan.md §3.5; W9's route ×
role matrix): the auth_* settings, the auth.session_* actions and their audit rows, lifetimes with a
fake clock (idle, absolute, max sessions, rotation), the pre-session and touch writes without audit
rows; and, with the web stack, the cookie flags, `me`, the CSRF token (missing, wrong, another
session's), a revoked session refused at once, and every endpoint × owner / manager / editor /
viewer / no grant / machine user / nobody against its declared role.

The actions part runs under any python3; the app part needs the venv (Python 3.12, Starlette):
    .venv/bin/python -m unittest test_dashboard_sessions
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import store  # noqa: E402

_MISSING = [m for m in ("starlette", "pydantic", "jsonschema") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'the web stack'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_dashboard_sessions") if _MISSING or sys.version_info < (3, 12) else ""
PUBLIC = "https://afc.example.test"


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
        self.tmp = tempfile.mkdtemp(prefix="afc-sessions-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        self.clock = Clock()
        saved = store._utcnow
        store._utcnow = self.clock
        self.addCleanup(setattr, store, "_utcnow", saved)
        self.conn = store.connect(self.db, create=True)
        self.addCleanup(self.conn.close)

    def act(self, name, params, actor="user:7"):
        return actions.perform(self.conn, name, params, actor=actor, via="dashboard")

    def login(self, n, user=7, replaces=None, factors=("password", "totp")):
        return self.act("auth.session_login", {"user_id": user, "id_sha256": h(n), "csrf_sha256": h(f"c{n}"),
                                               "factors": list(factors), "replaces": replaces}, f"user:{user}")

    def row(self, n):
        return store.get_auth_session(self.conn, h(n))

    def set(self, key, value):
        actions.perform(self.conn, "setting.set", {"key": key, "value": value}, actor="cli", via="cli")

    def audits(self, action):
        return [a for a in store.audit_log(self.conn, 200) if a["action"] == action]


class Settings(DbCase):
    def test_defaults_d162_d179(self):
        s = actions.settings(self.conn)
        self.assertEqual({k: s[k]["value"] for k in s if k.startswith("auth_")},
                         {"auth_idle_timeout_min": 30, "auth_absolute_lifetime_h": 12, "auth_remember_device_days": 7,
                          "auth_reauth_window_min": 10, "auth_max_sessions_per_user": 10,
                          "auth_login_rate": {"account_failures": 5, "ip_failures": 20, "window_min": 15}})
        self.assertTrue(all(s[k]["section"] == "auth" for k in s if k.startswith("auth_")))

    def test_checks(self):
        for key, bad in (("auth_idle_timeout_min", 4), ("auth_idle_timeout_min", 30.5), ("auth_max_sessions_per_user", 51),
                         ("auth_remember_device_days", True), ("auth_login_rate", {"account_failures": 5}),
                         ("auth_login_rate", {"account_failures": 0, "ip_failures": 20, "window_min": 15})):
            with self.assertRaises(ValueError, msg=(key, bad)):
                self.set(key, bad)
        self.set("auth_remember_device_days", 0)
        self.assertEqual(actions.AuthLifetimes(self.conn).remember_device, timedelta(0))

    def test_an_invalid_saved_value_is_an_error_not_the_default(self):
        self.conn.execute("INSERT INTO settings(key, value, version, updated_at, updated_by) VALUES (?, ?, 1, ?, 'x')",
                          ("auth_idle_timeout_min", json.dumps(9999), store.now_iso()))
        with self.assertRaisesRegex(ValueError, "auth_idle_timeout_min"):
            actions.AuthLifetimes(self.conn)


class Actions(DbCase):
    def test_login_replaces_the_pre_session_and_is_audited(self):
        actions.auth_session_start_pre(self.conn, h("pre"), h("cpre"), "Firefox")
        res = self.login(1, replaces=h("pre"))
        self.assertEqual(res["ended"], {str(self.row("pre")["id"]): "rotated"})
        r = self.row(1)
        self.assertEqual((r["kind"], r["user_id"], r["factors"], r["authenticated_at"]),
                         ("full", 7, ["password", "totp"], r["created_at"]))
        audit = self.audits("auth.session_login")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["actor"], "user:7")
        self.assertNotIn(h(1), json.dumps(audit[0]))           # row ids in the audit, never a hash

    def test_only_the_user_logs_in_as_themself(self):
        with self.assertRaises(actions.Forbidden):
            self.act("auth.session_login", {"user_id": 8, "id_sha256": h(1), "csrf_sha256": h(2),
                                            "factors": ["oidc"]}, "user:7")
        with self.assertRaises(ValueError):
            self.login(1, factors=("password", "magic"))
        with self.assertRaises(ValueError):
            self.act("auth.session_login", {"user_id": 7, "id_sha256": "short", "csrf_sha256": h(2),
                                            "factors": ["oidc"]})

    def test_max_sessions_ends_the_oldest(self):
        self.set("auth_max_sessions_per_user", 2)
        for n in range(3):
            self.login(n)
            self.clock.advance(seconds=5)
        self.assertEqual(self.row(0)["end_reason"], "evicted")
        self.assertIsNone(self.row(1)["ended_at"])
        self.assertIsNone(self.row(2)["ended_at"])
        self.login(9, user=8)                                   # another user's count is its own
        self.assertIsNone(self.row(1)["ended_at"])

    def test_login_ends_sessions_past_a_lifetime(self):
        self.login(1)
        self.clock.advance(minutes=31)
        self.login(2)
        self.assertEqual(self.row(1)["end_reason"], "idle")

    def test_lifetimes(self):
        lt = actions.AuthLifetimes(self.conn)
        self.login(1)
        state = lambda: actions.auth_session_state(self.row(1), lt)     # noqa: E731
        self.clock.advance(minutes=30)
        self.assertEqual(state(), "live")
        self.clock.advance(seconds=1)
        self.assertEqual(state(), "idle")
        for _ in range(12 * 6):                                 # active every 10 minutes: still ends at 12 h
            actions.auth_session_touch(self.conn, self.row(1)["id"])
            self.clock.advance(minutes=10)
        self.assertEqual(state(), "expired")

    def test_touch_writes_at_most_once_a_minute(self):
        self.login(1)
        sid = self.row(1)["id"]
        self.clock.advance(seconds=30)
        self.assertFalse(actions.auth_session_touch(self.conn, sid))
        self.clock.advance(seconds=31)
        self.assertTrue(actions.auth_session_touch(self.conn, sid))
        self.assertEqual(self.row(1)["last_seen_at"], store.now_iso())
        self.assertEqual(len(store.audit_log(self.conn, 50)), 1)     # the login's only

    def test_pre_sessions_have_no_audit_row_and_are_pruned(self):
        actions.auth_session_start_pre(self.conn, h("a"), h("ca"))
        self.clock.advance(minutes=31)
        actions.auth_session_start_pre(self.conn, h("b"), h("cb"))
        self.assertIsNone(self.row("a"))
        self.assertIsNotNone(self.row("b"))
        self.assertEqual(store.audit_log(self.conn, 50), [])

    def test_rotate(self):
        self.login(1)
        created = self.row(1)["created_at"]
        self.clock.advance(minutes=20)
        self.act("auth.session_rotate", {"id_sha256": h(1), "new_id_sha256": h(2), "new_csrf_sha256": h("c2"),
                                         "factors": ["passkey_hw"], "authenticated": True})
        old, new = self.row(1), self.row(2)
        self.assertEqual(old["end_reason"], "rotated")
        self.assertEqual((new["created_at"], new["factors"], new["authenticated_at"]),
                         (created, ["passkey_hw"], store.now_iso()))
        self.clock.advance(minutes=1)
        self.act("auth.session_rotate", {"id_sha256": h(2), "new_id_sha256": h(3), "new_csrf_sha256": h("c3")})
        self.assertEqual(self.row(3)["authenticated_at"], new["authenticated_at"])   # no step-up: window kept
        with self.assertRaises(store.InvalidTransition):
            self.act("auth.session_rotate", {"id_sha256": h(1), "new_id_sha256": h(4), "new_csrf_sha256": h("c4")})
        with self.assertRaises(actions.Forbidden):
            self.act("auth.session_rotate", {"id_sha256": h(3), "new_id_sha256": h(4), "new_csrf_sha256": h("c4")},
                     "user:8")
        self.clock.advance(minutes=31)
        with self.assertRaisesRegex(store.InvalidTransition, "lifetime"):
            self.act("auth.session_rotate", {"id_sha256": h(3), "new_id_sha256": h(4), "new_csrf_sha256": h("c4")})

    def test_logout_and_revoke(self):
        for n in range(3):
            self.login(n)
        self.act("auth.session_logout", {"id_sha256": h(0)})
        self.assertEqual(self.row(0)["end_reason"], "logout")
        res = self.act("auth.session_revoke", {"session_id": self.row(1)["id"]}, "cli")
        self.assertEqual(res["ended"], [self.row(1)["id"]])
        self.assertEqual(self.act("auth.session_revoke", {"user_id": 7}, "cli")["ended"], [self.row(2)["id"]])
        self.assertEqual(self.act("auth.session_revoke", {"user_id": 7}, "cli")["ended"], [])   # no-op
        self.assertEqual(len(self.audits("auth.session_revoke")), 2)
        with self.assertRaises(ValueError):
            self.act("auth.session_revoke", {"session_id": 1, "user_id": 7}, "cli")


if not SKIP:
    from starlette.testclient import TestClient  # noqa: E402

    from dashboard import routes, sessions  # noqa: E402
    from dashboard.api_types import Model  # noqa: E402
    from dashboard.app import create_app  # noqa: E402
    from dashboard.auth import factors  # noqa: E402
    from dashboard.routes import ENDPOINTS, Endpoint, Principal, Reply, Session  # noqa: E402

    class Ok(Model):
        ok: bool

    class Empty(Model):
        pass

    async def _ok(call):
        return Reply(Ok(ok=True))

    # one test write, and one read per minimum role (the matrix covers every level, not only those
    # ENDPOINTS uses today)
    EXTRA = (Endpoint("t_write", "POST", "/api/v1/t/write", "session", Empty, Ok, _ok, "x", role="signed_in"),
             *(Endpoint(f"t_{r}", "GET", f"/api/v1/t/{r}", "session", None, Ok, _ok, "x", role=r)
               for r in routes.ROLE_RANK))
    PRINCIPALS = {"owner": Principal(1, role="owner"), "manager": Principal(2, role="manager"),
                  "editor": Principal(3, role="editor"), "viewer": Principal(4, role="viewer"),
                  "no grant": Principal(5), "machine": Principal(6, kind="machine", role="owner")}


def _dist(test):
    d = tempfile.mkdtemp(prefix="afc-dist-")
    test.addCleanup(shutil.rmtree, d, True)
    os.makedirs(os.path.join(d, "assets"))
    with open(os.path.join(d, "index.html"), "w") as fh:
        fh.write("<!doctype html><title>AFClaude</title>")
    return d


@unittest.skipIf(bool(SKIP), SKIP)
class App(DbCase):
    def setUp(self):
        super().setUp()
        self.principals = {7: Principal(7, role="editor"), 9: Principal(9, kind="machine")}

        async def roles(user_id):
            return self.principals[user_id]
        self.store = sessions.SessionStore(self.db, roles)
        app = create_app(PUBLIC, _dist(self), self.store, db_path=self.db, endpoints=ENDPOINTS + EXTRA)
        self.c = TestClient(app, base_url=PUBLIC, raise_server_exceptions=False)

    def me(self, client=None):
        r = (client or self.c).get("/api/v1/me")
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def write(self, token, client=None):
        hdr = {"Content-Type": "application/json", "Origin": PUBLIC}
        if token is not None:
            hdr["X-CSRF-Token"] = token
        return (client or self.c).post("/api/v1/t/write", content="{}", headers=hdr)

    def sign_in(self, user=7, client=None):
        """A login as W6 will do it: the store's login on the browser's pre-session, then its cookie."""
        c = client or self.c
        c.cookies.clear()
        self.me(c)
        self.pre_raw = c.cookies.get(sessions.COOKIE)
        req = _Req(self.pre_raw)
        import asyncio
        _, cookie = asyncio.run(self.store.login(req, user, ["password", "totp"]))
        raw = cookie.split(";")[0].split("=", 1)[1]
        c.cookies.clear()
        c.cookies.set(sessions.COOKIE, raw)
        return sessions.csrf_token(raw)

    def test_me_starts_a_pre_session_with_the_cookie_flags(self):
        r = self.me()
        body = r.json()
        self.assertEqual((body["user"], body["time_zone"]), (None, "Europe/Berlin"))
        cookie = r.headers["set-cookie"]
        raw = re.match(r"__Host-afc_session=([A-Za-z0-9_-]{43}); ", cookie).group(1)
        self.assertEqual(set(cookie.split("; ")[1:]), {"Path=/", "Max-Age=43200", "Secure", "HttpOnly", "SameSite=Lax"})
        self.assertEqual(body["csrf_token"], sessions.csrf_token(raw))
        self.assertEqual(self.store_row(raw)["kind"], "pre")
        with open(self.db, "rb") as fh:
            data = fh.read()
        self.assertNotIn(raw.encode(), data)                         # only hashes are stored
        self.assertNotIn(body["csrf_token"].encode(), data)
        again = self.me()
        self.assertNotIn("set-cookie", again.headers)
        self.assertEqual(again.json()["csrf_token"], body["csrf_token"])

    def store_row(self, raw):
        return store.get_auth_session(self.conn, sessions.id_sha256(raw))

    def test_login_rotates_the_id_and_me_shows_the_user(self):
        token = self.sign_in()
        self.assertNotEqual(token, sessions.csrf_token(self.pre_raw))
        self.assertNotEqual(self.c.cookies.get(sessions.COOKIE), self.pre_raw)
        self.assertEqual(self.store_row(self.pre_raw)["end_reason"], "rotated")
        self.assertEqual(self.me().json()["user"], {"id": 7, "role": "editor"})

    def test_csrf_token(self):
        self.me()
        self.assertEqual(self.write("x").status_code, 403)          # a wrong token with a pre-session...
        token = self.sign_in()
        other = TestClient(self.c.app, base_url=PUBLIC)
        other_token = self.me(other).json()["csrf_token"]
        for bad in (None, "", "wrong", other_token, token + "x"):
            r = self.write(bad)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"), bad)
        self.assertEqual(self.write(token).json(), {"ok": True})
        self.assertEqual(self.write(token, other).status_code, 403)   # ...and a right token, wrong session

    def test_a_write_without_any_session(self):
        fresh = TestClient(self.c.app, base_url=PUBLIC)
        self.assertEqual(self.write("x", fresh).status_code, 401)    # a session endpoint: sign in first

    def test_revoked_session_refused_at_once(self):
        self.sign_in()
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 200)
        actions.perform(self.conn, "auth.session_revoke", {"user_id": 7}, actor="cli", via="cli")
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 401)
        self.assertIsNone(self.me().json()["user"])

    def test_idle_and_absolute_lifetime(self):
        self.sign_in()
        self.clock.advance(minutes=29)
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 200)   # touches last_seen
        self.clock.advance(minutes=29)
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 200)
        self.clock.advance(minutes=31)
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 401)
        self.sign_in()
        for _ in range(12 * 4 + 1):                             # active every 15 min: ends after 12 h
            self.clock.advance(minutes=15)
            self.c.get("/api/v1/t/signed_in")
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 401)

    def test_touch_once_a_minute(self):
        self.sign_in()
        raw = self.c.cookies.get(sessions.COOKIE)
        seen = self.store_row(raw)["last_seen_at"]
        self.clock.advance(seconds=59)
        self.c.get("/api/v1/t/signed_in")
        self.assertEqual(self.store_row(raw)["last_seen_at"], seen)
        self.clock.advance(seconds=1)
        self.c.get("/api/v1/t/signed_in")
        self.assertEqual(self.store_row(raw)["last_seen_at"], store.now_iso())

    def test_rotate_and_logout(self):
        import asyncio
        self.sign_in()
        old_raw = self.c.cookies.get(sessions.COOKIE)
        sess = asyncio.run(self.store.authenticate(_Req(old_raw)))
        new, cookie = asyncio.run(self.store.rotate(sess, step_up=True))
        new_raw = cookie.split(";")[0].split("=", 1)[1]
        self.assertIsNone(asyncio.run(self.store.authenticate(_Req(old_raw))))
        cur = asyncio.run(self.store.authenticate(_Req(new_raw)))
        self.assertEqual((cur.csrf_token, cur.principal), (new.csrf_token, Principal(7, role="editor")))
        cleared = asyncio.run(self.store.logout(cur))
        self.assertIn("Max-Age=0", cleared)
        self.assertIsNone(asyncio.run(self.store.authenticate(_Req(new_raw))))

    def test_machine_user_refused(self):
        self.sign_in(user=9)
        self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 403)
        self.assertEqual(self.c.get("/api/v1/me").status_code, 403)

    def test_without_5a_a_signed_in_request_fails_loudly(self):
        self.sign_in()
        self.store.roles = sessions.no_roles
        with self.assertLogs("afclaude.web", "ERROR"):
            self.assertEqual(self.c.get("/api/v1/t/signed_in").status_code, 500)

    def test_factor_names_match_the_factor_rule(self):
        self.assertEqual(set(actions.AUTH_FACTORS), factors.FACTORS)


class _Req:
    """The part of a Starlette request the store reads (cookies, headers)."""
    def __init__(self, raw):
        self.cookies = {sessions.COOKIE: raw} if raw else {}
        self.headers = {"user-agent": "test"}


@unittest.skipIf(bool(SKIP), SKIP)
class RoleMatrix(unittest.TestCase):
    """W9: every endpoint × every kind of principal against its declared role."""

    def setUp(self):
        self.who = None

        test = self

        class Fake:
            async def authenticate(self, request):
                if test.who == "nobody":
                    return Session("0" * 64, "tok", None)       # a pre-session
                return Session("0" * 64, "tok", PRINCIPALS[test.who])

            async def start_pre(self, request):
                raise AssertionError("not reached: every request has a session here")
        self.c = TestClient(create_app(PUBLIC, _dist(self), Fake(), db_path=testenv.DB, endpoints=ENDPOINTS + EXTRA),
                            base_url=PUBLIC, raise_server_exceptions=False)

    def expected(self, ep, who):
        if ep.name == "me" and who == "machine":
            return 403
        if ep.access == "public":
            return 200
        if who == "nobody":
            return 401
        p = PRINCIPALS[who]
        return 200 if p.kind == "person" and routes.role_allows(p.role, ep.role) else 403

    def test_matrix(self):
        self.assertEqual({e.name: e.role for e in ENDPOINTS if e.access == "session"}, {"api_schema": "viewer"})
        for ep in ENDPOINTS + EXTRA:
            for who in (*PRINCIPALS, "nobody"):
                self.who = who
                if ep.method == "GET":
                    r = self.c.get(ep.path)
                else:
                    r = self.c.request(ep.method, ep.path, content="{}", headers={
                        "Content-Type": "application/json", "Origin": PUBLIC, "X-CSRF-Token": "tok"})
                self.assertEqual(r.status_code, self.expected(ep, who), (ep.name, who, r.text))

    def test_unknown_role_refused_in_the_table(self):
        with self.assertRaises(ValueError):
            Endpoint("x", "GET", "/api/v1/x", "session", None, Ok, _ok, "x", role="admin")


if __name__ == "__main__":
    unittest.main()
