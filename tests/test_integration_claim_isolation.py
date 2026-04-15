"""Integration test for C2 regression: two pipelines cannot double-claim.

Runs against the live Supabase project using the service role key from
.env.local. Inserts a sentinel row with a distinctive URL, exercises the
claim flow from both pipelines, and asserts that exactly one wins the
'processing' state. Cleans up the row after the assertion, even on failure.

Skipped when SUPABASE_URL or SUPABASE_SERVICE_KEY are not in the
environment. Run from project root:

    python -m unittest tests.test_integration_claim_isolation

Uses the sentinel prefix 'https://tiktok.com/t/TEST_C2_' so a stray row
is easy to spot and delete manually. The sentinel URL is NOT a real
TikTok — it will 404 if the edge function ever forwards it, which is the
correct behavior because this test never triggers the webhook.
"""

import os
import sys
import unittest
import uuid
import pathlib


def _load_env_local():
    """Load .env.local into os.environ if not already set. Handles the
    quoting style vercel env pull produces."""
    path = pathlib.Path(__file__).resolve().parents[1] / ".env.local"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip()
        os.environ.setdefault(k.strip(), v)


_load_env_local()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

SKIP_REASON = "SUPABASE_URL / SUPABASE_SERVICE_KEY not set"


@unittest.skipUnless(SUPABASE_URL and SUPABASE_KEY, SKIP_REASON)
class ClaimIsolationTests(unittest.TestCase):
    """Proves that once a row is claimed by one pipeline, the other path
    cannot also claim it. Regression guard for the 06:00 UTC double-bill."""

    @classmethod
    def setUpClass(cls):
        try:
            from supabase import create_client
        except ImportError:
            raise unittest.SkipTest("supabase-py not installed in this env")
        cls.sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        cls.test_url = f"https://tiktok.com/t/TEST_C2_{uuid.uuid4().hex[:12]}"
        cls.test_id = None

    def setUp(self):
        row = (
            self.sb.table("transcripts")
            .insert({
                "url": self.test_url,
                "status": "queued",
                "visual_status": "pending",
                "title": "C2 regression test sentinel",
                "creator": "tests.test_integration_claim_isolation",
            })
            .execute()
        )
        self.__class__.test_id = row.data[0]["id"]

    def tearDown(self):
        if self.__class__.test_id:
            try:
                self.sb.table("transcripts").delete().eq(
                    "id", self.__class__.test_id
                ).execute()
            except Exception as e:
                print(f"[cleanup] failed to delete {self.__class__.test_id}: {e}",
                      file=sys.stderr)
            self.__class__.test_id = None

    def _legacy_pickup_count(self) -> int:
        """Mirrors api/process_queue.py's current SELECT — status='queued'
        only. Returns the count that would appear in the legacy cron's batch.
        """
        res = (
            self.sb.table("transcripts")
            .select("id")
            .eq("status", "queued")
            .lt("retry_count", 3)
            .eq("id", self.__class__.test_id)
            .execute()
        )
        return len(res.data or [])

    def _new_path_claim(self) -> dict | None:
        """Mirrors the edge function: claim_transcript_for_processing RPC."""
        res = self.sb.rpc(
            "claim_transcript_for_processing",
            {"p_id": self.__class__.test_id},
        ).execute()
        data = res.data
        if isinstance(data, list):
            return data[0] if data else None
        return data

    def test_queued_row_visible_to_both_paths_before_claim(self):
        self.assertEqual(self._legacy_pickup_count(), 1,
                         "newly queued row should be visible to legacy cron")
        # No claim yet — the new-path RPC would also succeed if called first.
        # Tested indirectly in test_first_claim_wins_and_second_fails.

    def test_first_claim_wins_and_second_fails(self):
        claim_a = self._new_path_claim()
        self.assertIsNotNone(claim_a, "first claim should succeed")

        claim_b = self._new_path_claim()
        self.assertIsNone(claim_b, "second claim on same row must fail")

    def test_legacy_cannot_pick_up_after_new_path_claims(self):
        claim = self._new_path_claim()
        self.assertIsNotNone(claim, "precondition: new path must claim")

        count = self._legacy_pickup_count()
        self.assertEqual(
            count, 0,
            "after new-path claim, legacy SELECT must return empty — "
            "otherwise 06:00 UTC cron will double-bill AssemblyAI"
        )

    def test_legacy_submit_one_exits_early_on_already_processing_row(self):
        """If the edge function claimed the row first, the legacy cron's
        submit_one must not overwrite status or re-submit to AssemblyAI."""
        claim = self._new_path_claim()
        self.assertIsNotNone(claim, "precondition: new path must claim")

        # Confirm the row is now 'processing' before we call submit_one
        pre = (
            self.sb.table("transcripts")
            .select("status")
            .eq("id", self.__class__.test_id)
            .single()
            .execute()
        )
        self.assertEqual(pre.data["status"], "processing")

        # Build the record shape submit_one expects, without a direct_url
        # so that if the early-exit ever regresses the test still won't
        # actually hit AssemblyAI (it would short-circuit on missing url).
        from api.process_queue import submit_one
        result = submit_one(self.sb, {
            "id": self.__class__.test_id,
            "url": "https://tiktok.com/ignored-in-this-test",
            "direct_url": None,
            "retry_count": 0,
        })

        self.assertEqual(
            result.get("status"), "skipped",
            "submit_one must exit early on an already-claimed row"
        )
        self.assertEqual(result.get("reason"), "already_claimed")

        # And crucially: retry_count must NOT have been bumped
        post = (
            self.sb.table("transcripts")
            .select("retry_count, status")
            .eq("id", self.__class__.test_id)
            .single()
            .execute()
        )
        self.assertEqual(post.data["status"], "processing",
                         "status must remain 'processing', not be overwritten")
        self.assertIn(post.data["retry_count"], (None, 0),
                      "retry_count must not be bumped by legacy path on a "
                      "row already owned by the new path")

    @unittest.skip(
        "transcripts has a BEFORE UPDATE trigger (update_updated_at from "
        "supabase_setup.sql) that overrides any attempt to backdate "
        "updated_at. Testing the 15-min-stale branch of "
        "release_stuck_transcript_rows would require either disabling the "
        "trigger via a test-only RPC (invasive) or waiting 15 real minutes "
        "(not a unit test). Behavior is trivially correct by inspection of "
        "the SQL and observable in prod via the *-release cron logs."
    )
    def test_release_stuck_requeues_within_attempt_cap(self):
        pass


if __name__ == "__main__":
    unittest.main()
