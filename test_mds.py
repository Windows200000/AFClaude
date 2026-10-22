#!/usr/bin/env python3
"""Tests for dashboard/auth/mds.py (phase 6b P1, docs/phase6b_plan.md §3.2): the pinned root, the
blob's chain (test CA hierarchy incl. the cross-certificate shape of FIDO's R3 -> R46 move, dates,
the host name), the JWS signature and algorithms, the payload, the serial rule, the cache record,
the entry filter, both stale rules, fetch() against a mock transport, refresh()'s alert on failure.

Offline (synthetic blobs from auth_testkit). One live check against FIDO's real blob runs only with
AFCLAUDE_LIVE_MDS=1 (one GET; FIDO allows one an hour).

Needs the project venv (fido2, cryptography, httpx2; Python 3.12):
    .venv/bin/python -m unittest test_mds
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Like test_dashboard: under another interpreter every test here is skipped with the reason, so a
# combined `python3 -m unittest discover` stays green.
_MISSING = [m for m in ("fido2", "cryptography", "httpx2") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'fido2'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_mds") if _MISSING or sys.version_info < (3, 12) else ""
LIVE = os.environ.get("AFCLAUDE_LIVE_MDS") == "1"

if not SKIP:
    import hashlib

    import httpx2
    from fido2.mds3 import MetadataBlobPayloadEntry

    import auth_testkit as kit
    from dashboard.auth import mds

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
AAGUID = "11111111-1111-1111-1111-111111111111"


@unittest.skipIf(bool(SKIP), SKIP)
class PinnedRoot(unittest.TestCase):
    def test_root_file_matches_pin(self):
        with open(mds.ROOT_PATH, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), mds.ROOT_SHA256)
        root = mds.pinned_root()
        self.assertIn("GlobalSign Root R46", root.subject.rfc4514_string())
        self.assertEqual(root.subject, root.issuer)
        self.assertEqual(root.not_valid_after_utc.year, 2046)

    def test_tampered_root_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".der", delete=False) as fh:
            fh.write(kit.der(kit.Pki().root))
        try:
            with self.assertRaises(mds.MdsError) as ctx:
                mds.pinned_root(fh.name)
            self.assertEqual(ctx.exception.code, "root_mismatch")
        finally:
            os.unlink(fh.name)


@unittest.skipIf(bool(SKIP), SKIP)
class VerifyBlob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pki = kit.Pki()
        cls.body = kit.payload([kit.entry(aaguid=AAGUID)])

    def verify(self, blob, root=None, **kw):
        return mds.verify_blob(blob, now=kw.pop("now", NOW), root=root or self.pki.root, **kw)

    def refused(self, code, blob, **kw):
        with self.assertRaises(mds.MdsError) as ctx:
            self.verify(blob, **kw)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def test_valid_chain(self):
        blob = kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c)
        vb = self.verify(blob)
        self.assertEqual((vb.serial, vb.next_update, len(vb.payload.entries)), (291, date(2026, 11, 1), 1))
        self.assertEqual(vb.sha256, hashlib.sha256(blob).hexdigest())
        self.assertEqual(len(vb.chain), 3)

    def test_cross_certificate_in_x5c(self):
        # the live shape: leaf, intermediate, and the new root cross-signed by the old one
        blob = kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c + [self.pki.cross])
        self.assertEqual(self.verify(blob).serial, 291)

    def test_x5c_order_does_not_matter(self):
        blob = kit.make_blob(self.body, self.pki.leaf_key, [self.pki.leaf, self.pki.cross, self.pki.intermediate])
        self.assertEqual(self.verify(blob).serial, 291)

    def test_rs256(self):
        pki = kit.Pki(leaf_rsa=True)
        blob = kit.make_blob(self.body, pki.leaf_key, pki.x5c, alg="RS256")
        self.assertEqual(self.verify(blob, root=pki.root).serial, 291)

    def test_wrong_root(self):
        self.refused("chain", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c), root=kit.Pki().root)

    def test_old_root_anchors_only_through_the_cross_certificate(self):
        # the shape fido2's strict-order check can't follow; path building can, either way
        blob = kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c + [self.pki.cross])
        self.assertEqual(self.verify(blob, root=self.pki.old_root).serial, 291)
        self.refused("chain", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c), root=self.pki.old_root)

    def test_pinned_root_refuses_test_chain(self):
        with self.assertRaises(mds.MdsError) as ctx:
            mds.verify_blob(kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c), now=NOW)
        self.assertEqual(ctx.exception.code, "chain")

    def test_expired_intermediate(self):
        pki = kit.Pki(int_end=NOW - timedelta(days=1))
        self.refused("chain", kit.make_blob(self.body, pki.leaf_key, pki.x5c), root=pki.root)

    def test_not_yet_valid_at_time(self):
        self.refused("chain", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c),
                     now=NOW - timedelta(days=60))

    def test_missing_intermediate(self):
        self.refused("chain", kit.make_blob(self.body, self.pki.leaf_key, [self.pki.leaf]))

    def test_wrong_host(self):
        pki = kit.Pki(host="mds.example.test")
        self.refused("chain", kit.make_blob(self.body, pki.leaf_key, pki.x5c), root=pki.root)

    def test_bad_signature(self):
        blob = kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c)
        h, p, s = blob.split(b".")
        other = kit.make_blob(kit.payload([], no=999), self.pki.leaf_key, self.pki.x5c).split(b".")[1]
        self.refused("bad_signature", b".".join([h, other, s]))

    def test_signed_by_another_key(self):
        self.refused("bad_signature", kit.make_blob(self.body, kit.ec_key(), self.pki.x5c))

    def test_algorithms_refused(self):
        for alg in ("none", "HS256", "ES384", "PS256"):
            with self.subTest(alg=alg):
                self.refused("bad_alg", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c, alg=alg))

    def test_alg_key_mismatch(self):
        self.refused("bad_key", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c,
                                              header={"alg": "RS256"}))

    def test_malformed(self):
        good = kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c)
        h, p, s = good.split(b".")
        self.refused("malformed", h + b"." + p)
        self.refused("malformed", kit.b64url(b"{not json") + b"." + p + b"." + s)
        self.refused("malformed", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c, header={"x5c": []}))
        self.refused("malformed", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c, header={"crit": ["b64"]}))
        self.refused("malformed", kit.make_blob(self.body, self.pki.leaf_key, self.pki.x5c, header={"x5c": ["@@"]}))

    def test_malformed_payload(self):
        for raw in (b"not json", b"[]", b'{"no": 1}'):
            with self.subTest(raw=raw):
                self.refused("payload_malformed",
                             kit.make_blob({}, self.pki.leaf_key, self.pki.x5c, raw_payload=raw))
        broken = kit.payload([kit.entry(aaguid=AAGUID, drop=("keyProtection",))])
        self.refused("payload_malformed", kit.make_blob(broken, self.pki.leaf_key, self.pki.x5c))

    def test_mds31_statement_without_uv_details_and_attachment_hint(self):
        # live since 2026: passkey-provider statements (e.g. a password manager's) leave both out
        body = kit.payload([kit.entry(aaguid=AAGUID, drop=("userVerificationDetails", "attachmentHint"),
                                      key_protection=("software",), multi_device="explicit")])
        vb = self.verify(kit.make_blob(body, self.pki.leaf_key, self.pki.x5c))
        self.assertEqual(vb.payload.entries[0].metadata_statement.key_protection, ["software"])


@unittest.skipIf(bool(SKIP), SKIP)
class SerialAndRecord(unittest.TestCase):
    def test_serial_rule(self):
        self.assertEqual(mds.check_serial(5, None), "new")
        self.assertEqual(mds.check_serial(6, 5), "new")
        self.assertEqual(mds.check_serial(5, 5), "unchanged")
        with self.assertRaises(mds.MdsRollback):
            mds.check_serial(4, 5)

    def test_record_round_trip(self):
        pki = kit.Pki()
        vb = mds.verify_blob(kit.make_blob(kit.payload([kit.entry(aaguid=AAGUID)] * 3), pki.leaf_key, pki.x5c),
                             now=NOW, root=pki.root)
        rec = mds.to_record(vb, NOW)
        self.assertEqual((rec.serial, rec.entry_count, rec.fetched_at), (291, 3, NOW))
        self.assertLess(len(rec.payload_z), len(vb.payload_json))
        lst = mds.load_record(rec)
        self.assertEqual((lst.serial, lst.verified_at, len(lst.payload.entries)), (291, NOW, 3))
        later = NOW + timedelta(days=3)
        self.assertEqual(mds.load_record(rec, verified_at=later).verified_at, later)

    def test_corrupt_record(self):
        rec = mds.CacheRecord(serial=1, next_update=date(2026, 11, 1), fetched_at=NOW, sha256="",
                              payload_z=b"garbage", entry_count=0)
        with self.assertRaises(mds.MdsError) as ctx:
            mds.load_record(rec)
        self.assertEqual(ctx.exception.code, "cache_corrupt")


def _entry(**kw):
    return MetadataBlobPayloadEntry.from_dict(kit.entry(aaguid=AAGUID, **kw)) if not SKIP else None


@unittest.skipIf(bool(SKIP), SKIP)
class EntryFilter(unittest.TestCase):
    def usable(self, **kw):
        return mds.hardware_usable(_entry(**kw))

    def test_certified_hardware(self):
        for status in ("FIDO_CERTIFIED", "FIDO_CERTIFIED_L1", "FIDO_CERTIFIED_L2plus", "FIPS140_CERTIFIED_L2",
                       "UPDATE_AVAILABLE", "NOT_FIDO_CERTIFIED"):
            with self.subTest(status=status):
                self.assertEqual(self.usable(statuses=(status,)), (True, "ok"))

    def test_problem_statuses_anywhere_in_history(self):
        for status in ("REVOKED", "USER_VERIFICATION_BYPASS", "ATTESTATION_KEY_COMPROMISE",
                       "USER_KEY_REMOTE_COMPROMISE", "USER_KEY_PHYSICAL_COMPROMISE"):
            with self.subTest(status=status):
                e = _entry(statuses=("FIDO_CERTIFIED_L1", status, "UPDATE_AVAILABLE"))
                self.assertEqual(mds.problem_statuses(e), [status])
                self.assertEqual(mds.hardware_usable(e), (False, "problem_status"))

    def test_newest_status_must_be_good(self):
        for status in ("RETIRED", "SELF_ASSERTION_SUBMITTED"):
            with self.subTest(status=status):
                self.assertEqual(self.usable(statuses=("FIDO_CERTIFIED_L1", status)), (False, "status"))
        self.assertEqual(self.usable(statuses=("RETIRED", "FIDO_CERTIFIED_L1")), (True, "ok"))

    def test_key_protection(self):
        for kp, ok in ((("hardware",), True), (("hardware", "secure_element"), True), (("hardware", "tee"), True),
                       (("tee",), True), (("software",), False), (("hardware", "remote_handle"), False),
                       (("hardware", "secure_element", "remote_handle"), False), (("software", "tee"), False), ((), False)):
            with self.subTest(kp=kp):
                self.assertEqual(self.usable(key_protection=kp)[0], ok)

    def test_syncing_provider_never_hardware(self):
        self.assertEqual(self.usable(multi_device="explicit"), (False, "syncing"))
        self.assertEqual(self.usable(multi_device="implicit"), (False, "syncing"))
        self.assertEqual(self.usable(multi_device="unsupported"), (True, "ok"))

    def test_no_statement(self):
        self.assertEqual(self.usable(statement=False), (False, "no_statement"))


@unittest.skipIf(bool(SKIP), SKIP)
class StaleRules(unittest.TestCase):
    def setUp(self):
        pki = kit.Pki()
        vb = mds.verify_blob(kit.make_blob(kit.payload([], next_update=date(2026, 11, 1)), pki.leaf_key, pki.x5c),
                             now=NOW, root=pki.root)
        self.lst = mds.load_record(mds.to_record(vb, NOW))

    def test_registration_stale_after_next_update_plus_7_days(self):
        edge = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.assertFalse(self.lst.registration_stale(datetime(2026, 11, 2, tzinfo=timezone.utc)))
        self.assertFalse(self.lst.registration_stale(edge))
        self.assertTrue(self.lst.registration_stale(edge + timedelta(seconds=1)))

    def test_hardware_login_max_age(self):
        self.assertFalse(self.lst.too_old_for_hardware_login(NOW + timedelta(days=30)))
        self.assertTrue(self.lst.too_old_for_hardware_login(NOW + timedelta(days=30, seconds=1)))
        self.assertFalse(self.lst.too_old_for_hardware_login(NOW + timedelta(days=60), max_age_days=90))
        self.assertTrue(self.lst.too_old_for_hardware_login(NOW + timedelta(days=8), max_age_days=7))
        for bad in (6, 91):
            with self.assertRaises(ValueError):
                self.lst.too_old_for_hardware_login(NOW, max_age_days=bad)

    def test_naive_time_refused(self):
        with self.assertRaises(ValueError):
            self.lst.registration_stale(datetime(2026, 11, 2))


@unittest.skipIf(bool(SKIP), SKIP)
class Fetch(unittest.TestCase):
    def fetch(self, handler, **kw):
        return mds.fetch("https://mds.example.test/", transport=httpx2.MockTransport(handler), **kw)

    def failed(self, code, handler, **kw):
        with self.assertRaises(mds.MdsError) as ctx:
            self.fetch(handler, **kw)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def test_ok(self):
        self.assertEqual(self.fetch(lambda req: httpx2.Response(200, content=b"a.b.c")), b"a.b.c")

    def test_redirect_not_followed(self):
        self.failed("http_status", lambda req: httpx2.Response(301, headers={"location": "https://elsewhere.test/"}))

    def test_http_error_status(self):
        self.failed("http_status", lambda req: httpx2.Response(503))

    def test_too_large(self):
        self.failed("too_large", lambda req: httpx2.Response(200, content=b"x" * 2048), max_bytes=1024)

    def test_timeout(self):
        def handler(req):
            raise httpx2.ReadTimeout("slow", request=req)
        self.failed("timeout", handler)

    def test_total_deadline(self):
        def slow():
            for _ in range(3):
                time.sleep(0.03)
                yield b"x"
        self.failed("timeout", lambda req: httpx2.Response(200, content=slow()), timeout=0.05)

    def test_connect_error(self):
        def handler(req):
            raise httpx2.ConnectError("refused", request=req)
        self.failed("fetch", handler)

    def test_rate_limit(self):
        self.assertTrue(mds.may_fetch(None, NOW))
        self.assertFalse(mds.may_fetch(NOW - timedelta(minutes=59), NOW))
        self.assertTrue(mds.may_fetch(NOW - timedelta(hours=1), NOW))


@unittest.skipIf(bool(SKIP), SKIP)
class Refresh(unittest.TestCase):
    def setUp(self):
        self.pki = kit.Pki()
        self.alerts = []

    def refresh(self, blob=None, stored=None, last=None, fetcher=None):
        blob = blob or kit.make_blob(kit.payload([kit.entry(aaguid=AAGUID)], no=300), self.pki.leaf_key, self.pki.x5c)
        return mds.refresh(stored_serial=stored, last_fetched_at=last, now=NOW, fetcher=fetcher or (lambda: blob),
                           alert=lambda s, b: self.alerts.append((s, b)), root=self.pki.root)

    def test_new(self):
        res = self.refresh(stored=299)
        self.assertEqual((res.outcome, res.serial, res.record.serial, res.record.entry_count), ("new", 300, 300, 1))
        self.assertEqual(self.alerts, [])

    def test_unchanged(self):
        res = self.refresh(stored=300)
        self.assertEqual((res.outcome, res.record), ("unchanged", None))

    def test_rollback_alerts_and_raises(self):
        with self.assertRaises(mds.MdsRollback):
            self.refresh(stored=301)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0], mds.ALERT_SUBJECT)
        self.assertIn("rollback", self.alerts[0][1])

    def test_failed_fetch_alerts_and_raises(self):
        def fetcher():
            raise mds.MdsError("timeout", "GET: not done after 30 s")
        with self.assertRaises(mds.MdsError):
            self.refresh(fetcher=fetcher)
        self.assertEqual([s for s, _ in self.alerts], [mds.ALERT_SUBJECT])

    def test_failed_verify_alerts(self):
        with self.assertRaises(mds.MdsError):
            self.refresh(blob=kit.make_blob(kit.payload([]), kit.ec_key(), self.pki.x5c))
        self.assertEqual(len(self.alerts), 1)

    def test_too_soon_no_fetch_no_alert(self):
        def fetcher():
            raise AssertionError("fetched")
        with self.assertRaises(mds.MdsTooSoon):
            self.refresh(last=NOW - timedelta(minutes=10), fetcher=fetcher)
        self.assertEqual(self.alerts, [])


@unittest.skipIf(bool(SKIP) or not LIVE, SKIP or "live MDS check: set AFCLAUDE_LIVE_MDS=1 (one GET to FIDO)")
class LiveBlob(unittest.TestCase):
    def test_live_blob_verifies_with_pinned_root(self):
        blob = mds.fetch()
        started = time.monotonic()
        vb = mds.verify_blob(blob, now=datetime.now(timezone.utc))
        seconds = time.monotonic() - started
        print(f"\nlive MDS: serial {vb.serial}, nextUpdate {vb.next_update}, {len(vb.payload.entries)} entries, "
              f"{len(blob)} bytes, verified in {seconds:.2f} s; chain {vb.chain}", file=sys.stderr)
        self.assertGreater(len(vb.payload.entries), 100)


if __name__ == "__main__":
    unittest.main()
