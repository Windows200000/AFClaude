import json
import os
import tempfile
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import usage_report as ur

OLD, NEW, NEWER = ("aaaaaaaa-0000-4000-8000-00000000000%d" % i for i in (1, 2, 3))
OTHER = "bbbbbbbb-0000-4000-8000-000000000001"


def turn(ts, mid, out):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": "m", "usage": {"input_tokens": 1, "output_tokens": out}}}


class SessionChain(unittest.TestCase):
    """D-328 fix round 3: a /clear goes on under a new id; the per-session report counts every id of
    the conversation's "cleared" chain (transcripts, subagents, the last recorded report)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        st = {"cleared": {OLD: {"to": NEW, "at": "t"}, NEW: {"to": NEWER, "at": "t"}}}
        for p in (mock.patch.object(ur.ka, "PROJECTS_DIR", self.tmp.name),
                  mock.patch.object(ur.ka, "load_state", lambda: st)):
            p.start()
            self.addCleanup(p.stop)
        proj = os.path.join(self.tmp.name, "-p")
        os.makedirs(proj)
        for sid, n in ((OLD, 1), (NEW, 2), (NEWER, 4), (OTHER, 100)):
            with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
                f.write(json.dumps(turn(f"2026-11-0{n % 7 + 1}T00:00:00+00:00", "m" + sid, n)) + "\n")
            sub = os.path.join(proj, sid, "subagents")
            os.makedirs(sub)
            with open(os.path.join(sub, f"agent-a{sid[-8:]}.jsonl"), "w") as f:
                f.write(json.dumps(turn("2026-11-01T00:00:00+00:00", "s" + sid, n)) + "\n")

    def test_ids_both_ways(self):
        for sid in (OLD, NEW, NEWER):
            self.assertEqual(ur.session_ids(sid), [OLD, NEW, NEWER])
        self.assertEqual(ur.session_ids(OTHER), [OTHER])

    def test_rows_count_the_whole_chain(self):
        rows, main = ur.build_rows(NEWER, None)
        self.assertEqual((main["turns"], main["tokens"]["out"]), (3, 7))
        self.assertEqual(sorted(r["id"] for r in rows), sorted("a" + s[-8:] for s in (OLD, NEW, NEWER)))

    def test_last_recorded_report_under_an_earlier_id(self):
        lines = [json.dumps({"at": "2026-11-01T00:00:00+00:00", "session": OLD}),
                 json.dumps({"at": "2026-11-02T00:00:00+00:00", "session": OTHER})]
        with mock.patch.object(ur.telemetry, "lines", lambda path: lines):
            self.assertEqual(ur.last_recorded_ts(NEWER).isoformat(), "2026-11-01T00:00:00+00:00")


class LoadMetaMount(unittest.TestCase):
    """Mount mode: a missing agent-<id>.meta.json is {} (older agents have none); one that is there but
    unreadable, invalid or not a JSON object raises (D-235), the same TxError as bridge mode."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sub = os.path.join(self.tmp.name, "-p", NEW, "subagents")
        os.makedirs(self.sub)
        for p in (mock.patch.object(ur.ka, "PROJECTS_DIR", self.tmp.name),
                  mock.patch.object(ur.ka, "load_state", lambda: {}),
                  mock.patch.object(ur.txsource, "bridge_on", lambda: False)):
            p.start()
            self.addCleanup(p.stop)

    def put(self, name, data):
        with open(os.path.join(self.sub, name), "wb") as f:
            f.write(data)

    def test_valid_object(self):
        self.put("agent-a1.meta.json", b'{"description": "job", "agentType": "x"}')
        self.assertEqual(ur.load_meta(NEW, "a1"), {"description": "job", "agentType": "x"})

    def test_missing_file_is_empty(self):
        self.assertEqual(ur.load_meta(NEW, "nope"), {})

    def test_invalid_json_raises(self):
        self.put("agent-a1.meta.json", b'{"description": ')
        with self.assertRaisesRegex(ur.txsource.TxError, "agent-a1.meta.json cannot be read"):
            ur.load_meta(NEW, "a1")

    def test_undecodable_bytes_raise(self):
        self.put("agent-a1.meta.json", b"\xff\xfe\x00")
        with self.assertRaises(ur.txsource.TxError):
            ur.load_meta(NEW, "a1")

    def test_non_object_raises(self):
        for i, data in enumerate((b"[]", b'"x"', b"null", b"3")):
            self.put(f"agent-n{i}.meta.json", data)
            with self.assertRaisesRegex(ur.txsource.TxError, "not a JSON object"):
                ur.load_meta(NEW, f"n{i}")

    def test_unreadable_raises(self):
        os.mkdir(os.path.join(self.sub, "agent-d1.meta.json"))      # open() fails with IsADirectoryError (also as root)
        with self.assertRaisesRegex(ur.txsource.TxError, "cannot be read"):
            ur.load_meta(NEW, "d1")

    def test_a_bad_meta_fails_the_whole_report(self):
        self.put("agent-a1.jsonl", json.dumps(turn("2026-11-01T00:00:00+00:00", "m1", 1)).encode() + b"\n")
        self.put("agent-a1.meta.json", b"{")
        with self.assertRaises(ur.txsource.TxError):
            ur.build_rows(NEW, None)


class ParseSince(unittest.TestCase):
    def test_z_suffix_offset_and_naive(self):
        want = ur.parse_since("2026-11-07T23:00:00+00:00")
        self.assertEqual(ur.parse_since("2026-11-07T23:00:00Z"), want)
        self.assertEqual(ur.parse_since("2026-11-07T23:00:00"), want)         # naive means UTC
        self.assertEqual(ur.parse_since("2026-11-08T00:00:00+01:00"), want)


class SessionChainInTheDb(unittest.TestCase):
    """Without KEEPALIVE_STATE_DIR (the CLI in a host shell) session_ids reads the links from the
    DB (keepalive.STATE_FILE's default is the live state), not from a side file in the repo dir."""
    A, B = ("dddddddd-0000-4000-8000-00000000000%d" % i for i in (1, 2))

    def test_follows_a_db_link(self):
        self.assertNotIn("KEEPALIVE_STATE_DIR", os.environ)
        self.assertTrue(ur.ka.runner_state.is_live("keepalive", ur.ka.STATE_FILE))

        def unlink():
            st = ur.ka.load_state()
            (st.get("cleared") or {}).pop(self.A, None)
            ur.ka.save_state(st)
        self.addCleanup(unlink)
        ur.ka.store_clear_link(self.A, self.B, "2026-11-01T00:00:00+00:00")
        self.assertEqual(ur.session_ids(self.B), [self.A, self.B])
        self.assertEqual(ur.session_ids(self.A), [self.A, self.B])


if __name__ == "__main__":
    unittest.main()
