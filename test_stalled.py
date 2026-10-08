#!/usr/bin/env python3
"""Offline tests for stalled.py / store.py (temp dirs + temp DBs).
Real-host tests only READ files under ~/.claude/projects."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import keepalive as ka  # noqa: E402
import stalled  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def stall(uuid, ts, reset="2:49am", kind="session"):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "isSidechain": False,
            "isApiErrorMessage": True, "error": "rate_limit", "cwd": "/home/x/proj",
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": f"You've hit your {kind} limit · resets {reset} (UTC)"}]}}


def user(uuid, ts, text="do things", cwd="/home/x/proj"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "cwd": cwd, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def reply(uuid, ts):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "cwd": "/home/x/proj", "isSidechain": False,
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]}}


TRAILING = [{"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-26T01:10:00.100Z"},
            {"type": "last-prompt", "lastPrompt": "x"}, {"type": "cost-state"}, {"type": "atis-latch"}]
U1 = user("u1", "2026-09-26T01:00:00.000Z")
A1 = reply("a1", "2026-09-26T01:05:00.000Z")
S1 = stall("s1", "2026-09-26T01:10:00.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.proj, "-proj"))
        self.conn = store.connect(os.path.join(self.tmp.name, "db", "t.db"), create=True)
        self.own_list = os.path.join(self.tmp.name, "own_sessions.txt")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def path(self, sid=SID, proj="-proj"):
        return os.path.join(self.proj, proj, f"{sid}.jsonl")

    def write(self, entries, sid=SID, mode="w", raw=""):
        with open(self.path(sid), mode) as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
            fh.write(raw)

    def scan(self):
        return stalled.scan(self.conn, self.proj, self.own_list)

    def sess(self, sid=SID):
        return store.get_session(self.conn, sid)


class Schema(Base):
    def test_idempotent(self):
        store.init(self.conn)
        store.init(self.conn)
        c2 = store.connect(os.path.join(self.tmp.name, "db", "t.db"))
        c2.close()
        self.assertEqual(store.get_meta(self.conn, "schema_version"), str(store.SCHEMA_VERSION))

    def test_upsert_partial_update_keeps_other_fields(self):
        store.upsert_session(self.conn, SID, title="a", cwd="/x")
        store.upsert_session(self.conn, SID, cwd="/y")
        s = self.sess()
        self.assertEqual((s["title"], s["cwd"]), ("a", "/y"))
        with self.assertRaises(ValueError):
            store.upsert_session(self.conn, SID, nope=1)

    def test_hit_unique(self):
        store.upsert_session(self.conn, SID)
        self.assertTrue(store.add_hit(self.conn, SID, "e1", Z("2026-09-26T01:00:00Z"), "session", None, "t"))
        self.assertFalse(store.add_hit(self.conn, SID, "e1", Z("2026-09-26T01:00:00Z"), "session", None, "t"))
        self.assertEqual(len(store.session_history(self.conn, SID)), 1)


class Detect(Base):
    def test_stalled_with_trailing_metadata(self):
        self.write([U1, A1, S1] + TRAILING)
        st = self.scan()
        s = self.sess()
        self.assertEqual(s["stalled"], 1)
        self.assertEqual(s["stall_kind"], "session")
        self.assertEqual(s["stall_reset_at"], "2026-09-26T02:49:00Z")
        self.assertEqual(s["stalled_since"], "2026-09-26T01:10:00Z")
        self.assertEqual(s["last_msg_uuid"], "s1")
        self.assertEqual(s["last_activity"], "2026-09-26T01:10:00.100Z")   # trailing system line
        self.assertEqual(s["first_seen"], "2026-09-26T01:00:00.000Z")
        self.assertEqual(s["cwd"], "/home/x/proj")
        self.assertEqual((st["stalled"], st["hits"], st["sessions"]), (1, 1, 1))
        self.assertEqual([r["session_id"] for r in store.stalled_sessions(self.conn)], [SID])

    def test_resumed_after_stall(self):
        self.write([U1, S1] + TRAILING + [user("u2", "2026-09-26T03:00:00.000Z")])
        self.scan()
        s = self.sess()
        self.assertEqual(s["stalled"], 0)
        self.assertIsNone(s["stall_kind"])
        self.assertEqual(len(store.session_history(self.conn, SID)), 1)   # the hit is still history
        self.assertEqual(store.stalled_sessions(self.conn), [])
        self.assertEqual([r["session_id"] for r in store.stalled_sessions(self.conn, include_resolved=True)], [SID])

    def test_weekly_with_date(self):
        self.write([U1, stall("w1", "2026-09-26T01:10:00.000Z", reset="Oct 1, 4:59pm", kind="weekly")])
        self.scan()
        s = self.sess()
        self.assertEqual((s["stall_kind"], s["stall_reset_at"]), ("weekly", "2026-10-01T16:59:00Z"))

    def test_other_synthetic_not_a_hit(self):
        other = dict(stall("x1", "2026-09-26T01:10:00.000Z"), error=None,
                     message={"role": "assistant", "model": "<synthetic>",
                              "content": [{"type": "text", "text": "No response requested."}]})
        self.write([U1, other])
        st = self.scan()
        self.assertEqual((st["hits"], self.sess()["stalled"]), (0, 0))

    def test_subagent_transcripts_ignored(self):
        sub = os.path.join(self.proj, "-proj", SID, "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-1.jsonl"), "w") as fh:
            fh.write(json.dumps(S1) + "\n")
        self.write([U1, A1])
        st = self.scan()
        self.assertEqual((st["files"], st["hits"], st["sessions"]), (1, 0, 1))

    def test_titles_priority(self):
        self.write([{"type": "ai-title", "aiTitle": "ai name"}, U1])
        self.scan()
        self.assertEqual(self.sess()["title"], "ai name")
        self.write([{"type": "custom-title", "customTitle": "my name"},
                    {"type": "ai-title", "aiTitle": "later ai name"}], mode="a")
        self.scan()
        self.assertEqual(self.sess()["title"], "my name")
        self.write([{"type": "agent-name", "agentName": "agent"}], mode="a")
        self.scan()
        self.assertEqual(self.sess()["title"], "my name")
        self.write([{"type": "custom-title", "customTitle": "renamed"}], mode="a")
        self.scan()
        self.assertEqual(self.sess()["title"], "renamed")

    def test_own_flag(self):
        self.write([user("u1", "2026-09-26T01:00:00.000Z", cwd="/mnt/x/work/AFClaude")], sid=SID)
        self.write([user("u1", "2026-09-26T01:00:00.000Z", cwd="/mnt/x/work/keepalive/probe")], sid=SID2)
        sid3 = "00000000-0000-4000-8000-000000000003"
        self.write([{"type": "custom-title", "customTitle": "ka-night"}, U1], sid=sid3)
        sid4 = "00000000-0000-4000-8000-000000000004"
        self.write([U1], sid=sid4)
        self.scan()
        self.assertEqual([self.sess(s)["own"] for s in (SID, SID2, sid3, sid4)], [1, 1, 1, 0])
        with open(self.own_list, "w") as fh:
            fh.write(sid4 + "\n")
        self.scan()   # no transcript changed, own list did
        self.assertEqual(self.sess(sid4)["own"], 1)


class Incremental(Base):
    def test_append_stall_then_resume(self):
        self.write([U1, A1])
        st = self.scan()
        self.assertEqual((st["stalled"], st["hits"]), (0, 0))
        size1 = os.path.getsize(self.path())
        self.assertEqual(self.sess()["last_scanned_offset"], size1)

        # nothing new -> file not read
        st = self.scan()
        self.assertEqual((st["changed"], st["bytes"]), (0, 0))

        # second scan: a stall (plus metadata) is appended
        self.write([S1] + TRAILING, mode="a")
        st = self.scan()
        self.assertEqual(st["changed"], 1)
        self.assertEqual(st["bytes"], os.path.getsize(self.path()) - size1)   # only the new bytes
        self.assertEqual((st["stalled"], st["new_hits"]), (1, 1))
        self.assertEqual(self.sess()["stall_reset_at"], "2026-09-26T02:49:00Z")

        # metadata-only append keeps the stall
        self.write([{"type": "cost-state"}], mode="a")
        st = self.scan()
        self.assertEqual((st["changed"], st["stalled"]), (1, 1))

        # third scan: the resume is appended
        self.write([user("u2", "2026-09-26T03:00:00.000Z"), reply("a2", "2026-09-26T03:00:05.000Z")], mode="a")
        st = self.scan()
        self.assertEqual((st["stalled"], st["new_hits"], st["hits"]), (0, 0, 1))
        self.assertEqual(self.sess()["last_msg_uuid"], "a2")
        self.assertEqual(self.sess()["first_seen"], "2026-09-26T01:00:00.000Z")

    def test_partial_line_waits_for_newline(self):
        self.write([U1])
        line = json.dumps(S1)
        self.write([], mode="a", raw=line[:40])        # half-written line
        st = self.scan()
        self.assertEqual(st["stalled"], 0)
        off = self.sess()["last_scanned_offset"]
        self.assertLess(off, os.path.getsize(self.path()))
        self.write([], mode="a", raw=line[40:] + "\n")  # completed
        st = self.scan()
        self.assertEqual((st["stalled"], st["hits"]), (1, 1))
        self.assertEqual(self.sess()["last_scanned_offset"], os.path.getsize(self.path()))

    def test_shrink_rescans_from_zero(self):
        self.write([U1, A1, S1] + TRAILING + [{"type": "custom-title", "customTitle": "old"}])
        self.scan()
        self.assertEqual(self.sess()["stalled"], 1)
        # file replaced by a shorter one (e.g. rewritten): not stalled, different title
        self.write([{"type": "ai-title", "aiTitle": "new"}, U1, A1])
        st = self.scan()
        s = self.sess()
        self.assertEqual(st["rescanned"], 1)
        self.assertEqual((s["stalled"], s["title"], s["last_msg_uuid"]), (0, "new", "a1"))
        self.assertEqual(s["last_scanned_offset"], os.path.getsize(self.path()))
        self.assertEqual(st["hits"], 1)   # history survives

    def test_rescan_same_stall_no_duplicate_hit(self):
        self.write([U1, S1])
        self.scan()
        self.write([S1])   # shorter, same notice uuid
        st = self.scan()
        self.assertEqual((st["rescanned"], st["hits"], st["new_hits"], st["stalled"]), (1, 1, 0, 1))

    def test_matches_keepalive_last_message(self):
        """Incremental result == keepalive's whole-file detection, at every prefix."""
        entries = [U1, A1, S1] + TRAILING + [user("u2", "2026-09-26T03:00:00.000Z"),
                                             stall("s2", "2026-09-26T04:00:00.000Z", reset="5pm"),
                                             {"type": "last-prompt"}]
        open(self.path(), "w").close()
        for e in entries:
            self.write([e], mode="a")
            self.scan()
            want = ka.stall_info(ka.last_message(self.path()))
            s = self.sess()
            self.assertEqual(bool(s["stalled"]), want is not None, e)
            if want:
                self.assertEqual(s["stall_reset_at"], store.iso(want["reset_from_text"]))
        self.assertEqual(len(store.session_history(self.conn, SID)), 2)


