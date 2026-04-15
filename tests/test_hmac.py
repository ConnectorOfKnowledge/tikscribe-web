"""Unit tests for HMAC signing / verification.

Run from project root:  python -m unittest tests.test_hmac
"""

import unittest
from api._hmac import sign, verify, WINDOW_SECONDS


SECRET = "a" * 64  # 32-byte hex placeholder


class SignTests(unittest.TestCase):
    def test_returns_int_timestamp_and_hex_digest(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertEqual(ts, 1_700_000_000)
        self.assertEqual(len(sig), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in sig))

    def test_deterministic_for_same_inputs(self):
        _, sig_a = sign(SECRET, b"body", timestamp=42)
        _, sig_b = sign(SECRET, b"body", timestamp=42)
        self.assertEqual(sig_a, sig_b)

    def test_different_body_different_signature(self):
        _, sig_a = sign(SECRET, b"a", timestamp=42)
        _, sig_b = sign(SECRET, b"b", timestamp=42)
        self.assertNotEqual(sig_a, sig_b)

    def test_different_timestamp_different_signature(self):
        _, sig_a = sign(SECRET, b"body", timestamp=42)
        _, sig_b = sign(SECRET, b"body", timestamp=43)
        self.assertNotEqual(sig_a, sig_b)

    def test_empty_secret_raises(self):
        with self.assertRaises(ValueError):
            sign("", b"body", timestamp=42)


class VerifyTests(unittest.TestCase):
    def test_accepts_valid_signature(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertTrue(verify(SECRET, b"hello", ts, sig, now=1_700_000_000))

    def test_accepts_within_window(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertTrue(verify(SECRET, b"hello", ts, sig, now=1_700_000_000 + 100))

    def test_rejects_tampered_body(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify(SECRET, b"goodbye", ts, sig, now=1_700_000_000))

    def test_rejects_wrong_secret(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify("b" * 64, b"hello", ts, sig, now=1_700_000_000))

    def test_rejects_stale_timestamp(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(
            verify(SECRET, b"hello", ts, sig, now=1_700_000_000 + WINDOW_SECONDS + 1)
        )

    def test_rejects_future_timestamp(self):
        ts, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(
            verify(SECRET, b"hello", ts, sig, now=1_700_000_000 - WINDOW_SECONDS - 1)
        )

    def test_rejects_empty_signature(self):
        self.assertFalse(verify(SECRET, b"hello", 1_700_000_000, "", now=1_700_000_000))

    def test_rejects_empty_secret(self):
        _, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify("", b"hello", 1_700_000_000, sig, now=1_700_000_000))

    def test_rejects_non_int_timestamp(self):
        _, sig = sign(SECRET, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify(SECRET, b"hello", "1700000000", sig, now=1_700_000_000))


if __name__ == "__main__":
    unittest.main()
