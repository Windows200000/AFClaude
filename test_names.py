#!/usr/bin/env python3
"""Readable tmux names (phase 3b S2, D-147): names.py (pure), the driven.name action, and the
keep-alive's lookup / claim / one-time rename (D-281) against a temp DB and a fake tmux."""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import keepalive as ka  # noqa: E402
import names  # noqa: E402
import store  # noqa: E402

SID = "abcdef01-0000-4000-8000-000000000001"
SID2 = "abcdef02-0000-4000-8000-000000000002"
SID3 = "abcdef03-0000-4000-8000-000000000003"


class Names(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(names.tmux_name("task-manager", project="AFClaude"), "afc-afclaude-task-manager")
        self.assertEqual(names.tmux_name("review"), "afc-review")
        self.assertEqual(names.tmux_name("stall", title="Fix the login bug!", sid=SID), "afc-stall-fix-the-login-bug-abcd")
        self.assertEqual(names.tmux_name("stall", sid=SID), "afc-stall-abcd")                 # no title
        self.assertEqual(names.tmux_name("request", title="Dashboard v2", sid=SID), "afc-work-dashboard-v2-abcd")
        with self.assertRaisesRegex(ValueError, "needs its project"):
            names.tmux_name("task-manager")
        with self.assertRaisesRegex(ValueError, "needs the session id"):
            names.tmux_name("stall", title="x")
        with self.assertRaisesRegex(ValueError, "kind must be"):
            names.tmux_name("task")

    def test_slug(self):
        self.assertEqual(names.slug("Überweisung für Straße"), "uberweisung-fur-strasse")
        self.assertEqual(names.slug("  --Ça va? Øresund / ŁÓDŹ  "), "ca-va-oresund-lodz")
        self.assertEqual(names.slug("日本語のタイトル"), "")
        self.assertEqual(names.slug("a" * 50, 10), "a" * 10)
        self.assertEqual(names.slug("abc def", 4), "abc")                # no hyphen left at the cut
        self.assertEqual(names.tmux_name("task-manager", project="日本"), "afc-task-manager")
        self.assertEqual(names.tmux_name("stall", title="日本語", sid=SID), "afc-stall-abcd")

    def test_charset_and_length(self):
        long = "A very long title, with: punctuation; and MORE words than fit " * 3
        for kind, kw in (("task-manager", {"project": long}), ("stall", {"title": long, "sid": SID}),
                         ("request", {"title": long, "sid": SID})):
            n = names.tmux_name(kind, **kw)
            self.assertLessEqual(len(n), names.MAX_LEN, n)
            self.assertRegex(n, r"^afc-[a-z0-9-]+$")
            self.assertTrue(names.TMUX_RE.match(n), n)
            self.assertFalse(n.endswith("-") or "--" in n, n)
        self.assertTrue(names.tmux_name("task-manager", project=long).endswith("-task-manager"))
        self.assertTrue(names.tmux_name("stall", title=long, sid=SID).endswith("-abcd"))

    def test_suffix_only_on_collision(self):
        self.assertEqual(names.tmux_name("review", taken={"afc-other"}), "afc-review")
        self.assertEqual(names.tmux_name("review", taken={"afc-review"}), "afc-review-2")
        self.assertEqual(names.tmux_name("review", taken={"afc-review", "afc-review-2"}), "afc-review-3")
        long = "x" * 60
        base = names.tmux_name("task-manager", project=long)
        second = names.tmux_name("task-manager", project=long, taken={base})
        self.assertEqual(len(base), 40)
        self.assertEqual(len(second), 40)                                 # the slug is cut to fit the suffix
        self.assertTrue(second.endswith("-task-manager-2"))
        with self.assertRaisesRegex(ValueError, "no free tmux name"):
            names.tmux_name("review", taken={"afc-review"} | {f"afc-review-{i}" for i in range(2, 100)})

    def test_the_whitelist_regex(self):
        for ok in ("ka-0123abcd", "afc-review", "afc-" + "a" * 36, "afc-a-b-1"):
            self.assertTrue(names.TMUX_RE.match(ok), ok)
        for bad in ("ka-0123abc", "ka-0123ABCD", "afc-", "afc-" + "a" * 37, "afc-a b", "afc-a;b", "afc-A",
                    "afc-a\n", "afc-a.b", "afc-a:b", "../afc-a", "Claude", ""):
            self.assertFalse(names.TMUX_RE.match(bad), repr(bad))


class Base(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def do(self, actor="keepalive", via="runner", **params):
        return actions.perform(self.conn, "driven.name", params, actor=actor, via=via)

    def row(self, sid):
        return store.get_driven_session(self.conn, sid)


class DrivenName(Base):
    """driven.name: S2's writer of the stored names (audited; S1's driven.set replaces it)."""
    def test_new_row_rename_end_and_reopen(self):
        r = self.do(session_id=SID, tmux="afc-review", kind="review")
        self.assertEqual((r["kind"], r["tmux"], r["ended_at"]), ("review", "afc-review", None))
        self.do(session_id=SID, tmux="afc-review-2")
        self.do(session_id=SID, ended=True)
        self.assertIsNotNone(self.row(SID)["ended_at"])
        self.do(session_id=SID, ended=False)
        self.assertIsNone(self.row(SID)["ended_at"])
        self.do(session_id=SID, tmux="afc-review-2")                       # no change: no audit row
        audit = [tuple(a) for a in self.conn.execute(
            "SELECT actor, via, action FROM audit_log WHERE target_id=? ORDER BY id", (SID,))]
        self.assertEqual(audit, [("keepalive", "runner", "driven.name")] * 4)

    def test_a_live_name_belongs_to_one_row(self):
        self.do(session_id=SID, tmux="afc-review", kind="review")
        with self.assertRaisesRegex(actions.Conflict, "afc-review is the live name of"):
            self.do(session_id=SID2, tmux="afc-review", kind="review")
        self.do(session_id=SID, ended=True)
        self.do(session_id=SID2, tmux="afc-review", kind="review")         # free once ended
        with self.assertRaises(actions.Conflict):
            self.do(session_id=SID, ended=False)                           # and not live twice

    def test_validation(self):
        for params, err in (({"tmux": "afc-review"}, "needs kind and tmux"),
                            ({"kind": "review"}, "needs kind and tmux"),
                            ({"tmux": "afc-Review", "kind": "review"}, "tmux must be"),
                            ({"tmux": "afc-a;b", "kind": "review"}, "tmux must be"),
                            ({"tmux": "afc-review", "kind": "task"}, "kind must be"),
                            ({"tmux": "afc-review", "kind": "review", "ended": "yes"}, "ended must be"),
                            ({"tmux": "afc-p-task-manager", "kind": "task-manager"}, "needs its project")):
            with self.assertRaisesRegex(ValueError, err, msg=params):
                self.do(session_id=SID, **params)
        with self.assertRaisesRegex(ValueError, "full session UUID"):
            self.do(session_id="abcdef01", tmux="afc-review", kind="review")
        self.assertIsNone(self.row(SID))

    def test_rows_it_does_not_touch(self):
        actions.own_session_add(self.conn, SID, actor="cli", via="cli")
        with self.assertRaisesRegex(ValueError, "kind 'own'"):
            self.do(session_id=SID, tmux="afc-x", kind="review")
        testenv.tmux_row(SID2)                                             # a task-manager row (3b S1)
        with self.assertRaisesRegex(ValueError, "stays one"):
            self.do(session_id=SID2, kind="review")
        self.do(session_id=SID2, tmux="afc-p-task-manager")                # its name may change (the rename)
        self.assertEqual(self.row(SID2)["kind"], "task-manager")

    def test_not_for_mcp(self):
        with self.assertRaisesRegex(actions.Forbidden, "driven.name"):
            self.do(actor="mcp:x", via="mcp", session_id=SID, tmux="afc-review", kind="review")


class FakeTmux:
    """host.run_on_host for tmux calls: `alive` names, `clients` attached ones, renames recorded."""
    def __init__(self, alive=(), clients=()):
        self.alive, self.clients, self.calls = set(alive), set(clients), []

    def __call__(self, argv, **k):
        self.calls.append(argv)
        verb = argv[1]
        target = argv[argv.index("-t") + 1].lstrip("=").rstrip(":")
        if verb == "has-session":
            return types.SimpleNamespace(returncode=0 if target in self.alive else 1, stdout="", stderr="")
        if verb == "list-clients":
            return types.SimpleNamespace(returncode=0, stdout="1700000000\n" if target in self.clients else "",
                                         stderr="")
        if verb == "rename-session" and target in self.alive:
            self.alive = (self.alive - {target}) | {argv[-1]}
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no such session")


class Lookup(Base):
    def setUp(self):
        super().setUp()
        self.old = ka.host.run_on_host
        self.addCleanup(setattr, ka.host, "run_on_host", self.old)

    def test_no_row_is_an_error_never_a_guess(self):
        with self.assertRaisesRegex(ka.NoTmuxName, "abcdef01 has no driven_sessions row"):
            ka.tmux_name(SID)
        self.assertEqual(ka.tmux_label(SID), "no stored tmux name")
        ka.host.run_on_host = FakeTmux(alive={"ka-abcdef01"})
        ok, problems, plan = ka.preflight(SID)
        self.assertEqual((ok, plan), (False, None))
        self.assertIn("no driven_sessions row", problems[0])
        self.assertEqual(ka.host.run_on_host.calls, [])                   # no tmux call under a guessed name
        testenv.tmux_row(SID, "afc-x-task-manager")
        self.assertEqual(ka.tmux_name(SID), "afc-x-task-manager")

    def test_tmux_has_fails_loudly(self):
        ka.host.run_on_host = lambda argv, **k: types.SimpleNamespace(returncode=255, stdout="", stderr="ssh: down")
        with self.assertRaisesRegex(OSError, "rc=255"):
            ka.tmux_has("afc-review")

    def test_claim(self):
        tmux = ka.host.run_on_host = FakeTmux(alive={"afc-review"})       # a tmux session without a row
        self.assertEqual(ka.claim_tmux(SID, "review", arm=False), "afc-review-2")
        self.assertIsNone(self.row(SID))                                   # a dry run writes nothing
        self.assertEqual(ka.claim_tmux(SID, "review"), "afc-review-2")
        self.assertEqual(self.row(SID)["kind"], "review")
        self.assertEqual(ka.claim_tmux(SID, "review"), "afc-review-2")    # a live row keeps its name
        tmux.alive = set()                                                  # everything gone
        self.assertEqual(ka.claim_tmux(SID2, "review"), "afc-review")
        tmux.alive = {"afc-review", "afc-review-2"}                        # SID2's and SID's reviews run
        self.assertEqual(ka.claim_tmux(SID3, "review"), "afc-review-3")
        tmux.alive = set()
        self.do(session_id=SID3, ended=True)
        self.assertEqual(ka.claim_tmux(SID3, "review"), "afc-review")     # SID2's row ends (its tmux is gone)
        self.assertIsNotNone(self.row(SID2)["ended_at"])
        self.assertIsNone(self.row(SID3)["ended_at"])

    def test_claim_never_makes_or_takes_a_task_manager_name(self):
        ka.host.run_on_host = FakeTmux()
        with self.assertRaisesRegex(ka.NoTmuxName, "task-manager abcdef01 has no stored tmux name"):
            ka.claim_tmux(SID, "task-manager", project="P")
        testenv.tmux_row(SID2, "afc-review")                              # a task-manager row, tmux not running
        self.assertEqual(ka.claim_tmux(SID3, "review"), "afc-review-2")
        self.assertIsNone(self.row(SID2)["ended_at"])
        self.assertEqual(ka.claim_tmux(SID2, "stall", title="x"), "afc-review")   # its own row: its name
        actions.own_session_add(self.conn, SID, actor="cli", via="cli")
        with self.assertRaisesRegex(ValueError, "kind 'own'"):
            ka.claim_tmux(SID, "stall")


class Rename(Base):
    """D-281: the live task-manager's tmux session is renamed once after 3b S2, with its row."""
    def setUp(self):
        super().setUp()
        self.old = ka.host.run_on_host
        self.addCleanup(setattr, ka.host, "run_on_host", self.old)
        self.tmux = ka.host.run_on_host = FakeTmux(alive={"ka-abcdef01"})
        testenv.tmux_row(SID)

    def test_rename_and_back(self):
        self.assertEqual(ka.rename_tmux("ka-abcdef01", "afc-afclaude-task-manager"), SID)
        self.assertEqual(self.tmux.alive, {"afc-afclaude-task-manager"})
        self.assertEqual(ka.tmux_name(SID), "afc-afclaude-task-manager")
        audit = self.conn.execute("SELECT actor, action, before, after FROM audit_log WHERE target_id=?",
                                  (SID,)).fetchall()
        self.assertEqual([(a[0], a[1]) for a in audit], [("cli", "driven.name")])
        self.assertIn("ka-abcdef01", audit[0][2])
        ka.rename_tmux("afc-afclaude-task-manager", "ka-abcdef01")        # the rollback before reverting S2
        self.assertEqual((self.tmux.alive, ka.tmux_name(SID)), ({"ka-abcdef01"}, "ka-abcdef01"))

    def test_refusals_change_nothing(self):
        testenv.tmux_row(SID2, "afc-taken", kind="stall")
        for (old, new), err in ((("ka-abcdef01", "afc-Bad"), "not a tmux name"),
                                (("ka-abcdef01", "ka-abcdef01"), "the new name is the old one"),
                                (("afc-nobody", "afc-x"), "0 live driven_sessions rows"),
                                (("ka-abcdef01", "afc-taken"), "live name of abcdef02")):
            with self.assertRaisesRegex(ValueError, err):
                ka.rename_tmux(old, new)
        self.tmux.alive.add("afc-x")
        with self.assertRaisesRegex(ValueError, "afc-x already runs"):
            ka.rename_tmux("ka-abcdef01", "afc-x")
        self.tmux.clients.add("ka-abcdef01")
        with self.assertRaisesRegex(ValueError, "a terminal is attached"):
            ka.rename_tmux("ka-abcdef01", "afc-y")
        self.tmux.clients.clear()
        lock = ka.session_lock(SID)
        try:
            with self.assertRaisesRegex(ValueError, "holds its lock"):
                ka.rename_tmux("ka-abcdef01", "afc-y")
        finally:
            ka.release_lock(lock)
        self.assertNotIn("rename-session", [c[1] for c in self.tmux.calls])
        self.assertEqual(ka.tmux_name(SID), "ka-abcdef01")

    def test_a_failed_row_update_renames_back(self):
        old = actions.perform

        def boom(conn, name, params=None, **k):
            if name == "driven.name":
                raise store.DBError("disk I/O error", "io")
            return old(conn, name, params, **k)
        actions.perform = boom
        self.addCleanup(setattr, actions, "perform", old)
        with self.assertRaisesRegex(RuntimeError, "not renamed .*renamed back to ka-abcdef01: rc=0"):
            ka.rename_tmux("ka-abcdef01", "afc-afclaude-task-manager")
        self.assertEqual(self.tmux.alive, {"ka-abcdef01"})
        self.assertEqual(ka.tmux_name(SID), "ka-abcdef01")


if __name__ == "__main__":
    unittest.main()
