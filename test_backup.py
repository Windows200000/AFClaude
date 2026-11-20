#!/usr/bin/env python3
"""Offline tests for the DB backup bundle and its restore (backup.py, phase 5c step B1 of
docs/phase5c_plan.md; temp DBs only): the round trip, the restored install's identity and pause
(D-173), create never writing its source, and every refusal (no install_id, a database at the
target, a damaged or tampered bundle, a newer schema), with nothing partial left behind.
Step B3 (D-287): the scheduled, write-once backups: the protected floor under every backup_keep, the
two grants (record creates, prune deletes, each refusing the other and every other actor), the table's
own triggers, a failed run pausing automation and keeping every older bundle, a full disk."""
import testenv  # noqa: F401  (first: a temp DB, no live settings)

import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import actions
import backup
import failstop
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


class Scheduled(Base):
    """Phase 5c B3: the daily job's write-once bundles. The live database is a temp one (store.DB_PATH)."""
    OWNER = "owner"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(store, "DB_PATH", self.src)
        p.start()
        self.addCleanup(p.stop)
        self.notes = []
        n = mock.patch.object(failstop, "_notify", lambda subject, body: self.notes.append(subject))
        n.start()
        self.addCleanup(n.stop)
        self.dir = backup.backup_dir()
        self.t0 = datetime(2026, 11, 18, 3, 40, tzinfo=timezone.utc)      # a Wednesday

    def conn(self):
        return store.connect(self.src)

    def rows(self):
        c = self.conn()
        try:
            return store.backup_rows(c)
        finally:
            c.close()

    def files(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def perform(self, name, actor, **params):
        c = self.conn()
        try:
            return actions.perform(c, name, params, actor=actor, via="runner")
        finally:
            c.close()

    def fake(self, tier, n):
        """A recorded bundle that is only a small file (the table and the prune don't read it). n: its
        number, i.e. its time."""
        os.makedirs(self.dir, exist_ok=True)
        name = f"afclaude-backup-0123abcd-20261022T{n:09d}Z.tar.gz"
        with open(os.path.join(self.dir, name), "wb") as fh:
            fh.write(b"x" * 10)
        m = {"install_id": "0123abcd" * 4, "install_epoch": 0, "schema_version": store.SCHEMA_VERSION,
             "created_at": "2026-10-22T03:12:00.123Z"}
        self.perform("backup.record", actions.BACKUP_RECORD_ACTOR, file=name, tier=tier, manifest=m,
                     sha256="a" * 64, size=10, directory=self.dir)
        return name

    def test_a_run_makes_a_verified_read_only_recorded_bundle(self):
        res = backup.run_scheduled(now=self.t0)
        self.assertEqual((res["tier"], res["deleted"]), ("weekly", []))     # the first run of a week is the weekly
        path = os.path.join(self.dir, res["file"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        self.assertEqual(self.files(), [res["file"]])                        # no .partial, no work directory
        self.assertEqual(backup.verify(path)["install_id"], self.meta(self.src, "install_id"))
        (row,) = self.rows()
        self.assertEqual((row["file"], row["tier"], row["size"], row["sha256"], row["protected"]),
                         (res["file"], "weekly", os.path.getsize(path), sha(path), True))
        self.assertTrue(row["verified_at"])
        audit = [a for a in store.audit_log(self.conn(), target_type="backup")]
        self.assertEqual([(a["action"], a["actor"]) for a in audit], [("backup.record", "runner:backup")])

    def test_tier_is_the_first_run_since_the_weekly_day(self):
        def tier(day, hour=3):
            c = self.conn()
            try:
                return backup.tier_for(c, datetime(2026, 11, day, hour, 40, tzinfo=timezone.utc))
            finally:
                c.close()
        self.assertEqual(tier(18), "weekly")                                  # nothing recorded yet
        self.fake("weekly", 1)                                               # created_at 22.10.: an old one
        self.assertEqual(tier(18), "weekly")                                 # none since this week's Sunday
        res = backup.run_scheduled(now=self.t0)                              # Wednesday 18.11.: the weekly
        self.assertEqual(res["tier"], "weekly")
        self.assertEqual((tier(18, 23), tier(21)), ("daily", "daily"))       # the rest of that week
        self.assertEqual(tier(22), "weekly")                                 # Sunday 22.11.: a new week
        testenv.set_setting("backup_weekly_day", "wed", db=self.src)
        self.assertEqual(tier(18, 23), "daily")                              # this Wednesday already has one
        self.assertEqual(tier(25), "weekly")

    def new_install(self):
        """A fresh live database and an empty backup directory for one subtest."""
        d = tempfile.mkdtemp(prefix="afclaude-backup-live-")
        self.addCleanup(shutil.rmtree, d, True)
        self.src = os.path.join(d, "afclaude.db")
        store.connect(self.src, create=True).close()
        p = mock.patch.object(store, "DB_PATH", self.src)
        p.start()
        self.addCleanup(p.stop)
        self.dir = backup.backup_dir()

    def test_the_floor_survives_every_backup_keep(self):
        """The latest daily-or-weekly and the 2 latest weekly bundles are never pruned, whatever backup_keep
        says, 0 included; the rest goes by count; rows and files agree afterwards."""
        tiers = ["weekly", "daily", "daily", "weekly", "daily", "manual", "weekly", "daily", "daily", "manual"]
        for keep in (0, 1, 2, 3, 5, 14, 100):
            with self.subTest(keep=keep):
                self.new_install()
                names = [self.fake(t, i + 1) for i, t in enumerate(tiers)]
                floor = {names[8], names[6], names[3]}          # the newest daily; the 2 newest weeklies
                res = self.perform("backup.prune", actions.BACKUP_PRUNE_ACTOR, directory=self.dir, keep=keep)
                want = floor | set(names[max(0, len(names) - keep):] if keep else [])
                self.assertEqual({r["file"] for r in self.rows()}, want)
                self.assertEqual(set(self.files()), want)
                self.assertEqual(set(res["deleted"]), set(names) - want)
                self.assertEqual({r["file"] for r in self.rows() if r["protected"]}, floor)

    def test_the_grants_are_split_and_the_table_is_write_once(self):
        a = self.fake("daily", 1)
        b = self.fake("daily", 2)
        for actor in ("task-manager:abc", "agent:abc", "mcp:x", "runner:other", "runner", "user:7"):
            with self.subTest(actor=actor):
                for name, params in (("backup.prune", {"directory": self.dir, "keep": 0}),
                                     ("backup.record", {"file": a, "tier": "daily", "manifest": {},
                                                        "sha256": "a" * 64, "size": 10, "directory": self.dir})):
                    with self.assertRaises(actions.Forbidden):
                        self.perform(name, actor, **params)
        with self.assertRaisesRegex(actions.Forbidden, "write-once"):        # the record actor can't prune
            self.perform("backup.prune", actions.BACKUP_RECORD_ACTOR, directory=self.dir, keep=0)
        with self.assertRaisesRegex(actions.Forbidden, "write-once"):        # nor can the prune actor record
            self.perform("backup.record", actions.BACKUP_PRUNE_ACTOR, file=a, tier="daily", manifest={},
                         sha256="a" * 64, size=10, directory=self.dir)
        self.assertEqual(set(self.files()), {a, b})
        # at the table: no connection updates a row, none deletes one outside the prune; the floor even with
        # the prune gate open
        c = self.conn()
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "write-once"):
                c.execute("UPDATE backups SET tier='manual'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "only through"):
                c.execute("DELETE FROM backups WHERE file=?", (a,))
            c.rollback()
            c.execute("UPDATE backups_prune_gate SET allowed=1")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "protected"):
                c.execute("DELETE FROM backups WHERE file=?", (b,))         # the latest daily
            c.rollback()
        finally:
            c.close()
        with self.assertRaises(actions.Conflict):                            # a name is never recorded twice
            self.perform("backup.record", actions.BACKUP_RECORD_ACTOR, file=b, tier="daily",
                         manifest={"install_id": "x", "install_epoch": 0, "schema_version": 1, "created_at": "t"},
                         sha256="a" * 64, size=10, directory=self.dir)

    def test_record_checks_what_it_records(self):
        ok = dict(file="afclaude-backup-0123abcd-20261022T031200123Z.tar.gz", tier="daily",
                  manifest={"install_id": "x", "install_epoch": 0, "schema_version": 1, "created_at": "t"},
                  sha256="a" * 64, size=10, directory=self.dir)
        os.makedirs(self.dir)
        for bad, msg in ((dict(ok, file="../x.tar.gz"), "bundle name"), (dict(ok, tier="hourly"), "tier"),
                         (dict(ok, manifest={}), "manifest"), (dict(ok, sha256="z"), "sha256"),
                         (dict(ok, size=0), "size"), (dict(ok), "is not a file")):
            with self.subTest(msg=msg), self.assertRaisesRegex(ValueError, msg):
                self.perform("backup.record", actions.BACKUP_RECORD_ACTOR, **bad)
        self.assertEqual(self.rows(), [])

    def test_a_scheduled_run_prunes_after_the_new_bundle_is_recorded(self):
        testenv.set_setting("backup_keep", 1, db=self.src)
        old = [self.fake("daily", i) for i in (1, 2, 3)]
        res = backup.run_scheduled(now=self.t0)
        self.assertEqual(set(res["deleted"]), set(old))                     # keep 1 = the new one; old ones go
        self.assertEqual({r["file"] for r in self.rows()}, {res["file"]})
        prunes = [a for a in store.audit_log(self.conn(), target_type="backup") if a["action"] == "backup.prune"]
        self.assertEqual([a["actor"] for a in prunes], ["runner:backup_prune"])

    def test_a_manual_run_is_tier_manual_recorded_as_the_caller_and_prunes_nothing(self):
        testenv.set_setting("backup_keep", 0, db=self.src)
        old = [self.fake("daily", 1), self.fake("daily", 2)]
        res = backup.run_scheduled(manual=True)
        self.assertEqual((res["tier"], res["deleted"]), ("manual", []))
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(set(self.files()) - {res["file"]}, set(old))

    def paused(self):
        c = self.conn()
        try:
            return actions.get_setting(c, "automation_paused"), actions.get_setting(c, "automation_paused_reason")
        finally:
            c.close()

    def test_a_failed_verify_pauses_and_keeps_every_older_bundle(self):
        testenv.set_setting("backup_keep", 0, db=self.src)                  # retention would take all but the floor
        first = [self.fake("weekly", 1), self.fake("daily", 2), self.fake("daily", 3), self.fake("daily", 4)]
        before = (self.files(), [r["id"] for r in self.rows()])
        real = backup.verify
        for stage, patched in (("the partial", lambda b: real(b) if not b.endswith(".partial") else self.boom()),
                               ("the finished file", lambda b: self.boom() if b.endswith(".tar.gz") else real(b))):
            with self.subTest(stage=stage):
                with mock.patch.object(backup, "verify", patched):
                    with self.assertRaisesRegex(backup.BackupError, "boom"):
                        backup.daily_job()
                self.assertEqual((self.files(), [r["id"] for r in self.rows()]), before)   # nothing pruned, no new
                paused, why = self.paused()
                self.assertTrue(paused)
                self.assertIn(backup.FAIL_SUBJECT, why)
        self.assertEqual(set(self.files()), set(first))
        self.assertEqual(len([n for n in self.notes if backup.FAIL_SUBJECT in n]), 1)           # alerted once per pause

    def boom(self):
        raise backup.BackupError("boom")

    def test_no_install_id_fails_loud(self):
        """The deploy order: a database without install_id makes the job fail, alert and pause; the fix is
        `backup.py stamp` (the plan's deploy step), after which the next run works."""
        c = sqlite3.connect(self.src)
        c.execute("DELETE FROM meta WHERE key='install_id'")
        c.commit()
        c.close()
        with self.assertRaisesRegex(backup.BackupError, "backup.py stamp"):
            backup.daily_job()
        self.assertEqual((self.files(), self.rows()), ([], []))
        self.assertTrue(self.paused()[0])
        backup.stamp(self.src)
        self.assertEqual(backup.run_scheduled(now=self.t0)["tier"], "weekly")

    def test_a_full_disk_leaves_no_partial_and_no_row(self):
        real = tarfile.TarFile.addfile

        def full(tar, ti, fileobj=None):
            real(tar, ti, fileobj)
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(tarfile.TarFile, "addfile", full):
            with self.assertRaisesRegex(OSError, "No space"):
                backup.daily_job()
        self.assertEqual((self.files(), self.rows()), ([], []))
        self.assertTrue(self.paused()[0])

    def test_too_little_free_space_is_refused_before_anything_is_written(self):
        gone = shutil._ntuple_diskusage(100, 99, 1)
        with mock.patch.object(shutil, "disk_usage", return_value=gone):
            with self.assertRaisesRegex(backup.BackupError, "not enough free space"):
                backup.run_scheduled()
        self.assertEqual((self.files(), self.rows()), ([], []))

    def test_cli_list_and_prune(self):
        names = [self.fake("daily", i) for i in (1, 2, 3)]
        self.assertEqual([r["file"] for r in backup.listing()], names[::-1])
        self.assertEqual([r["protected"] for r in backup.listing()], [True, False, False])
        res = backup.prune(keep=1, actor=self.OWNER)
        self.assertEqual(res["deleted"], names[:2])
        with self.assertRaisesRegex(ValueError, "non-negative"):
            backup.prune(keep=-1, actor=self.OWNER)
        self.assertEqual(backup.main(["list"]), 0)

    def test_the_owner_hears_it_in_plain_words(self):
        import owner_text
        self.assertNotEqual(owner_text.for_subject(backup.FAIL_SUBJECT), owner_text.GENERIC)
        self.assertIn("backup", owner_text.for_subject("backup failed"))

    def test_the_setting_validation(self):
        for key, bad in (("backup_time", "25:00"), ("backup_time", "3pm"), ("backup_keep", -1),
                         ("backup_weekly_day", "someday")):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.perform("setting.set", self.OWNER, key=key, value=bad)
        self.perform("setting.set", self.OWNER, key="backup_time", value="4:05")
        c = self.conn()
        try:
            self.assertEqual(actions.get_setting(c, "backup_time"), "04:05")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