class CLI(Base):
    def test_list_and_history(self):
        import contextlib
        import io
        self.write([U1, S1] + TRAILING)
        db = os.path.join(self.tmp.name, "db", "t.db")
        for argv in (["scan"], ["list"], ["list", "--all", "--json"], ["history", SID[:12]]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = stalled.main(["--db", db, "--projects-dir", self.proj] + argv)
            self.assertEqual(rc, 0, argv)
            if argv[0] == "list" and "--json" in argv:
                rows = json.loads(out.getvalue())
                self.assertEqual(rows[0]["session_id"], SID)
                self.assertEqual(rows[0]["reset_at"], "2026-09-26T02:49:00Z")
                self.assertEqual(rows[0]["reset_at_berlin"], "2026-09-26 04:49 CEST")
                self.assertEqual(rows[0]["hits"], 1)
            elif argv[0] == "history":
                self.assertIn("STALLED", out.getvalue())
                self.assertIn("1 limit hit(s)", out.getvalue())


class RealHost(unittest.TestCase):
    """READ-ONLY against ~/.claude/projects; the DB is a temp file."""
    REAL = os.path.expanduser("~/.claude/projects")

    def setUp(self):
        if not os.path.isdir(self.REAL):
            self.skipTest("no ~/.claude/projects")
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(os.path.join(self.tmp.name, "real.db"), create=True)
        stalled.scan(self.conn, self.REAL, os.path.join(self.tmp.name, "none.txt"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_a954f3c8_is_stalled(self):
        sid = "a954f3c8-e15e-41dd-9b25-b2f8b1a88a06"
        s = store.get_session(self.conn, sid)
        if not s:
            self.skipTest("transcript gone")
        self.assertEqual(s["stalled"], 1)
        self.assertEqual(s["stall_kind"], "session")
        self.assertEqual(s["stall_reset_at"], "2026-09-25T17:00:00Z")
        self.assertIn(sid, [r["session_id"] for r in store.stalled_sessions(self.conn)])

    def test_all_sessions_match_keepalive(self):
        """Every settled top-level transcript: same verdict as keepalive's full read."""
        for s in self.conn.execute("SELECT * FROM sessions").fetchall():
            p = s["path"]
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size != s["last_scanned_size"] or time.time() - st.st_mtime < 60:
                continue  # still being written
            want = ka.stall_info(ka.last_message(p))
            self.assertEqual(bool(s["stalled"]), want is not None, p)
            if want:
                self.assertEqual(s["stall_reset_at"], store.iso(want["reset_from_text"]), p)


if __name__ == "__main__":
    unittest.main()
