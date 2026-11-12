#!/usr/bin/env python3
"""Tests for the reader switch (phase 5b K4, txsource.py, docs/phase5b_plan.md §3.4).

The stub kit is the real kit (docker/host-kit/host_exec.py, run as `--run <command>` against a temp home and a
temp host.json) behind host.run_on_host, so no ssh runs and the answers have the real shapes. Both switch
positions (AFCLAUDE_READERS = mount | bridge) read the same fixture transcripts; a failure of the bridge raises
and never reads the mount instead (D-234/D-235).

    python3 -m unittest test_txsource
"""
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic env; before the AFClaude imports)
import host
import inbox
import keepalive as ka
import project_docs as pd
import stalled
import store
import txsource
from test_host_kit import good_config, run_exec, write_json
from test_project_docs import Env as DocsEnv

SID = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
PROJ = "-w-p"


def entry(kind, text, ts, **kw):
    msg = {"role": kind, "content": text}
    return dict({"type": kind, "timestamp": ts, "uuid": f"{kind[0]}-{ts}", "message": msg}, **kw)


def notice(ts):
    return {"type": "assistant", "timestamp": ts, "uuid": "n-" + ts, "error": "rate_limit",
            "message": {"model": "<synthetic>", "role": "assistant",
                        "content": [{"type": "text", "text": "You've hit your session limit · resets 5pm (UTC)"}]}}


