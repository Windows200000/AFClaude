#!/usr/bin/env python3
"""Tests for dashboard/auth/passkeys.py (phase 6b P2, docs/phase6b_plan.md §3.3, §3.4; D-159): every
row of the classification table with the soft authenticator (auth_testkit) against a test MDS list,
fido2's recorded real-device attestations (TPM / Windows Hello, packed, Apple, Android SafetyNet),
and the assertion checks (UV, BE/BS, the sign counter, a model revoked after registration, the
list's age).

Offline. Needs the project venv (fido2, cryptography; Python 3.12):
    .venv/bin/python -m unittest test_passkeys
"""
import importlib.util
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_MISSING = [m for m in ("fido2", "cryptography", "httpx2") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'fido2'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_passkeys") if _MISSING or sys.version_info < (3, 12) else ""

if not SKIP:
    from fido2.webauthn import AttestationObject, AuthenticatorData

    import auth_testkit as kit
    from dashboard.auth import mds, passkeys

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
AAGUID = b"\x11" * 16
OTHER = b"\x22" * 16
ZERO = b"\x00" * 16


def mds_list(entries, *, next_update=date(2026, 11, 1), verified_at=NOW):
    pki = kit.Pki()
    vb = mds.verify_blob(kit.make_blob(kit.payload(entries, next_update=next_update), pki.leaf_key, pki.x5c),
                         now=NOW, root=pki.root)
    return mds.load_record(mds.to_record(vb, verified_at))


@unittest.skipIf(bool(SKIP), SKIP)
class Classify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca = kit.AttestationCa(AAGUID)
        cls.lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID), roots=[kit.der(cls.ca.root)])])

    def classify(self, att, cdh, lst="default", rk=True, known=False, now=NOW):
        return passkeys.classify(att, cdh, self.lst if lst == "default" else lst, now=now,
                                 cred_props_rk=rk, credential_known=known)

    def check(self, result, kind, reason):
        self.assertEqual((result.kind, result.reason), (kind, reason), result)
        self.assertIn(reason, passkeys.MESSAGES)
        return result

    def test_hardware(self):
        r = self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register()), "hardware", "mds_verified")
        self.assertTrue(r.counts_alone)
        self.assertIsNone(r.warning)
        self.assertEqual((r.aaguid, r.mds_serial, r.mds_description, r.backup_eligible, r.attestation_format),
                         (kit.uuid_str(AAGUID), 291, "Test Security Key", False, "packed"))

    # --- refused --------------------------------------------------------------------------------

    def test_uv_missing(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(uv=False)), "refused", "uv_missing")

    def test_bs_without_be(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(bs=True)), "refused", "bs_without_be")

    def test_not_discoverable(self):
        att = kit.SoftAuthenticator(AAGUID, self.ca).register()
        self.check(self.classify(*att, rk=False), "refused", "not_discoverable")
        self.check(self.classify(*att, rk=None), "hardware", "mds_verified")   # not reported: fido2 + residentKey required

    def test_credential_already_registered(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(), known=True),
                   "refused", "credential_exists")

    def test_no_list(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(), lst=None),
                   "refused", "mds_unavailable")

    def test_stale_list_refuses_every_registration(self):
        late = datetime(2026, 11, 8, 0, 0, 1, tzinfo=timezone.utc)
        for kw in ({}, {"be": True}, {"fmt": "none"}):
            with self.subTest(**kw):
                self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(**kw), now=late),
                           "refused", "mds_out_of_date")
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(),
                                 now=datetime(2026, 11, 8, tzinfo=timezone.utc)), "hardware", "mds_verified")

    def test_revoked_or_compromised_model(self):
        for status in sorted(s.value for s in mds.PROBLEM_STATUSES):
            lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID), roots=[kit.der(self.ca.root)],
                                      statuses=("FIDO_CERTIFIED_L1", status))])
            for kw in ({}, {"be": True, "bs": True}, {"fmt": "none"}):
                with self.subTest(status=status, **kw):
                    self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(**kw), lst=lst),
                               "refused", "mds_problem_status")

    def test_invalid_attestation_signature(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(tamper=True)),
                   "refused", "attestation_invalid")

    def test_attestation_cert_aaguid_mismatch(self):
        ca = kit.AttestationCa(OTHER)    # the certificate says OTHER, the authenticator data AAGUID
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, ca).register()), "refused", "attestation_invalid")

    # --- synced (one factor, D-159) -------------------------------------------------------------

    def test_backup_eligible(self):
        for bs in (False, True):
            with self.subTest(bs=bs):
                r = self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(be=True, bs=bs)),
                               "synced", "backup_eligible")
                self.assertFalse(r.counts_alone)
                self.assertEqual(r.warning, passkeys.SYNCED_WARNING)
                self.assertEqual(r.backed_up, bs)

    def test_none_attestation(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(fmt="none")), "synced", "attestation_none")

    def test_self_attestation(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(self_attest=True)),
                   "synced", "self_attestation")

    def test_unsupported_format(self):
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(fmt="x-unknown")),
                   "synced", "attestation_unsupported")

    def test_not_in_mds(self):
        ca = kit.AttestationCa(OTHER)
        self.check(self.classify(*kit.SoftAuthenticator(OTHER, ca).register()), "synced", "not_in_mds")

    def test_chain_to_another_root(self):
        ca = kit.AttestationCa(AAGUID)   # same AAGUID, a root the MDS entry doesn't list
        self.check(self.classify(*kit.SoftAuthenticator(AAGUID, ca).register()), "synced", "chain_not_verified")

    def test_model_not_listed_as_hardware(self):
        for kw in ({"key_protection": ("software",)}, {"key_protection": ("hardware", "remote_handle")},
                   {"multi_device": "explicit"}, {"statuses": ("FIDO_CERTIFIED_L1", "RETIRED")}):
            with self.subTest(**kw):
                lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID), roots=[kit.der(self.ca.root)], **kw)])
                self.check(self.classify(*kit.SoftAuthenticator(AAGUID, self.ca).register(), lst=lst),
                           "synced", "mds_not_hardware")

    def test_zero_aaguid_found_by_key_identifier(self):
        ca = kit.AttestationCa(ZERO, with_aaguid_ext=False)
        lst = mds_list([kit.entry(key_ids=[ca.key_id], roots=[kit.der(ca.root)])])
        r = self.check(self.classify(*kit.SoftAuthenticator(ZERO, ca).register(), lst=lst), "hardware", "mds_verified")
        self.assertEqual(r.mds_key_id, ca.key_id.hex())

    def test_warning_wording(self):
        # D-159: "warn the user that it's non hardware and say he'll still require 2fa"
        self.assertIn("not hardware-bound", passkeys.SYNCED_WARNING)
        self.assertIn("second factor", passkeys.SYNCED_WARNING)


