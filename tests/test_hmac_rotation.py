"""Unit tests for HMAC dual-secret verification during rotation."""

import unittest
from api._hmac import sign, verify_with_rotation


OLD = "a" * 64
NEW = "b" * 64


class RotationTests(unittest.TestCase):
    def test_accepts_signature_from_primary(self):
        ts, sig = sign(NEW, b"hello", timestamp=1_700_000_000)
        self.assertTrue(verify_with_rotation((NEW, OLD), b"hello", ts, sig, now=ts))

    def test_accepts_signature_from_secondary(self):
        ts, sig = sign(OLD, b"hello", timestamp=1_700_000_000)
        self.assertTrue(verify_with_rotation((NEW, OLD), b"hello", ts, sig, now=ts))

    def test_rejects_signature_from_neither(self):
        ts, sig = sign("c" * 64, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify_with_rotation((NEW, OLD), b"hello", ts, sig, now=ts))

    def test_rejects_when_all_secrets_empty(self):
        ts, sig = sign(NEW, b"hello", timestamp=1_700_000_000)
        self.assertFalse(verify_with_rotation(("", ""), b"hello", ts, sig, now=ts))

    def test_skips_empty_entries(self):
        ts, sig = sign(OLD, b"hello", timestamp=1_700_000_000)
        # Primary is empty string (env var unset), secondary is the real one
        self.assertTrue(verify_with_rotation(("", OLD), b"hello", ts, sig, now=ts))

    def test_still_enforces_replay_window(self):
        ts, sig = sign(OLD, b"hello", timestamp=1_700_000_000)
        self.assertFalse(
            verify_with_rotation((NEW, OLD), b"hello", ts, sig, now=1_700_000_500)
        )


if __name__ == "__main__":
    unittest.main()
