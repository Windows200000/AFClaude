#!/usr/bin/env python3
"""Tests for the last direct transcript readers on the reader switch (usage_report.py, compact_timing.py fit):
the same results in mount and bridge mode on the same fixture files (via the real kit, test_txsource.StubKit),
and a bridge failure raises instead of returning fewer files (D-234/D-235).

    python3 -m unittest test_readers_bridge
"""
import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import testenv  # noqa: F401  (hermetic env; before the AFClaude imports)
import compact_timing as ct
import txsource
import usage_report as ur
from test_txsource import StubKit

OLD = "aaaaaaaa-0000-4000-8000-000000000001"
NEW = "aaaaaaaa-0000-4000-8000-000000000002"
OTHER = "bbbbbbbb-0000-4000-8000-000000000001"
UTC = timezone.utc


def turn(ts, mid, out, model="m"):
    return {"type": "assistant", "timestamp": ts, "uuid": "u" + mid,
            "message": {"id": mid, "model": model, "usage": {"input_tokens": 1, "output_tokens": out}}}


class Fixture(StubKit):
    """A /clear chain OLD -> NEW over two project dirs, subagents (one with a .meta.json), another session."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"KEEPALIVE_PROJECTS_DIR": self.projects})     # the readers' default dir
        p.start()
        self.addCleanup(p.stop)
        now = datetime.now(UTC)
        self.t = lambda mins: (now - timedelta(minutes=mins)).isoformat()
        self.write(OLD, "-w-p", [turn(self.t(50), "m1", 5)])
        self.write(NEW, "-w-q", [turn(self.t(40), "m2", 7), turn(self.t(30), "m3", 11)])   # cwd changed
        self.write(OTHER, "-w-p", [turn(self.t(20), "m4", 100)])
        self.sub(OLD, "-w-p", "aold", [turn(self.t(45), "s1", 3)], meta={"description": "old job", "agentType": "w"})
        self.sub(NEW, "-w-q", "anew", [turn(self.t(25), "s2", 4)])
        self.sub(NEW, "-w-p", "anew", [turn(self.t(24), "s3", 6)])      # the same agent under another project dir
        self.sub(OTHER, "-w-p", "aoth", [turn(self.t(10), "s4", 50)])
        self.state = {"cleared": {OLD: {"to": NEW, "at": "t"}}}
        p = mock.patch.object(ur.ka, "load_state", lambda: self.state)
        p.start()
        self.addCleanup(p.stop)

    def write(self, sid, proj, entries):
        path = os.path.join(self.projects, proj, sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in entries))

    def sub(self, sid, proj, agent, entries, meta=None):
        d = os.path.join(self.projects, proj, sid, "subagents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"agent-{agent}.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in entries))
        if meta:
            with open(os.path.join(d, f"agent-{agent}.meta.json"), "w") as fh:
                json.dump(meta, fh)

    def rel(self, path):
        """A reader's path as the fixture's relative one, whichever tree (host or mirror) it is in."""
        for base in (self.projects, txsource.projects_mirror()):
            if path.startswith(base + os.sep):
                return path[len(base) + 1:]
        raise AssertionError(path)


class UsageReport(Fixture):
    def both(self, fn):
        with self.mount_mode():
            a = fn()
        return a, fn()

    def test_main_transcripts_chain_same_files_same_order(self):
        a, b = self.both(lambda: [self.rel(p) for p in ur.main_transcripts(NEW)])
        self.assertEqual(a, [f"-w-p/{OLD}.jsonl", f"-w-q/{NEW}.jsonl"])
        self.assertEqual(a, b)

    def test_subagent_files_chain_and_two_project_dirs(self):
        a, b = self.both(lambda: {k: sorted(self.rel(p) for p in v) for k, v in ur.subagent_files(NEW).items()})
        self.assertEqual(sorted(a), ["anew", "aold"])
        self.assertEqual(len(a["anew"]), 2)
        self.assertEqual(a, b)

    def test_rows_equal_in_both_modes_but_meta(self):
        def rows():
            rs, main = ur.build_rows(NEW, None)
            return [{k: v for k, v in r.items() if k not in ("description", "agent_type")} for r in rs], main
        with self.mount_mode():
            a = rows()
        with mock.patch.object(ur, "load_meta", lambda s, a: {}):          # meta is its own test
            b = rows()
        # status is "running"/"done" by age: same fixture, same answer
        self.assertEqual(a, b)
        self.assertEqual(a[1]["tokens"]["out"], 5 + 7 + 11)

    def test_meta_is_read_in_mount_mode(self):
        with self.mount_mode():
            self.assertEqual(ur.load_meta(NEW, "aold")["description"], "old job")
            self.assertEqual(ur.load_meta(NEW, "anew"), {})

    def test_meta_in_bridge_mode_raises_it_is_not_served(self):
        with self.assertRaisesRegex(txsource.TxError, "meta.json"):
            ur.load_meta(NEW, "aold")

    def test_a_failing_bridge_raises_never_fewer_files(self):
        self.broken = lambda remote: subprocess.CompletedProcess(remote, 255, "", "ssh: gone")
        txsource.drop_listing()
        for fn in (ur.main_transcripts, ur.subagent_files):
            with self.assertRaises(txsource.TxError):
                fn(NEW)

    def test_a_cut_off_listing_raises(self):
        def cut(remote):
            if remote[0] == "transcripts":
                return subprocess.CompletedProcess(remote, 0, json.dumps({"files": [], "truncated": True, "errors": []}), "")
            raise AssertionError(remote)
        self.broken = cut
        with self.assertRaisesRegex(txsource.TxError, "cut off"):
            ur.main_transcripts(NEW)

    def test_stale_mirror_is_brought_up_to_date(self):
        ur.main_transcripts(NEW)
        with open(os.path.join(self.projects, "-w-q", f"{NEW}.jsonl"), "a") as fh:
            fh.write(json.dumps(turn(self.t(1), "m9", 1000)) + "\n")
        txsource.drop_listing()
        self.assertEqual(ur.scan(ur.main_transcripts(NEW))["tokens"]["out"], 5 + 7 + 11 + 1000)


class CompactTiming(Fixture):
    def reqs(self):
        return [(r.ts, r.output) for r in ct.all_requests(self.projects, datetime.now(UTC) - timedelta(days=1))]

    def test_all_requests_equal_in_both_modes(self):
        with self.mount_mode():
            a = self.reqs()
        b = self.reqs()
        self.assertEqual(len(a), 8)
        self.assertEqual(a, b)

    def test_files_older_than_the_window_are_skipped_by_the_host_mtime(self):
        old = os.path.join(self.projects, "-w-p", f"{OTHER}.jsonl")
        os.utime(old, (1.0, 1.0))
        with self.mount_mode():
            a = self.reqs()
        b = self.reqs()
        self.assertEqual(a, b)
        self.assertNotIn(100, [o for _t, o in b])

    def test_main_session_files(self):
        with self.mount_mode():
            a = sorted(self.rel(p) for p, _m in ct._transcripts(self.projects, NEW))
        b = sorted(self.rel(p) for p, _m in ct._transcripts(self.projects, NEW))
        self.assertEqual(a, b)
        self.assertEqual(b, [f"-w-q/{NEW}.jsonl"])

    def test_run_fit_reads_the_main_session_through_the_bridge(self):
        seen = []
        with mock.patch.object(ct, "fit", lambda samples, reqs, prev, now, scans: seen.append((len(reqs), len(scans))) or {}), \
                mock.patch.object(ct, "read_samples", lambda since: []), mock.patch.object(ct, "load", lambda: {}):
            ct.run_fit(1, self.projects, NEW, datetime.now(UTC))
        self.assertEqual(seen, [(8, 1)])

    def test_another_projects_dir_raises_in_bridge_mode(self):
        with self.assertRaises(txsource.TxError):
            ct.all_requests(os.path.join(self.tmp, "elsewhere"), datetime.now(UTC) - timedelta(days=1))

    def test_a_failing_bridge_raises_never_fewer_files(self):
        self.broken = lambda remote: subprocess.CompletedProcess(remote, 255, "", "ssh: gone")
        txsource.drop_listing()
        with self.assertRaises(txsource.TxError):
            ct.all_requests(self.projects, datetime.now(UTC) - timedelta(days=1))

    def test_a_file_deleted_after_the_listing_is_skipped_like_in_mount_mode(self):
        txsource.listing()
        os.unlink(os.path.join(self.projects, "-w-p", f"{OTHER}.jsonl"))
        shutil.rmtree(os.path.join(self.projects, "-w-p", OTHER))
        self.assertNotIn(100, [o for _t, o in self.reqs()])

    def test_the_cli_reports_a_bridge_failure(self):
        self.broken = lambda remote: subprocess.CompletedProcess(remote, 255, "", "ssh: gone")
        txsource.drop_listing()
        with mock.patch.object(ct, "registry_session", lambda s: NEW), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(ct.main(["fit", "--projects-dir", self.projects]), 1)
        self.assertIn("error", "".join(c.args[0] for c in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