class StubKit(unittest.TestCase):
    """A fake host (home, claude dir, host.json) behind host.run_on_host; the readers in bridge mode."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="txsource-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.projects = os.path.join(self.home, ".claude", "projects")
        self.sessions = os.path.join(self.home, ".claude", "sessions")
        self.root = os.path.join(self.tmp, "root")
        self.data = os.path.join(self.tmp, "hostdata")
        self.mirror = os.path.join(self.tmp, "data", "tx_mirror")
        for d in (os.path.join(self.projects, PROJ), self.sessions, self.root, self.data):
            os.makedirs(d)
        self.hj = os.path.join(self.tmp, "host.json")
        write_json(self.hj, good_config(self.root, self.data))
        self.calls = []
        self.broken = None                      # a callable(argv) -> CompletedProcess, or an exception to raise
        patches = [mock.patch.object(host, "run_on_host", self.run_on_host),
                   mock.patch.dict(os.environ, {"AFCLAUDE_READERS": "bridge", "AFCLAUDE_TX_MIRROR": self.mirror}),
                   mock.patch.object(ka, "PROJECTS_DIR", self.projects),
                   mock.patch.object(ka, "SESSIONS_DIR", os.path.join(self.tmp, "no-such-sessions")),
                   mock.patch.object(txsource, "CHUNK", txsource.CHUNK)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        txsource.drop_listing()
        self.addCleanup(txsource.drop_listing)

    def run_on_host(self, argv, input=None, cwd=None, env=None, timeout=None):
        argv = [str(a) for a in argv]
        remote = host.bridge_argv(argv)                  # a shape the real bridge accepts
        self.calls.append(remote)
        if isinstance(self.broken, Exception):
            raise self.broken
        if self.broken:
            return self.broken(remote)
        r = run_exec(shlex.join(remote), self.hj, home=self.home)
        return subprocess.CompletedProcess(argv, r.returncode, r.stdout.decode(), r.stderr.decode())

    def tx(self, name, entries, mtime=None, sub=None):
        path = os.path.join(self.projects, PROJ, *( [sub] if sub else []), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in entries))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def reads(self):
        return [c for c in self.calls if c[0] == "transcript-read"]

    @contextlib.contextmanager
    def mount_mode(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_READERS": "mount"}):
            yield


class Switch(StubKit):
    def test_default_is_mount_and_bad_values_raise(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AFCLAUDE_READERS")
            self.assertEqual(txsource.mode(), "mount")
            os.environ["AFCLAUDE_READERS"] = ""
            self.assertFalse(txsource.bridge_on())
            os.environ["AFCLAUDE_READERS"] = " bridge "
            self.assertEqual(txsource.mode(), "bridge")
            for bad in ("Bridge", "ssh", "1"):
                os.environ["AFCLAUDE_READERS"] = bad
                with self.assertRaises(txsource.TxError):
                    txsource.mode()
        self.assertTrue(txsource.bridge_on())

    def test_mount_mode_never_calls_the_bridge(self):
        path = self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z")])
        with self.mount_mode():
            self.assertEqual(ka.transcript_path(SID), path)
            self.assertEqual(len(list(ka.iter_entries(path))), 1)
            self.assertEqual(ka.file_mtime(path), os.path.getmtime(path))
            self.assertEqual(ka.sessions_dir(), ka.SESSIONS_DIR)
            self.assertEqual(ka.subagent_files(path), [])
        self.assertEqual(self.calls, [])

    def test_both_positions_read_the_same_entries(self):
        self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z"), notice("2026-11-01T10:05:00Z"),
                                 {"type": "system", "timestamp": "2026-11-01T10:06:00Z"}], mtime=5000)
        self.tx("agent-a.jsonl", [entry("user", "sub", "2026-11-01T10:01:00Z")], mtime=6000, sub=f"{SID}/subagents")
        results = {}
        for m in ("mount", "bridge"):
            with mock.patch.dict(os.environ, {"AFCLAUDE_READERS": m}):
                p = ka.transcript_path(SID)
                last = ka.last_message(p)
                results[m] = (list(ka.iter_entries(p)), last, ka.stall_info(last)["kind"], ka.file_mtime(p),
                              [os.path.basename(f) + str(ka.file_mtime(f)) for f in ka.subagent_files(p)],
                              ka.chain_subagent_files(p, [SID]) and len(ka.chain_subagent_files(p, [SID])),
                              ka.file_size(p))
        self.assertEqual(results["mount"], results["bridge"])
        self.assertEqual(results["bridge"][2], "session")
        self.assertIsNone(ka.transcript_path("33333333-3333-4333-8333-333333333333"))


class Mirror(StubKit):
    def test_path_is_the_private_mirror_with_the_hosts_mtime(self):
        host_path = self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z")], mtime=5000)
        p = ka.transcript_path(SID)
        self.assertEqual(p, os.path.join(self.mirror, "projects", PROJ, f"{SID}.jsonl"))
        self.assertNotEqual(p, host_path)
        with open(p, "rb") as a, open(host_path, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(os.path.getmtime(p), 5000.0)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(p)).st_mode & 0o777, 0o700)

    def test_only_the_missing_bytes_are_fetched(self):
        host_path = self.tx(f"{SID}.jsonl", [entry("user", "one", "2026-11-01T10:00:00Z")])
        p = ka.transcript_path(SID)
        first = os.path.getsize(p)
        self.tx(f"{SID}.jsonl", [entry("assistant", "two", "2026-11-01T10:01:00Z")], mtime=7000)
        before = len(self.reads())
        self.assertEqual(ka.file_size(p), os.path.getsize(host_path))        # refreshes the copy
        offsets = [c[3] for c in self.reads()[before:]]
        self.assertEqual(offsets[0], str(first))
        self.assertEqual(ka.file_mtime(p), 7000.0)
        self.assertEqual([e["message"]["content"] for e in ka.iter_entries(p)], ["one", "two"])

    def test_a_file_in_several_chunks_and_a_shrunk_file(self):
        txsource.CHUNK = 64
        host_path = self.tx(f"{SID}.jsonl", [entry("user", "x" * 40, f"2026-11-01T10:00:0{i}Z") for i in range(5)])
        p = ka.transcript_path(SID)
        self.assertGreater(len(self.reads()), 5)
        with open(p, "rb") as a, open(host_path, "rb") as b:
            self.assertEqual(a.read(), b.read())
        with open(host_path, "w") as fh:                                       # rewritten shorter on the host
            fh.write(json.dumps(entry("user", "new", "2026-11-02T10:00:00Z")) + "\n")
        p = ka.transcript_path(SID)
        self.assertEqual([e["message"]["content"] for e in ka.iter_entries(p)], ["new"])

    def test_a_listing_is_reused_briefly_and_a_new_file_needs_a_fresh_one(self):
        self.assertIsNone(ka.transcript_path(SID))
        self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z")])
        self.assertIsNone(ka.transcript_path(SID))                             # inside LIST_TTL
        txsource.drop_listing()
        self.assertIsNotNone(ka.transcript_path(SID))

    def test_subagent_stubs_carry_the_mtime_and_a_content_read_fills_them(self):
        main = self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z")])
        self.tx("agent-a.jsonl", [entry("user", "sub", "2026-11-01T10:01:00Z")], mtime=6000, sub=f"{SID}/subagents")
        p = ka.transcript_path(SID)
        (stub,) = ka.subagent_files(p)
        self.assertEqual((os.path.getsize(stub), os.path.getmtime(stub)), (0, 6000.0))
        before = len(self.reads())
        self.assertEqual(ka.file_mtime(stub), 6000.0)                          # no content fetched for an mtime
        self.assertEqual(len(self.reads()), before)
        self.assertEqual([e["message"]["content"] for e in ka.iter_entries(stub)], ["sub"])
        self.assertEqual(ka.chain_subagent_files(p, [SID]), [stub])
        self.assertTrue(main)

    def test_run_metrics_activity_reads_the_mirror(self):
        import run_metrics
        self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z"), notice("2026-11-01T10:05:00Z")])
        self.tx("agent-a.jsonl", [entry("assistant", "sub", "2026-11-01T10:01:00Z")], sub=f"{SID}/subagents")
        lo, hi = ka.parse_ts("2026-11-01T00:00:00Z"), ka.parse_ts("2026-11-02T00:00:00Z")
        got = {}
        for m in ("mount", "bridge"):
            with mock.patch.dict(os.environ, {"AFCLAUDE_READERS": m}):
                got[m] = run_metrics.session_activity(SID, lo, hi)
        self.assertEqual(got["mount"], got["bridge"])
        self.assertEqual(len(got["bridge"][0]), 2)

    def test_inbox_seen_after_an_offset(self):
        self.tx(f"{SID}.jsonl", [entry("user", "one", "2026-11-01T10:00:00Z")])
        p = ka.transcript_path(SID)
        off = ka.file_size(p)
        self.assertFalse(inbox.seen(p, off, "MARK-1"))
        self.tx(f"{SID}.jsonl", [entry("user", "MARK-1 text", "2026-11-01T10:01:00Z")])
        self.assertTrue(inbox.seen(p, off, "MARK-1"))                          # refreshed from the host


class Sessions(StubKit):
    def reg(self, pid, sid, **kw):
        write_json(os.path.join(self.sessions, f"{pid}.json"), dict({"pid": pid, "sessionId": sid, "procStart": "1"}, **kw))

    def test_the_registry_is_mirrored_and_reread(self):
        self.reg(4242, SID, name="x")
        with open(os.path.join(self.sessions, "7.json"), "w") as fh:
            fh.write("{ half written")
        d = ka.sessions_dir()
        self.assertEqual(sorted(os.listdir(d)), ["4242.json"])                 # the unreadable one is skipped
        self.assertEqual(ka.session_entry(4242)["sessionId"], SID)
        self.assertEqual(ka.session_entry(1), {})
        self.reg(4243, SID2)
        os.unlink(os.path.join(self.sessions, "4242.json"))
        with mock.patch.object(txsource, "SESSIONS_TTL", 0):
            self.assertEqual(sorted(os.listdir(ka.sessions_dir())), ["4243.json"])
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)

    def test_the_mirror_is_reused_inside_the_ttl(self):
        self.reg(1, SID)
        ka.sessions_dir()
        ka.sessions_dir()
        self.assertEqual(len([c for c in self.calls if c[0] == "sessions"]), 1)


class Scans(StubKit):
    """stalled.scan reads the bytes through range reads: same DB result in both positions."""

    def scan(self, mode):
        db = os.path.join(self.tmp, f"scan-{mode}.db")
        store.connect(db, create=True).close()
        with mock.patch.dict(os.environ, {"AFCLAUDE_READERS": mode}):
            conn = store.connect(db)
            try:
                st = stalled.scan(conn, None, own_list=os.path.join(self.tmp, "own.txt"))
                rows = [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY session_id")]
                hits = conn.execute("SELECT COUNT(*) FROM limit_hits").fetchone()[0]
            finally:
                conn.close()
        keep = ("session_id", "path", "title", "cwd", "stalled", "stall_kind", "last_scanned_size", "last_scanned_offset",
                "first_seen", "last_msg_uuid")
        return {k: v for k, v in st.items() if k in ("files", "changed", "bytes", "new_hits", "new_sessions")}, \
            [{k: r[k] for k in keep} for r in rows], hits

    def test_stalled_scan_same_result_in_both_positions(self):
        self.tx(f"{SID}.jsonl", [dict(entry("user", "hi", "2026-11-01T10:00:00Z"), cwd="/x/y"),
                                 {"type": "custom-title", "customTitle": "A title"}, notice("2026-11-01T10:05:00Z")])
        self.tx(f"{SID2}.jsonl", [entry("user", "other", "2026-11-01T11:00:00Z")])
        self.tx("agent-a.jsonl", [notice("2026-11-01T10:07:00Z")], sub=f"{SID}/subagents")   # not a top-level session
        mount, bridge = self.scan("mount"), self.scan("bridge")
        self.assertEqual(mount, bridge)
        self.assertEqual((bridge[0]["files"], bridge[0]["new_sessions"], bridge[0]["new_hits"], bridge[2]), (2, 2, 1, 1))
        self.assertTrue(bridge[1][0]["stalled"])
        self.assertTrue(all(r["path"].startswith(self.projects) for r in bridge[1]))   # the host path form, not the mirror
        self.assertFalse(os.path.exists(self.mirror) and os.path.exists(os.path.join(self.mirror, "projects")))

    def test_an_incremental_scan_reads_only_the_new_bytes(self):
        self.tx(f"{SID}.jsonl", [entry("user", "one", "2026-11-01T10:00:00Z")])
        db = os.path.join(self.tmp, "inc.db")
        store.connect(db, create=True).close()
        conn = store.connect(db)
        own = os.path.join(self.tmp, "own.txt")
        stalled.scan(conn, None, own_list=own)
        first = os.path.getsize(os.path.join(self.projects, PROJ, f"{SID}.jsonl"))
        self.tx(f"{SID}.jsonl", [notice("2026-11-01T10:05:00Z")])
        st = stalled.scan(conn, None, own_list=own)
        conn.close()
        self.assertEqual((st["changed"], st["new_hits"]), (1, 1))
        self.assertEqual(st["bytes"], os.path.getsize(os.path.join(self.projects, PROJ, f"{SID}.jsonl")) - first)
        self.assertEqual(self.reads()[-1][3], str(first))

    def test_an_explicit_projects_dir_stays_local(self):
        self.tx(f"{SID}.jsonl", [entry("user", "one", "2026-11-01T10:00:00Z")])
        db = os.path.join(self.tmp, "loc.db")
        store.connect(db, create=True).close()
        conn = store.connect(db)
        st = stalled.scan(conn, self.projects, own_list=os.path.join(self.tmp, "own.txt"))
        conn.close()
        self.assertEqual(st["files"], 1)
        self.assertEqual(self.calls, [])

    def test_usage_sampler_scan_listing(self):
        self.tx(f"{SID}.jsonl", [entry("user", "one", "2026-11-01T10:00:00Z")])
        self.tx("agent-a.jsonl", [entry("user", "sub", "2026-11-01T10:01:00Z")], sub=f"{SID}/subagents")
        both = txsource.scan_listing(self.projects, False)
        self.assertEqual(sorted(os.path.relpath(p, self.projects) for p in both),
                         [f"{PROJ}/{SID}.jsonl", f"{PROJ}/{SID}/subagents/agent-a.jsonl"])
        top = txsource.scan_listing(self.projects, True)
        main = os.path.join(self.projects, PROJ, f"{SID}.jsonl")
        self.assertEqual(list(top), [main])
        data = txsource.read_scan_chunk(self.projects, main, 5, top[main])
        with open(main, "rb") as fh:
            self.assertEqual(data, fh.read()[5:])
        self.assertEqual(txsource.read_scan_chunk(self.projects, main, top[main], top[main]), b"")


class Failures(StubKit):
    """D-234/D-235: a bridge that fails raises; the mount is never read instead."""

    def setUp(self):
        super().setUp()
        self.path = self.tx(f"{SID}.jsonl", [entry("user", "hi", "2026-11-01T10:00:00Z")])   # the mount HAS the file

    def bad(self, rc=1, out="", err="boom"):
        return lambda remote: subprocess.CompletedProcess(remote, rc, out, err)

    def test_a_failing_bridge_raises_and_does_not_fall_back_to_the_mount(self):
        for fail in (self.bad(255, "", "ssh: connect to host failed"), self.bad(126, "", "not whitelisted"),
                     self.bad(0, "not json", ""), self.bad(0, "[]", "")):
            self.broken = fail
            txsource.drop_listing()
            with self.assertRaises(txsource.TxError):
                ka.transcript_path(SID)
            with self.assertRaises(txsource.TxError):
                ka.sessions_dir()
            with self.assertRaises(txsource.TxError):
                stalled.scan(None, None)

    def test_a_timeout_raises(self):
        self.broken = subprocess.TimeoutExpired("ssh", 60)
        with self.assertRaises(subprocess.TimeoutExpired):
            ka.transcript_path(SID)

    def test_a_cut_off_or_unreadable_listing_raises(self):
        for body, why in (({"files": [], "truncated": True, "errors": []}, "cut off"),
                          ({"files": [], "truncated": False, "errors": [{"path": "x", "error": "denied"}]}, "could not read")):
            self.broken = self.bad(0, json.dumps(body))
            txsource.drop_listing()
            with self.assertRaisesRegex(txsource.TxError, why):
                txsource.listing()

    def test_a_transcript_gone_on_the_host_is_file_not_found(self):
        ka.transcript_path(SID)
        os.unlink(self.path)
        with self.assertRaises(FileNotFoundError):
            txsource.mirror(f"{PROJ}/{SID}.jsonl")

    def test_a_wrong_answer_to_a_read_raises(self):
        txsource.drop_listing()
        self.broken = lambda remote: subprocess.CompletedProcess(remote, 0, json.dumps(
            {"files": [{"path": f"{PROJ}/{SID}.jsonl", "size": 5, "mtime": 1.0}], "truncated": False, "errors": []}) if remote[0] == "transcripts"
            else json.dumps({"path": "other.jsonl", "size": 5, "mtime": 1.0, "offset": 0, "data": "aGk="}), "")
        with self.assertRaisesRegex(txsource.TxError, "unusable shape"):
            ka.transcript_path(SID)

    def test_mirror_paths_cannot_leave_the_mirror(self):
        for bad in ("../x.jsonl", "/etc/x.jsonl", "a//b.jsonl", ""):
            with self.assertRaises(txsource.TxError):
                txsource.mirror_path(bad)


class Docs(DocsEnv):
    """The importer reads the build-log files through `doc-files read` in bridge mode."""

    def setUp(self):
        super().setUp()
        self.kit = StubKit("setUp")
        self.kit.setUp()
        self.addCleanup(self.kit.doCleanups)
        write_json(self.kit.hj, good_config(self.tmp, self.kit.data))        # the project dir is under the root
        os.environ.update(AFCLAUDE_READERS="bridge")
        patcher = mock.patch.object(host, "run_on_host", self.kit.run_on_host)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_import_through_the_kit_matches_the_file_import(self):
        self.write("PROGRESS.md", "# Progress\n\n- first\n")
        self.write("GOALS.md", "# Goals\n\n- [ ] one\n")
        r = pd.import_all(self.db, self.repo, self.reviews)
        self.assertEqual([c for c in self.kit.calls if c[0] == "doc-files"],
                         [["doc-files", "read", self.repo, "PROGRESS.md", "GOALS.md", "EXCEPTIONS.md"]])
        self.assertEqual((r["docs"]["progress"]["outcome"], r["docs"]["goals"]["outcome"], r["docs"]["exceptions"]["outcome"],
                          r["docs"]["alerts"]["outcome"]), ("imported", "imported", "missing", "missing"))
        self.assertEqual(self.render("progress"), "# Progress\n\n- first\n")
        self.assertTrue(r["docs"]["progress"]["verified"])
        with mock.patch.dict(os.environ, {"AFCLAUDE_READERS": "mount"}):
            r2 = pd.import_all(self.db, self.repo, self.reviews)
        self.assertEqual((r2["docs"]["progress"]["outcome"], r2["docs"]["goals"]["outcome"]), ("in_sync", "in_sync"))

    def test_a_review_report_is_still_read_from_the_data_dir(self):
        self.write("PROGRESS.md", "# Progress\n", )
        self.write("review1.md", "# review\n", where=self.reviews)
        r = pd.import_all(self.db, self.repo, self.reviews)
        self.assertEqual(r["docs"]["review:review1"]["outcome"], "imported")

    def test_a_bridge_failure_raises_and_the_mount_is_not_read(self):
        self.write("PROGRESS.md", "# Progress\n")
        self.kit.broken = lambda remote: subprocess.CompletedProcess(remote, 255, "", "ssh failed")
        with self.assertRaises(txsource.TxError):
            pd.import_all(self.db, self.repo, self.reviews)
        self.assertIsNone(self.render("progress"))

    def test_the_export_checks_through_the_kit_and_refuses_to_write(self):
        self.write("PROGRESS.md", "# Progress\n\n- first\n")
        pd.import_all(self.db, self.repo, self.reviews)
        self.act("doc.append", project="P", name="progress", text="- second\n", actor="runner:x", via="runner")
        r = pd.export(self.db, self.repo, check=True)
        self.assertEqual(r["files"]["PROGRESS.md"]["result"], "would write")
        with self.assertRaisesRegex(txsource.TxError, "host wrapper"):
            pd.export(self.db, self.repo)
        with open(os.path.join(self.repo, "PROGRESS.md")) as fh:
            self.assertEqual(fh.read(), "# Progress\n\n- first\n")


class Shapes(unittest.TestCase):
    def test_bridge_argv_accepts_transcript_read_and_nothing_wider(self):
        ok = ["transcript-read", "default", "-w-p/abc.jsonl", "0", str(1024 * 1024)]
        self.assertEqual(host.bridge_argv(ok), ok)
        self.assertEqual(host.bridge_argv(["transcript-read", "default", "-w-p/s/subagents/agent-1.jsonl", "10", "5"])[0],
                         "transcript-read")
        for bad in (ok[:-1], ok + ["x"], ["transcript-read", "Default", "a/b.jsonl", "0", "1"],
                    ["transcript-read", "default", "../a.jsonl", "0", "1"], ["transcript-read", "default", "/a.jsonl", "0", "1"],
                    ["transcript-read", "default", "a/b.txt", "0", "1"], ["transcript-read", "default", "a/b.jsonl", "-1", "1"],
                    ["transcript-read", "default", "a/./b.jsonl", "0", "1"]):
            with self.assertRaises(ValueError):
                host.bridge_argv(bad)


if __name__ == "__main__":
    unittest.main()
