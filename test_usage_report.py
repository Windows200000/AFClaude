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


if __name__ == "__main__":
    unittest.main()
