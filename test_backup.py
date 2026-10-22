#!/usr/bin/env python3
"""Offline tests for the DB backup bundle and its restore (backup.py, phase 5c step B1 of
docs/phase5c_plan.md; temp DBs only): the round trip, the restored install's identity and pause
(D-173), create never writing its source, and every refusal (no install_id, a database at the
target, a damaged or tampered bundle, a newer schema), with nothing partial left behind."""
import testenv  # noqa: F401  (first: a temp DB, no live settings)

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

import actions
import backup
import store


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afclaude-backup-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src", "afclaude.db")
        os.makedirs(os.path.dirname(self.src))
        self.out = os.path.join(self.tmp, "out")
        conn = store.connect(self.src, create=True)
        try:
            for i in range(30):
                actions.perform(conn, "task.add", {"title": f"t{i}", "project": "P"}, actor="cli", via="cli")
            actions.perform(conn, "setting.set", {"key": "stall_verify_minutes", "value": 20}, actor="cli", via="cli")
            conn.commit()
        finally:
            conn.close()

    def do(self, db, name, **params):
        conn = store.connect(db)
        try:
            return actions.perform(conn, name, params, actor="cli", via="cli")
        finally:
            conn.close()

    def counts(self, db):
        conn = sqlite3.connect(db)
        try:
            return backup.row_counts(conn)
        finally:
            conn.close()

    def meta(self, db, key):
        conn = sqlite3.connect(db)
        try:
            return store.get_meta(conn, key)
        finally:
            conn.close()

    def leftovers(self, d):
        return [n for n in os.listdir(d) if n.endswith(".partial") or n.startswith(".")] if os.path.isdir(d) else []

    def rebuild(self, bundle, mutate_manifest=None, db_bytes=None, extra=None, drop_db=False, name_db=None):
        """A copy of `bundle` with a changed manifest / database / member list. -> its path."""
        with tarfile.open(bundle, "r:gz") as tar:
            m = json.loads(tar.extractfile(backup.MANIFEST).read())
            db = tar.extractfile(backup.DB_MEMBER).read()
        if mutate_manifest:
            mutate_manifest(m)
        if db_bytes is not None:
            db = db_bytes(db)
        path = os.path.join(self.tmp, f"forged-{len(os.listdir(self.tmp))}.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            items = [(backup.MANIFEST, json.dumps(m).encode())]
            if not drop_db:
                items.append((name_db or backup.DB_MEMBER, db))
            items += extra or []
            for name, data in items:
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        return path


class RoundTrip(Base):
    def test_create_verify_restore(self):
        before = sha(self.src)
        res = backup.create(self.out, self.src)
        self.assertEqual(sha(self.src), before)                     # create never writes its source
        path, m = res["path"], res["manifest"]
        self.assertTrue(os.path.basename(path).startswith(f"afclaude-backup-{m['install_id'][:8]}-"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(self.leftovers(self.out), [])
        self.assertEqual(m["row_counts"], self.counts(self.src))
        self.assertEqual(m["row_counts"]["tasks"], 30)
        self.assertEqual((m["install_id"], m["install_epoch"], m["schema_version"], m["kind"]),
                         (self.meta(self.src, "install_id"), 0, store.SCHEMA_VERSION, "backup"))
        self.assertEqual(backup.verify(path), m)
        with tarfile.open(path, "r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), sorted(backup.MEMBERS))
            self.assertTrue(all(ti.uname == "" and ti.uid == 0 for ti in tar.getmembers()))

        into = os.path.join(self.tmp, "new-data")
        r = backup.restore(path, into)
        db = os.path.join(into, "afclaude.db")
        self.assertEqual(r["db"], db)
        self.assertEqual(sorted(os.listdir(into)), ["afclaude.db"])     # no WAL, no work dir left
        self.assertEqual((r["install_epoch"], r["paused"]), (1, True))
        self.assertIn("restored from a backup of " + m["created_at"], r["reason"])
        conn = store.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 30)
            self.assertEqual(actions.get_setting(conn, "stall_verify_minutes"), 20)
            self.assertTrue(actions.get_setting(conn, "automation_paused"))
            self.assertEqual(store.get_meta(conn, "install_id"), m["install_id"])
            self.assertEqual(store.get_meta(conn, "install_epoch"), "1")
            src = json.loads(store.get_meta(conn, "restored_from"))
            self.assertEqual((src["created_at"], src["bundle_sha256"], src["install_epoch"]),
                             (m["created_at"], res["sha256"], 0))
            audit = store.audit_log(conn, limit=5)
            self.assertEqual(audit[0]["action"], "install.restored")
            self.assertEqual(audit[0]["after"]["install_epoch"], 1)
        finally:
            conn.close()
        restored = self.counts(db)                                   # the rows are the same except
        for t in ("settings", "audit_log", "meta"):                  # what the restore itself wrote
            restored.pop(t)
            m["row_counts"].pop(t)
        self.assertEqual(restored, m["row_counts"])

    def test_a_restored_install_bundles_again_with_its_new_epoch(self):
        into = os.path.join(self.tmp, "a")
        backup.restore(backup.create(self.out, self.src)["path"], into)
        m2 = backup.create(os.path.join(self.tmp, "out2"), os.path.join(into, "afclaude.db"))["manifest"]
        self.assertEqual(m2["install_epoch"], 1)
        r = backup.restore(backup.create(self.out, os.path.join(into, "afclaude.db"))["path"],
                           os.path.join(self.tmp, "b"))
        self.assertEqual(r["install_epoch"], 2)

    def test_restore_starts_paused_off(self):
        self.do(self.src, "setting.set", key="restore_starts_paused", value=False)
        r = backup.restore(backup.create(self.out, self.src)["path"], os.path.join(self.tmp, "n"))
        self.assertEqual((r["paused"], r["reason"]), (False, None))

    def test_a_pause_from_backup_time_is_kept_in_the_reason(self):
        self.do(self.src, "automation.set", paused=True, reason="sampler failed")
        r = backup.restore(backup.create(self.out, self.src)["path"], os.path.join(self.tmp, "n"))
        self.assertTrue(r["paused"])
        self.assertIn("paused at backup time: sampler failed", r["reason"])

    def test_an_older_schema_is_migrated_forward(self):
        path = backup.create(self.out, self.src)["path"]
        calls = []
        orig = store.migrate
        store.migrate = lambda p=None: calls.append(p) or orig(p)
        try:
            backup.restore(path, os.path.join(self.tmp, "n"))
        finally:
            store.migrate = orig
        self.assertEqual(len(calls), 1)

    def test_cli(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(backup.main(["create", "--out", self.out, "--db", self.src]), 0)
        path = json.loads(out.getvalue())["path"]
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(backup.main(["verify", path]), 0)
            self.assertEqual(backup.main(["restore", path, "--into", os.path.join(self.tmp, "n")]), 0)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(backup.main(["restore", path, "--into", os.path.join(self.tmp, "n")]), 1)
        self.assertIn("already holds a database", err.getvalue())


class Refusals(Base):
    def setUp(self):
        super().setUp()
        self.bundle = backup.create(self.out, self.src)["path"]

    def refused(self, bundle, msg):
        into = os.path.join(self.tmp, f"into-{len(os.listdir(self.tmp))}")
        with self.assertRaisesRegex(backup.BackupError, msg):
            backup.restore(bundle, into)
        self.assertEqual(os.listdir(into), [])                  # no database, no work dir
        with self.assertRaisesRegex(backup.BackupError, msg):
            backup.verify(bundle)

    def test_no_install_id(self):
        conn = sqlite3.connect(self.src)
        conn.execute("DELETE FROM meta WHERE key='install_id'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(backup.BackupError, "backup.py stamp"):
            backup.create(os.path.join(self.tmp, "o2"), self.src)
        self.assertEqual(self.leftovers(os.path.join(self.tmp, "o2")), [])
        r = backup.stamp(self.src)
        self.assertEqual(self.meta(self.src, "install_id"), r["install_id"])
        with self.assertRaisesRegex(ValueError, "already has install_id"):
            backup.stamp(self.src)
        self.assertTrue(backup.create(os.path.join(self.tmp, "o2"), self.src)["path"])

    def test_missing_source(self):
        with self.assertRaisesRegex(backup.BackupError, "no database"):
            backup.create(self.out, os.path.join(self.tmp, "nope.db"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.db")))

    def test_target_holds_a_database(self):
        into = os.path.join(self.tmp, "full")
        os.makedirs(into)
        for name in ("afclaude.db", "afclaude.db-wal"):
            p = os.path.join(into, name)
            open(p, "w").close()
            with self.assertRaisesRegex(backup.BackupError, "already holds a database"):
                backup.restore(self.bundle, into)
            self.assertEqual(os.path.getsize(p), 0)
            os.remove(p)

    def test_tampered_database(self):
        def flip(db):
            b = bytearray(db)
            b[len(b) // 2] ^= 0xFF
            return bytes(b)
        self.refused(self.rebuild(self.bundle, db_bytes=flip), "does not match its manifest")

    def test_consistently_forged_database_fails_the_row_counts(self):
        def drop_rows(db):
            d = tempfile.mkdtemp(dir=self.tmp)
            p = os.path.join(d, "x.db")
            with open(p, "wb") as fh:
                fh.write(db)
            c = sqlite3.connect(p)
            c.execute("DROP TRIGGER task_events_no_delete")
            c.execute("DELETE FROM task_events")
            c.commit()
            c.close()
            with open(p, "rb") as fh:
                return fh.read()
        forged = self.rebuild(self.bundle, db_bytes=drop_rows)
        with tarfile.open(forged, "r:gz") as tar:
            data = tar.extractfile(backup.DB_MEMBER).read()

        def fix(m):
            m["files"][backup.DB_MEMBER] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        self.refused(self.rebuild(forged, mutate_manifest=fix), "row counts differ")

    def test_member_list(self):
        self.refused(self.rebuild(self.bundle, extra=[("../evil", b"x")]), "exactly the files")
        self.refused(self.rebuild(self.bundle, drop_db=True), "exactly the files")
        self.refused(self.rebuild(self.bundle, drop_db=True, extra=[("db/../db/afclaude.db", b"x")]),
                     "exactly the files")

    def test_manifest(self):
        for mutate, msg in ((lambda m: m.update(format="zip"), "not an afclaude-backup"),
                            (lambda m: m.update(format_version=2), "format 1"),
                            (lambda m: m.pop("kind"), "exactly the keys"),
                            (lambda m: m.update(extra=1), "exactly the keys"),
                            (lambda m: m.update(kind="move"), "not supported"),
                            (lambda m: m.update(install_epoch=-1), "non-negative"),
                            (lambda m: m.update(master_key="k"), "can't restore yet"),
                            (lambda m: m.update(install_id="other"), "do not match the manifest")):
            with self.subTest(msg=msg):
                self.refused(self.rebuild(self.bundle, mutate_manifest=mutate), msg)

    def test_not_a_bundle(self):
        p = os.path.join(self.tmp, "junk.tar.gz")
        with open(p, "wb") as fh:
            fh.write(b"not a tar")
        self.refused(p, "can't be read")
        with open(self.bundle, "rb") as fh:
            data = fh.read()
        with open(p, "wb") as fh:
            fh.write(data[: len(data) // 2])
        self.refused(p, "can't be read")

    def test_newer_schema(self):
        conn = sqlite3.connect(self.src)
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(store.SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
        newer = backup.create(os.path.join(self.tmp, "o3"), self.src)["path"]    # reading is fine
        into = os.path.join(self.tmp, "n")
        with self.assertRaisesRegex(backup.BackupError, "newer than this code"):
            backup.restore(newer, into)
        self.assertEqual(os.listdir(into), [])

    def test_bundle_never_overwritten(self):
        orig = store.now_iso
        store.now_iso = lambda: "2026-10-22T03:12:00.000Z"
        try:
            backup.create(os.path.join(self.tmp, "o4"), self.src)
            with self.assertRaisesRegex(backup.BackupError, "already exists"):
                backup.create(os.path.join(self.tmp, "o4"), self.src)
        finally:
            store.now_iso = orig

    def test_a_failed_verify_leaves_no_partial(self):
        orig = backup.verify
        backup.verify = lambda b: (_ for _ in ()).throw(backup.BackupError("boom"))
        try:
            with self.assertRaisesRegex(backup.BackupError, "boom"):
                backup.create(os.path.join(self.tmp, "o5"), self.src)
        finally:
            backup.verify = orig
        self.assertEqual(os.listdir(os.path.join(self.tmp, "o5")), [])


class Actions(Base):
    def test_restored_checks_its_source(self):
        good = {"install_id": self.meta(self.src, "install_id"), "install_epoch": 0, "created_at": "t",
                "app_version": None, "schema_version": 4, "bundle_sha256": "x", "kind": "backup"}
        for bad, msg in ((dict(good, extra=1), "exactly"), (dict(good, install_epoch="1"), "non-negative"),
                         (dict(good, install_id="other"), "is not the bundle's"), ("x", "exactly")):
            with self.subTest(msg=msg), self.assertRaisesRegex(ValueError, msg):
                self.do(self.src, "install.restored", source=bad)
        self.assertIsNone(self.meta(self.src, "install_epoch"))

    def test_autonomous_mcp_sessions_cannot_restore_or_stamp(self):
        conn = store.connect(self.src)
        try:
            for name, params in (("install.stamp", {}), ("install.restored", {"source": {}})):
                with self.assertRaises(actions.Forbidden):
                    actions.perform(conn, name, params, actor="mcp:x", via="mcp", autonomous=True)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