@unittest.skipIf(bool(SKIP), SKIP)
class RecordedAttestations(unittest.TestCase):
    """fido2's recorded real-device attestations (auth_testkit.VECTORS)."""

    def classify(self, fmt, lst):
        v = kit.VECTORS[fmt]
        att = AttestationObject.create(fmt, AuthenticatorData(v["auth_data"]), v["statement"])
        return passkeys.classify(att, v["client_data_hash"], lst, now=NOW, cred_props_rk=None, credential_known=False)

    def test_windows_hello_tpm_is_hardware_with_its_mds_entry(self):
        lst = mds_list([kit.entry(aaguid=kit.WINDOWS_HELLO_AAGUID, roots=[kit.MS_TPM_ROOT], key_protection=("hardware",),
                                  statuses=("FIDO_CERTIFIED_L1", "FIDO_CERTIFIED"),
                                  description="Windows Hello Hardware Authenticator")])
        r = self.classify("tpm", lst)
        self.assertEqual((r.kind, r.reason, r.mds_description),
                         ("hardware", "mds_verified", "Windows Hello Hardware Authenticator"))

    def test_windows_hello_tpm_without_entry_is_synced(self):
        self.assertEqual(self.classify("tpm", mds_list([])).reason, "not_in_mds")

    def test_old_packed_without_uv_refused(self):
        r = self.classify("packed", mds_list([]))    # a 2018 key: UP only
        self.assertEqual((r.kind, r.reason), ("refused", "uv_missing"))

    def test_apple_anonymous_is_synced(self):
        r = self.classify("apple", mds_list([]))
        self.assertEqual((r.kind, r.reason), ("synced", "not_in_mds"))

    def test_android_safetynet_is_synced(self):
        r = self.classify("android-safetynet", mds_list([]))
        self.assertEqual((r.kind, r.reason), ("synced", "not_in_mds"))


