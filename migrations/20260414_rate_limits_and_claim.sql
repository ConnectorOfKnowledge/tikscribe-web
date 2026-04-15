-- Rate-limit buckets + RPC helpers + claim function for Step 2 webhook flow.
-- Safe to re-run; guarded with IF NOT EXISTS / CREATE OR REPLACE.

BEGIN;

-- =========================================================================
-- 1. rate_limits table: per-key per-minute buckets
-- =========================================================================

CREATE TABLE IF NOT EXISTS rate_limits (
  key    text        NOT NULL,
  bucket timestamptz NOT NULL,
  count  int         NOT NULL DEFAULT 0,
  PRIMARY KEY (key, bucket)
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_bucket ON rate_limits(bucket);

-- =========================================================================
-- 2. rate_limit_check(key, window_minutes, max_count)
--    Bumps the current minute's bucket and returns whether the rolling
--    sum over the last `window_minutes` is within `max_count`.
-- =========================================================================

CREATE OR REPLACE FUNCTION rate_limit_check(
  p_key text,
  p_window_minutes int,
  p_max_count int
)
RETURNS TABLE (allowed boolean, current_count int)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_bucket timestamptz := date_trunc('minute', now());
  v_total  int;
BEGIN
  INSERT INTO rate_limits (key, bucket, count)
  VALUES (p_key, v_bucket, 1)
  ON CONFLICT (key, bucket) DO UPDATE
    SET count = rate_limits.count + 1;

  SELECT COALESCE(SUM(count), 0) INTO v_total
  FROM rate_limits
  WHERE key = p_key
    AND bucket > now() - (p_window_minutes || ' minutes')::interval;

  RETURN QUERY SELECT (v_total <= p_max_count), v_total;
END;
$$;

REVOKE ALL ON FUNCTION rate_limit_check(text, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rate_limit_check(text, int, int) TO service_role;

-- =========================================================================
-- 3. daily_submission_count()
--    Global count of rows inserted since start-of-day (UTC). Used by the
--    /api/transcribe circuit breaker to hard-cap at 500 rows/day.
-- =========================================================================

CREATE OR REPLACE FUNCTION daily_submission_count()
RETURNS int
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COUNT(*)::int
  FROM transcripts
  WHERE created_at >= date_trunc('day', now());
$$;

REVOKE ALL ON FUNCTION daily_submission_count() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION daily_submission_count() TO service_role;

-- =========================================================================
-- 4. claim_transcript_for_processing(id)
--    Atomic claim used by the edge function. Sets status='processing' and
--    bumps processing_attempts only if the row is eligible:
--      - status in ('queued','processing')
--      - (retry_count + processing_attempts) < 3
--      - visual_status != 'legacy_skip'
--    Returns the row if claimed, empty otherwise.
-- =========================================================================

CREATE OR REPLACE FUNCTION claim_transcript_for_processing(p_id uuid)
RETURNS TABLE (id uuid, url text, retry_count int, processing_attempts int)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- Only queued rows are claimable. 'processing' is exclusive-ownership
  -- held by whichever pipeline won the previous claim. If stranded, the
  -- release_stuck_transcript_rows() job moves it back to 'queued' before
  -- anyone can re-claim it. This prevents the two-pipelines collision
  -- that would otherwise happen at 06:00 UTC when the legacy cron fires.
  RETURN QUERY
    UPDATE transcripts t
    SET status              = 'processing',
        processing_attempts = COALESCE(t.processing_attempts, 0) + 1,
        updated_at          = now()
    WHERE t.id = p_id
      AND t.status = 'queued'
      AND (COALESCE(t.retry_count, 0) + COALESCE(t.processing_attempts, 0)) < 3
      AND t.visual_status <> 'legacy_skip'
    RETURNING t.id, t.url, t.retry_count, t.processing_attempts;
END;
$$;

REVOKE ALL ON FUNCTION claim_transcript_for_processing(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_transcript_for_processing(uuid) TO service_role;

-- =========================================================================
-- 5. claim_visual_for_processing(id)
--    Same idea for /api/analyze_visual. Prevents concurrent Gemini calls
--    on the same record.
-- =========================================================================

CREATE OR REPLACE FUNCTION claim_visual_for_processing(p_id uuid)
RETURNS TABLE (id uuid, direct_url text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- Eligible: pending (fresh), failed (retry), deferred (legacy processor
  -- completed transcript but ran out of time for Gemini). 'legacy_skip' is
  -- explicitly excluded by the status list, but kept in the guard for
  -- defence-in-depth in case the list is ever widened.
  RETURN QUERY
    UPDATE transcripts t
    SET visual_status = 'processing',
        updated_at    = now()
    WHERE t.id = p_id
      AND t.visual_status IN ('pending', 'failed', 'deferred')
      AND t.visual_status <> 'legacy_skip'
    RETURNING t.id, t.direct_url;
END;
$$;

REVOKE ALL ON FUNCTION claim_visual_for_processing(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_visual_for_processing(uuid) TO service_role;

-- =========================================================================
-- 6a. Release stuck visual_status='processing' rows
--    If /api/analyze_visual crashes after the claim (Gemini hang, Vercel
--    function timeout, etc.), the row is stranded. Reset to 'failed' so
--    the next retry can pick it up.
-- =========================================================================

CREATE OR REPLACE FUNCTION release_stuck_visual_rows()
RETURNS int
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  WITH released AS (
    UPDATE transcripts
    SET visual_status = 'failed',
        last_error    = coalesce(last_error, 'stuck in visual processing'),
        updated_at    = now()
    WHERE visual_status = 'processing'
      AND updated_at < now() - interval '15 minutes'
    RETURNING 1
  )
  SELECT COUNT(*)::int FROM released;
$$;

REVOKE ALL ON FUNCTION release_stuck_visual_rows() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION release_stuck_visual_rows() TO service_role;

-- =========================================================================
-- 6b. Release stuck status='processing' transcript rows.
--    If attempts are already exhausted, mark failed; otherwise requeue so
--    the next safety-net tick (or legacy cron) can pick it up. This is the
--    other half of the C2 fix: 'processing' must be a terminal state for a
--    single worker, or stranded rows block forever.
-- =========================================================================

CREATE OR REPLACE FUNCTION release_stuck_transcript_rows()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  requeued int := 0;
  failed   int := 0;
BEGIN
  WITH to_fail AS (
    UPDATE transcripts
    SET status     = 'failed',
        last_error = coalesce(last_error, 'stuck in processing, attempts exhausted'),
        updated_at = now()
    WHERE status = 'processing'
      AND updated_at < now() - interval '15 minutes'
      AND (coalesce(retry_count, 0) + coalesce(processing_attempts, 0)) >= 3
    RETURNING 1
  )
  SELECT COUNT(*)::int INTO failed FROM to_fail;

  WITH to_requeue AS (
    UPDATE transcripts
    SET status     = 'queued',
        last_error = coalesce(last_error, 'stuck in processing, requeued'),
        updated_at = now()
    WHERE status = 'processing'
      AND updated_at < now() - interval '15 minutes'
      AND (coalesce(retry_count, 0) + coalesce(processing_attempts, 0)) < 3
    RETURNING 1
  )
  SELECT COUNT(*)::int INTO requeued FROM to_requeue;

  RETURN requeued + failed;
END;
$$;

REVOKE ALL ON FUNCTION release_stuck_transcript_rows() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION release_stuck_transcript_rows() TO service_role;

-- =========================================================================
-- 7. Daily sweep of stale rate_limits rows (> 1 day old) -- scheduled below
-- =========================================================================

COMMIT;

-- Run ONCE, outside the transaction, to install the cleanup + safety-net jobs:
--
--   SELECT cron.schedule(
--     'tikscribe-rate-limits-sweep',
--     '17 3 * * *',
--     $$ DELETE FROM rate_limits WHERE bucket < now() - interval '1 day'; $$
--   );
--
--   SELECT cron.schedule(
--     'tikscribe-visual-release',
--     '*/10 * * * *',
--     $$ SELECT release_stuck_visual_rows(); $$
--   );
--
--   SELECT cron.schedule(
--     'tikscribe-transcript-release',
--     '*/10 * * * *',
--     $$ SELECT release_stuck_transcript_rows(); $$
--   );
--
--   -- Before scheduling the safety net, store the service role key in Vault:
--   --   SELECT vault.create_secret('<SERVICE_ROLE_KEY>', 'tikscribe_service_role_key');
--
--   SELECT cron.schedule(
--     'tikscribe-safety-net',
--     '*/30 * * * *',
--     $$
--     SELECT net.http_post(
--       url := 'https://dgnikbbugiuuwokwenlm.functions.supabase.co/process-transcript',
--       headers := jsonb_build_object(
--         'Content-Type',  'application/json',
--         'Authorization', 'Bearer ' || (
--           SELECT decrypted_secret FROM vault.decrypted_secrets
--           WHERE name = 'tikscribe_service_role_key' LIMIT 1
--         )
--       ),
--       body := jsonb_build_object(
--         'source', 'safety_net',
--         'record', jsonb_build_object('id', id)
--       )
--     )
--     FROM transcripts
--     WHERE (
--       (status = 'queued'     AND created_at < now() - interval '5 minutes')
--       OR
--       (status = 'processing' AND updated_at < now() - interval '15 minutes')
--     )
--       AND (coalesce(retry_count, 0) + coalesce(processing_attempts, 0)) < 3
--       AND visual_status <> 'legacy_skip'
--     LIMIT 20;
--     $$
--   );
