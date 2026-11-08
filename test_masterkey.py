#!/usr/bin/env python3
"""Offline tests for masterkey.py (phase 5b K3, D-163, D-161, D-235): the key from the env or a file
generated once (mode 0600), a race of three ensure() calls (threads and processes) giving one key, an
env vs file conflict, a wrong key against the stored master_key_check (and no new key beside it), and
seal/open: round trip, a tampered blob, the wrong purpose, a foreign key, a malformed blob."""
import base64
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import masterkey  # noqa: E402
import store  # noqa: E402


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class MasterKeyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        self.db = testenv.fresh_db(self)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.addCleanup(masterkey.forget)
        masterkey.forget()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(masterkey.ENV, None)

    def keyfile(self):
        return os.path.join(self.data, masterkey.KEY_FILE)

    def read_key(self):
        with open(self.keyfile()) as fh:
            return fh.read()

    def test_generated_once_mode_0600_and_check_written(self):
        kid = masterkey.ensure(self.data, self.conn)
        self.assertEqual(os.stat(self.keyfile()).st_mode & 0o777, 0o600)
        first = self.read_key()
        self.assertEqual(len(base64.urlsafe_b64decode(first.strip())), 32)
        self.assertIsNotNone(store.get_meta(self.conn, masterkey.CHECK_META))
        self.assertEqual(masterkey.ensure(self.data, self.conn), kid)   # a second start: the same key
        self.assertEqual(self.read_key(), first)
        rows = [r for r in store.audit_log(self.conn, 20) if r["action"] == "masterkey.check"]
        self.assertEqual(len(rows), 1)   # written once, audited, no secret in it
        self.assertNotIn(first.strip(), str(rows[0]))

    def test_race_of_three_ensure_calls_gives_one_key(self):
        ids, errors = [], []
        barrier = threading.Barrier(3)

        def go():
            try:
                conn = store.connect(self.db)
                barrier.wait()
                ids.append(masterkey.ensure(self.data, conn))
                conn.close()
            except Exception as e:   # noqa: BLE001  (collected, asserted below)
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(masterkey.key_id(masterkey._decode_key(self.read_key(), "x")), ids[0])

    def test_race_of_three_processes_gives_one_key(self):
        code = ("import sys; sys.path.insert(0, %r); import masterkey; "
                "print(masterkey.key_id(masterkey.find_key(sys.argv[1])))" % HERE)
        procs = [subprocess.Popen([sys.executable, "-c", code, self.data], stdout=subprocess.PIPE, text=True)
                 for _ in range(3)]
        outs = [p.communicate()[0].strip() for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs))
        self.assertEqual(len(set(outs)), 1)
        self.assertEqual(len(outs[0]), 12)

    def test_env_key_creates_no_file_and_a_matching_file_is_fine(self):
        raw = os.urandom(32)
        os.environ[masterkey.ENV] = b64(raw)
        kid = masterkey.ensure(self.data, self.conn)
        self.assertEqual(kid, masterkey.key_id(raw))
        self.assertFalse(os.path.exists(self.keyfile()))
        with open(self.keyfile(), "w") as fh:
            fh.write(base64.urlsafe_b64encode(raw).decode() + "\n")   # same key, other alphabet
        os.chmod(self.keyfile(), 0o600)
        self.assertEqual(masterkey.ensure(self.data, self.conn), kid)

    def test_empty_env_counts_as_unset(self):
        os.environ[masterkey.ENV] = ""
        masterkey.ensure(self.data, self.conn)
        self.assertTrue(os.path.exists(self.keyfile()))

    def test_env_vs_file_conflict_is_an_error(self):
        masterkey.ensure(self.data, self.conn)
        os.environ[masterkey.ENV] = b64(os.urandom(32))
        with self.assertRaisesRegex(masterkey.MasterKeyError, "different keys"):
            masterkey.ensure(self.data, self.conn)

    def test_bad_env_and_bad_file_are_errors_not_regenerated(self):
        os.environ[masterkey.ENV] = b64(os.urandom(16))
        with self.assertRaisesRegex(masterkey.MasterKeyError, "32 bytes"):
            masterkey.ensure(self.data, self.conn)
        os.environ[masterkey.ENV] = "not base64 !!"
        with self.assertRaisesRegex(masterkey.MasterKeyError, "base64"):
            masterkey.ensure(self.data, self.conn)
        os.environ.pop(masterkey.ENV)
        with open(self.keyfile(), "w") as fh:
            fh.write("")   # a torn file
        os.chmod(self.keyfile(), 0o600)
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.ensure(self.data, self.conn)
        with open(self.keyfile()) as fh:
            self.assertEqual(fh.read(), "")   # untouched

    def test_missing_data_dir(self):
        with self.assertRaisesRegex(masterkey.MasterKeyError, "not mounted"):
            masterkey.ensure(os.path.join(self.data, "nope"), self.conn)

    def test_wrong_key_against_the_stored_check(self):
        masterkey.ensure(self.data, self.conn)
        check = store.get_meta(self.conn, masterkey.CHECK_META)
        os.remove(self.keyfile())   # a DB restored without its key: a new one must not be made silently ...
        os.environ[masterkey.ENV] = b64(os.urandom(32))   # ... nor a wrong env value accepted
        masterkey.forget()
        with self.assertRaisesRegex(masterkey.MasterKeyError, "does not match"):
            masterkey.ensure(self.data, self.conn)
        self.assertEqual(store.get_meta(self.conn, masterkey.CHECK_META), check)
        with self.assertRaises(masterkey.MasterKeyError):   # and nothing was loaded
            masterkey.seal("x", b"y")

    def test_lost_key_file_with_a_check_makes_no_new_key(self):
        kid = masterkey.ensure(self.data, self.conn)
        saved = self.read_key()
        os.remove(self.keyfile())
        masterkey.forget()
        with self.assertRaisesRegex(masterkey.MasterKeyError, "no key is available"):
            masterkey.ensure(self.data, self.conn)
        self.assertFalse(os.path.exists(self.keyfile()))   # nothing was generated
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.seal("x", b"y")
        with open(self.keyfile(), "w") as fh:   # the key put back (here: as a file) starts fine
            fh.write(saved)
        os.chmod(self.keyfile(), 0o600)
        self.assertEqual(masterkey.ensure(self.data, self.conn), kid)

    def test_lost_key_file_then_env_key_put_back(self):
        raw = os.urandom(32)
        os.environ[masterkey.ENV] = b64(raw)
        kid = masterkey.ensure(self.data, self.conn)
        os.environ.pop(masterkey.ENV)
        masterkey.forget()
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.ensure(self.data, self.conn)
        self.assertFalse(os.path.exists(self.keyfile()))
        os.environ[masterkey.ENV] = b64(raw)   # no "differ" error: no wrong file was left behind
        self.assertEqual(masterkey.ensure(self.data, self.conn), kid)

    def test_key_file_accessible_to_others_is_refused(self):
        masterkey.ensure(self.data, self.conn)
        masterkey.forget()
        for mode in (0o640, 0o604, 0o666):
            os.chmod(self.keyfile(), mode)
            with self.assertRaisesRegex(masterkey.MasterKeyError, "accessible to others"):
                masterkey.ensure(self.data, self.conn)

    def test_unusable_files_raise_masterkey_error(self):
        os.mkdir(self.keyfile())   # a directory where the key file should be
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.ensure(self.data, self.conn)
        os.rmdir(self.keyfile())
        lock = os.path.join(self.data, masterkey.LOCK_FILE)
        os.remove(lock)
        os.mkdir(lock)   # and as the lock file
        with self.assertRaisesRegex(masterkey.MasterKeyError, "lock file"):
            masterkey.ensure(self.data, self.conn)
        os.rmdir(lock)
        with mock.patch.object(masterkey.os, "fsync", side_effect=OSError("disk")):
            with self.assertRaisesRegex(masterkey.MasterKeyError, "cannot write"):
                masterkey.ensure(self.data, self.conn)

    def test_context_binds_a_blob_to_its_record(self):
        masterkey.ensure(self.data, self.conn)
        blob = masterkey.seal("totp", b"seed", "user:7")
        self.assertEqual(masterkey.open("totp", blob, "user:7"), b"seed")
        for ctx in ("user:8", None, ""):   # swapped to another record, or no context
            with self.assertRaises(masterkey.MasterKeyError, msg=repr(ctx)):
                masterkey.open("totp", blob, ctx)
        plain = masterkey.seal("totp", b"seed")   # sealed without one: opens only without one
        self.assertEqual(masterkey.open("totp", plain), b"seed")
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.open("totp", plain, "user:7")
        empty = masterkey.seal("totp", b"seed", "")
        self.assertEqual(masterkey.open("totp", empty, ""), b"seed")
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.open("totp", empty)

    def test_blob_base64_is_strict_and_purpose_fullmatch(self):
        masterkey.ensure(self.data, self.conn)
        blob = masterkey.seal("totp", b"secret")
        v, kid, nonce, ct = blob.split(":")
        for bad in (f"{v}:{kid}:{nonce}:{ct}\n", f"{v}:{kid}:{nonce}:{ct[:4]}!{ct[4:]}",
                    f"{v}:{kid}:{nonce}:{ct.rstrip('=')}", f"{v}:{kid}:{nonce}\n:{ct}", f"{v}:{kid}:{nonce}:{ct}é"):
            with self.assertRaises(masterkey.MasterKeyError, msg=repr(bad)):
                masterkey.open("totp", bad)
        for bad in ("totp\n", "", "Totp", "a b"):
            with self.assertRaises(masterkey.MasterKeyError, msg=repr(bad)):
                masterkey.seal(bad, b"x")
        self.assertEqual(masterkey.open("totp", blob), b"secret")

    def test_seal_open_round_trip(self):
        masterkey.ensure(self.data, self.conn)
        blob = masterkey.seal("totp", b"JBSWY3DPEHPK3PXP")
        v, kid, nonce, ct = blob.split(":")
        self.assertEqual((v, len(kid)), ("v1", 12))
        self.assertNotIn("JBSWY3", blob)
        self.assertEqual(masterkey.open("totp", blob), b"JBSWY3DPEHPK3PXP")
        self.assertNotEqual(masterkey.seal("totp", b"a"), masterkey.seal("totp", b"a"))   # fresh nonce
        self.assertEqual(masterkey.open("totp", masterkey.seal("totp", b"")), b"")

    def test_tampered_blob_wrong_purpose_foreign_key_malformed(self):
        masterkey.ensure(self.data, self.conn)
        blob = masterkey.seal("totp", b"secret")
        v, kid, nonce, ct = blob.split(":")
        raw = bytearray(base64.urlsafe_b64decode(ct))
        raw[0] ^= 1
        bad_ct = f"{v}:{kid}:{nonce}:{base64.urlsafe_b64encode(bytes(raw)).decode()}"
        for bad in (bad_ct, f"{v}:{kid}:{nonce}:", f"{v}:{kid}:AAAA:{ct}", "v2:" + blob[3:], "nonsense",
                    f"{v}:{'0' * 12}:{nonce}:{ct}"):
            with self.assertRaises(masterkey.MasterKeyError, msg=bad):
                masterkey.open("totp", bad)
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.open("oidc", blob)   # the wrong purpose
        with self.assertRaises(masterkey.MasterKeyError):
            masterkey.seal("Bad Purpose", b"x")

    def test_purposes_use_different_keys(self):
        key = os.urandom(32)
        self.assertNotEqual(masterkey._purpose_key(key, "a"), masterkey._purpose_key(key, "b"))

    def test_seal_before_ensure_is_an_error(self):
        with self.assertRaisesRegex(masterkey.MasterKeyError, "not loaded"):
            masterkey.seal("totp", b"x")


if __name__ == "__main__":
    unittest.main()