@unittest.skipIf(bool(SKIP), SKIP)
class Assertion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID))])
        cls.auth = kit.SoftAuthenticator(AAGUID)

    def stored(self, kind="hardware", be=False, count=5, aaguid=AAGUID, key_id=None):
        return passkeys.StoredPasskey(kind=kind, backup_eligible=be, sign_count=count, aaguid=kit.uuid_str(aaguid),
                                      mds_key_id=key_id)

    def check(self, stored, lst="default", now=NOW, **kw):
        return passkeys.check_assertion(stored, self.auth.assertion(**kw), self.lst if lst == "default" else lst,
                                        now=now)

    def assert_refused(self, res, reason, disable):
        self.assertEqual((res.ok, res.reason, res.counts_as, res.disable), (False, reason, None, disable), res)
        self.assertEqual(res.alert, disable or reason == "bs_without_be")

    def test_hardware_alone(self):
        res = self.check(self.stored(), counter=6)
        self.assertEqual((res.ok, res.counts_as, res.sign_count, res.disable, res.alert), (True, "hardware", 6, False, False))

    def test_synced_is_one_factor(self):
        res = self.check(self.stored("synced", be=True, count=0), counter=0, be=True, bs=True)
        self.assertEqual((res.ok, res.counts_as, res.backed_up), (True, "one_factor", True))

    def test_uv_missing(self):
        self.assert_refused(self.check(self.stored(), counter=6, uv=False), "uv_missing", False)

    def test_bs_without_be(self):
        self.assert_refused(self.check(self.stored(), counter=6, bs=True), "bs_without_be", False)

    def test_be_change_disables(self):
        self.assert_refused(self.check(self.stored(be=False), counter=6, be=True), "be_changed", True)
        self.assert_refused(self.check(self.stored("synced", be=True, count=0), counter=0, be=False), "be_changed", True)

    def test_counter(self):
        for stored, new, ok in ((5, 6, True), (5, 5, False), (5, 4, False), (5, 0, False), (0, 0, True), (0, 3, True)):
            with self.subTest(stored=stored, new=new):
                res = self.check(self.stored(count=stored), counter=new)
                if ok:
                    self.assertTrue(res.ok)
                    self.assertEqual(res.sign_count, new)
                else:
                    self.assert_refused(res, "counter_not_increased", True)

    def test_synced_counter_zero_passes(self):
        self.assertTrue(self.check(self.stored("synced", be=True, count=0), counter=0, be=True).ok)

    def test_model_revoked_after_registration(self):
        for status in ("REVOKED", "USER_KEY_PHYSICAL_COMPROMISE"):
            with self.subTest(status=status):
                lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID), statuses=("FIDO_CERTIFIED_L1", status))])
                res = self.check(self.stored(), lst=lst, counter=6)
                self.assert_refused(res, "model_not_usable", True)
                self.assertIn("remove it", res.message)

    def test_model_gone_from_list(self):
        self.assert_refused(self.check(self.stored(), lst=mds_list([]), counter=6), "model_not_usable", True)

    def test_synced_ignores_the_list(self):
        self.assertTrue(self.check(self.stored("synced", be=True), lst=mds_list([]), counter=6, be=True).ok)

    def test_old_list_needs_second_factor(self):
        res = self.check(self.stored(), now=NOW + timedelta(days=31), counter=6)
        self.assertEqual((res.ok, res.counts_as, res.reason), (True, "one_factor", "mds_too_old"))
        res = self.check(self.stored(), lst=None, counter=6)
        self.assertEqual((res.ok, res.counts_as, res.reason), (True, "one_factor", "mds_too_old"))

    def test_revoked_model_refused_even_on_an_old_list(self):
        lst = mds_list([kit.entry(aaguid=kit.uuid_str(AAGUID), statuses=("REVOKED",))])
        self.assert_refused(self.check(self.stored(), lst=lst, now=NOW + timedelta(days=60), counter=6),
                            "model_not_usable", True)

    def test_zero_aaguid_by_key_id(self):
        key_id = b"\x33" * 20
        lst = mds_list([kit.entry(key_ids=[key_id])])
        self.assertEqual(self.check(self.stored(aaguid=ZERO, key_id=key_id.hex()), lst=lst, counter=6).counts_as,
                         "hardware")
        self.assert_refused(self.check(self.stored(aaguid=ZERO), lst=lst, counter=6), "model_not_usable", True)


if __name__ == "__main__":
    unittest.main()
