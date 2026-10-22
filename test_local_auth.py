#!/usr/bin/env python3
"""Tests for dashboard/auth's local factors (phase 6a W6, the pure part; docs/phase6a_plan.md
§3.6, design §12's auth list): Argon2id round trip, parameter floor and rehash; the password
rule; TOTP against the RFC 6238 / RFC 4226 vectors, the ±1 window and replay of a used step;
recovery codes single-use and peppered; the factor rule table with remembered device and
step-up; the rate limits and their backoff.

Needs the project venv (cryptography; Python 3.12):
    .venv/bin/python -m unittest test_local_auth
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Like test_dashboard: under another interpreter every test here is skipped (with the reason), so a
# combined `python3 -m unittest discover` stays green.
SKIP = ("needs Python 3.12 with cryptography (not {}): run with .venv/bin/python -m unittest "
        "test_local_auth".format(sys.executable)
        if sys.version_info < (3, 12) or importlib.util.find_spec("cryptography") is None else "")

if not SKIP:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    from dashboard.auth import factors, passwords, ratelimit, recovery, totp

GOOD = "correct horse battery"


def _phc(password, memory_kib=64 * 1024, iterations=3, lanes=4, salt=b"s" * 16, length=32):
    return Argon2id(salt=salt, length=length, iterations=iterations, lanes=lanes,
                    memory_cost=memory_kib).derive_phc_encoded(password.encode("utf-8"))


@unittest.skipIf(SKIP, SKIP)
class PasswordTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.phc = passwords.hash_password(GOOD)

    def test_round_trip(self):
        self.assertTrue(self.phc.startswith("$argon2id$v=19$m=65536,t=3,p=4$"))
        self.assertTrue(passwords.verify_password(GOOD, self.phc))
        self.assertFalse(passwords.verify_password(GOOD + "!", self.phc))
        self.assertNotEqual(self.phc, passwords.hash_password(GOOD))   # fresh salt

    def test_nfkc(self):
        fullwidth = "".join(chr(ord(c) + 0xFEE0) if c != " " else "　" for c in GOOD)
        self.assertTrue(passwords.verify_password(fullwidth, self.phc))

    def test_rule(self):
        for pw, code in (("a" * 11, "too_short"), ("a" * 129, "too_long"), ("ａ" * 11, "too_short")):
            with self.assertRaises(passwords.PasswordRuleError) as cm:
                passwords.check_rule(pw)
            self.assertEqual(cm.exception.code, code)
            with self.assertRaises(passwords.PasswordRuleError):
                passwords.hash_password(pw)
        for pw in ("a" * 12, "a" * 128, "lowercaseonly"):   # no composition rules
            passwords.check_rule(pw)

    def test_old_password_outside_rule_still_verifies(self):
        self.assertTrue(passwords.verify_password("short", _phc("short")))

    def test_floor(self):
        with self.assertRaises(ValueError):
            passwords.hash_password(GOOD, passwords.Params(32 * 1024, 3, 4))
        for weak in (_phc(GOOD, memory_kib=8, iterations=1, lanes=1), _phc(GOOD, iterations=2),
                     _phc(GOOD, lanes=2), _phc(GOOD, salt=b"s" * 8), _phc(GOOD, length=16)):
            with self.assertRaises(passwords.StoredHashError):   # even with the right password
                passwords.verify_password(GOOD, weak)

    def test_malformed_stored_hash(self):
        bad = ("", "garbage", self.phc.replace("argon2id", "argon2i"),
               self.phc.replace("v=19", "v=16"), self.phc[:-5] + "!!!!!", self.phc + "$x")
        for phc in bad:
            with self.assertRaises(passwords.StoredHashError, msg=phc):
                passwords.verify_password(GOOD, phc)

    def test_needs_rehash(self):
        self.assertFalse(passwords.needs_rehash(self.phc))
        self.assertTrue(passwords.needs_rehash(_phc(GOOD, iterations=4)))
        self.assertTrue(passwords.needs_rehash(self.phc, passwords.Params(64 * 1024, 4, 4)))
        self.assertTrue(passwords.needs_rehash(_phc(GOOD, length=64)))
        stronger = passwords.hash_password(GOOD, passwords.Params(64 * 1024, 4, 4))
        self.assertTrue(passwords.verify_password(GOOD, stronger))
        self.assertTrue(passwords.needs_rehash(stronger))

    def test_dummy_uses_current_params(self):
        self.assertFalse(passwords.verify_dummy(GOOD))
        with mock.patch.object(passwords, "verify_password", wraps=passwords.verify_password) as v:
            passwords.verify_dummy(GOOD)
        self.assertEqual(passwords.parse(v.call_args.args[1]).params, passwords.PARAMS)


@unittest.skipIf(SKIP, SKIP)
class TotpTest(unittest.TestCase):
    RFC_SEED = b"12345678901234567890"

    def test_rfc6238_vectors(self):
        # RFC 6238 Appendix B, SHA-1 column (8 digits)
        for t, code in ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                        (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")):
            self.assertEqual(totp.code_at(self.RFC_SEED, totp.step_at(t), digits=8), code)
            self.assertEqual(totp.code_at(self.RFC_SEED, totp.step_at(t)), code[-6:])

    def test_rfc4226_vectors(self):
        # RFC 4226 Appendix D: HOTP(counter) = TOTP at step = counter
        codes = ["755224", "287082", "359152", "969429", "338314",
                 "254676", "287922", "162583", "399871", "520489"]
        for counter, code in enumerate(codes):
            self.assertEqual(totp.code_at(self.RFC_SEED, counter), code)

    def test_window(self):
        seed = totp.new_seed()
        self.assertEqual(len(seed), 20)
        now = 1_800_000_015.0
        s = totp.step_at(now)
        for d in (-1, 0, 1):
            self.assertEqual(totp.verify(seed, totp.code_at(seed, s + d), now, None), s + d)
        for d in (-2, 2):
            self.assertIsNone(totp.verify(seed, totp.code_at(seed, s + d), now, None))

    def test_replay(self):
        seed = totp.new_seed()
        now = 1_800_000_015.0
        s = totp.step_at(now)
        code = totp.code_at(seed, s)
        used = totp.verify(seed, code, now, None)
        self.assertEqual(used, s)
        self.assertIsNone(totp.verify(seed, code, now, used))                      # same code
        self.assertIsNone(totp.verify(seed, code, now + 20, used))                 # still in window
        self.assertIsNone(totp.verify(seed, totp.code_at(seed, s - 1), now, used))  # older step
        self.assertEqual(totp.verify(seed, totp.code_at(seed, s + 1), now, used), s + 1)

    def test_code_format(self):
        seed = totp.new_seed()
        now = 1_800_000_015.0
        code = totp.code_at(seed, totp.step_at(now))
        self.assertEqual(totp.verify(seed, f" {code[:3]} {code[3:]} ", now, None), totp.step_at(now))
        fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in code)
        for bad in ("", code[:5], code + "0", "abcdef", fullwidth, code[:5] + "\n"):
            self.assertIsNone(totp.verify(seed, bad, now, None), bad)

    def test_seed_length(self):
        with self.assertRaises(ValueError):
            totp.code_at(b"x" * 19, 1)

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri(self.RFC_SEED, "alice")
        self.assertTrue(uri.startswith("otpauth://totp/"), uri)
        self.assertIn("secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", uri)
        self.assertIn("issuer=AFClaude", uri)


@unittest.skipIf(SKIP, SKIP)
class RecoveryTest(unittest.TestCase):

    def setUp(self):
        self.pepper = recovery.new_pepper()

    def test_generate(self):
        codes = recovery.generate()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for c in codes:
            self.assertRegex(c, r"^[A-Z2-7]{4}(-[A-Z2-7]{4}){3}$")

    def test_single_use(self):
        codes = recovery.generate()
        stored = [recovery.StoredCode(recovery.hash_code(self.pepper, c), False) for c in codes]
        i = recovery.find(self.pepper, codes[3], stored)
        self.assertEqual(i, 3)
        stored[3] = recovery.StoredCode(stored[3].code_hash, True)
        self.assertIsNone(recovery.find(self.pepper, codes[3], stored))
        self.assertEqual(recovery.find(self.pepper, codes[4], stored), 4)

    def test_wrong_and_malformed(self):
        codes = recovery.generate()
        stored = [recovery.StoredCode(recovery.hash_code(self.pepper, c), False) for c in codes]
        other = recovery.generate(1)[0]
        for bad in (other, "", "ABCD", codes[0] + "A", codes[0].replace(codes[0][0], "8"), "x" * 40):
            if bad in codes:
                continue
            self.assertIsNone(recovery.find(self.pepper, bad, stored), bad)
        self.assertIsNone(recovery.find(recovery.new_pepper(), codes[0], stored))   # another pepper

    def test_forgiving_input(self):
        code = "OIAB-CDEF-GHIJ-KLMN"
        stored = [recovery.StoredCode(recovery.hash_code(self.pepper, code), False)]
        for typed in ("oiab cdef ghij klmn", "01ABCDEFGHIJKLMN", " OIAB-CDEF-GHIJ-KLMN "):
            self.assertEqual(recovery.find(self.pepper, typed, stored), 0, typed)

    def test_only_hash_stored_and_pepper_length(self):
        code = recovery.generate(1)[0]
        h = recovery.hash_code(self.pepper, code)
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertNotIn(code.replace("-", ""), h.upper())
        with self.assertRaises(ValueError):
            recovery.hash_code(b"p" * 31, code)
        with self.assertRaises(ValueError):
            recovery.hash_code(self.pepper, "nope")

    def test_compares_every_row(self):
        codes = recovery.generate()
        stored = [recovery.StoredCode(recovery.hash_code(self.pepper, c), False) for c in codes]
        with mock.patch.object(recovery.hmac, "compare_digest",
                               wraps=recovery.hmac.compare_digest) as cmp:
            recovery.find(self.pepper, codes[0], stored)
        self.assertEqual(cmp.call_count, len(stored))


@unittest.skipIf(SKIP, SKIP)
class FactorRuleTest(unittest.TestCase):

    def test_table(self):
        table = [
            ((), False), (("password",), False), (("totp",), False), (("recovery_code",), False),
            (("password", "password"), False),
            (("password", "totp"), True), (("password", "recovery_code"), True),
            (("totp", "recovery_code"), True), (("password", "totp", "recovery_code"), True),
        ]
        for done, complete in table:
            self.assertEqual(factors.is_complete(done), complete, done)
            self.assertEqual(factors.is_complete(done, step_up=True), complete, done)

    def test_remembered_device_skips_only_the_second_factor(self):
        self.assertTrue(factors.is_complete(["password"], remembered_device=True))
        for done in ((), ("totp",), ("recovery_code",)):
            self.assertFalse(factors.is_complete(done, remembered_device=True), done)

    def test_remembered_device_never_step_up(self):
        self.assertFalse(factors.is_complete(["password"], remembered_device=True, step_up=True))
        self.assertTrue(factors.is_complete(["password", "totp"], remembered_device=True, step_up=True))

    def test_unknown_factor(self):
        with self.assertRaises(ValueError):
            factors.is_complete(["password", "sms"])


@unittest.skipIf(SKIP, SKIP)
class RateLimitTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_account_limit_and_backoff(self):
        rule = ratelimit.ACCOUNT
        fails = [self.NOW - 100 + i for i in range(4)]
        self.assertEqual(ratelimit.retry_after(fails, self.NOW, rule), 0)
        fails.append(self.NOW)
        self.assertEqual(ratelimit.retry_after(fails, self.NOW, rule), 60)
        self.assertEqual(ratelimit.retry_after(fails, self.NOW + 59, rule), 1)
        self.assertEqual(ratelimit.retry_after(fails, self.NOW + 60, rule), 0)
        fails.append(self.NOW + 60)
        self.assertEqual(ratelimit.retry_after(fails, self.NOW + 60, rule), 120)
        fails += [self.NOW + 61 + i for i in range(10)]
        self.assertEqual(ratelimit.retry_after(fails, self.NOW + 70, rule), 3600 - 0)

    def test_window_ages_out(self):
        rule = ratelimit.ACCOUNT
        fails = [self.NOW - 901 + i for i in range(5)]   # all older than 15 min at NOW + 5
        self.assertEqual(ratelimit.retry_after(fails, self.NOW + 5, rule), 0)

    def test_backoff_grows_over_the_horizon(self):
        rule = ratelimit.ACCOUNT
        earlier = [self.NOW - 3 * 3600 + i for i in range(5)]   # a limit hit 3 h ago
        burst = [self.NOW - 4 + i for i in range(5)]
        self.assertEqual(ratelimit.retry_after(earlier + burst, self.NOW, rule), 60 * 2 ** 5)
        older = [self.NOW - 25 * 3600 + i for i in range(5)]    # beyond the 24-h horizon
        self.assertEqual(ratelimit.retry_after(older + burst, self.NOW, rule), 60)

    def test_ip_rule(self):
        fails = [self.NOW - 30 + i for i in range(19)]
        self.assertEqual(ratelimit.retry_after(fails, self.NOW, ratelimit.IP), 0)
        self.assertEqual(ratelimit.retry_after(fails + [self.NOW], self.NOW, ratelimit.IP), 60)

    def test_future_failure_counts(self):
        fails = [self.NOW + 5] * 5
        self.assertEqual(ratelimit.retry_after(fails, self.NOW, ratelimit.ACCOUNT), 65)

    def test_huge_count_is_capped(self):
        fails = [self.NOW - i for i in range(5000)]
        self.assertEqual(ratelimit.retry_after(fails, self.NOW, ratelimit.ACCOUNT), 3600)

    def test_reached_limit(self):
        rule = ratelimit.ACCOUNT
        fails = []
        hits = []
        for i in range(7):
            fails.append(self.NOW + i)
            hits.append(ratelimit.reached_limit(fails, self.NOW + i, rule))
        self.assertEqual(hits, [False, False, False, False, True, False, False])


if __name__ == "__main__":
    unittest.main()
