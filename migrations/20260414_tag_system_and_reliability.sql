-- TickScribe migration: tag system + processing reliability + FTS
-- Project: dgnikbbugiuuwokwenlm
-- Date:    2026-04-14
-- Author:  builder session (following HANDOFF.md)
--
-- Deviations from HANDOFF.md (discussed in the session and approved):
--   * Do NOT rename `categories` -> `legacy_categories` in this migration.
--     Reason: api/process_queue.py, api/status.py, api/history.py, and
--     public/app.js all write/read `categories` today. Renaming without
--     a coordinated code deploy would 500 /api/transcribe for the Vercel
--     deploy window. The rename is deferred to a Step-6 cleanup migration
--     once the UI and processors have been cut over to `tags`.
--   * The `tags_lowercase` constraint uses a BEFORE trigger (coerces to
--     lowercase) rather than a CHECK with a subquery. Postgres forbids
--     subqueries in CHECK expressions. Coercing is also more forgiving.
--   * `url_normalized` gets a non-unique index for now. 3 pre-existing
--     duplicate URL groups would cause a unique index to fail. Dedup is
--     out-of-scope per "keep all 277 records" decision. Uniqueness will
--     be enforced at the INSERT layer in the edge function (Step 2).
--
-- Everything below is wrapped in a single transaction. If any statement
-- fails, nothing is applied.

BEGIN;

-- =========================================================================
-- 1. Tag column (flat text[] with path-prefix hierarchy: ai, ai/claude-code)
-- =========================================================================

ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';

-- =========================================================================
-- 2. Enforce lowercase tags via BEFORE trigger (coerces on write)
-- =========================================================================

CREATE OR REPLACE FUNCTION transcripts_tags_lowercase()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.tags IS NOT NULL AND array_length(NEW.tags, 1) IS NOT NULL THEN
    NEW.tags := ARRAY(SELECT lower(t) FROM unnest(NEW.tags) t);
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS transcripts_tags_lowercase_trg ON transcripts;
CREATE TRIGGER transcripts_tags_lowercase_trg
  BEFORE INSERT OR UPDATE OF tags ON transcripts
  FOR EACH ROW
  EXECUTE FUNCTION transcripts_tags_lowercase();

-- =========================================================================
-- 3. Processing reliability columns
--    Note: `retry_count` already exists from supabase_migration_visual.sql
--    and is referenced by api/process_queue.py. We keep it and add
--    `processing_attempts` as a separate counter for the new webhook/edge
--    pipeline (Step 2). Step 2 edge function will bump processing_attempts;
--    the legacy process_queue path continues bumping retry_count until it
--    is retired.
-- =========================================================================

ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS processing_attempts int NOT NULL DEFAULT 0;

ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS last_error text;

-- `updated_at` and its trigger already exist per supabase_setup.sql.

-- =========================================================================
-- 4. Mark pre-visual-era records as legacy_skip (not backfill candidates)
--    visual_status values (updated): pending, processing, completed,
--    failed, skipped, legacy_skip.
--    Step 2 edge function + pg_cron safety net must treat legacy_skip
--    as terminal and never re-queue those rows.
-- =========================================================================

UPDATE transcripts
SET visual_status = 'legacy_skip'
WHERE visual_status = 'pending'
  AND created_at < '2026-04-01';

-- =========================================================================
-- 5. Normalized URL column + NON-unique index (dedup deferred)
--    Three-pass regex to produce a stable dedup key, not a valid URL.
--      Pass 1: strip utm_*, fbclid, gclid (with their leading ? or &)
--      Pass 2: trim any trailing ? or & left dangling after stripping
--      Pass 3: if the first ? was stripped, promote the first surviving
--              & to ? so the residue looks like a normal query string.
--    This makes functionally-equivalent URLs produce identical keys, which
--    matters once Step 2 enforces uniqueness at the insert layer.
-- =========================================================================

ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS url_normalized text
  GENERATED ALWAYS AS (
    lower(
      regexp_replace(
        regexp_replace(
          regexp_replace(url, '[?&](utm_[^&]*|fbclid|gclid)=[^&]*', '', 'g'),
          '[?&]+$', '', ''
        ),
        '^([^?]+)&', '\1?', ''
      )
    )
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_transcripts_url_normalized
  ON transcripts(url_normalized);

-- =========================================================================
-- 6. Full-text search (generated tsvector + GIN index)
--    Weights: A=titles, B=creator/notes, C=visual summary, D=transcript.
-- =========================================================================

-- Weights: A=titles, B=creator, C=visual_summary, D=notes+transcript.
-- Notes and transcript are demoted to D because low-signal strings like
-- "watch this" would otherwise rank equal to creator matches.
ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS search_vector tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(generated_title, title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(creator, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(visual_summary, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(notes, '')), 'D') ||
    setweight(to_tsvector('english', coalesce(transcript, '')), 'D')
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_transcripts_search_vector
  ON transcripts USING GIN(search_vector);

-- =========================================================================
-- 7. Tag helper: rename a tag (and all children) in one call.
--    Usage: SELECT rename_tag('ai', 'artificial-intelligence');
--    Returns the number of rows touched by either the exact or the
--    child rename.
-- =========================================================================

-- Service-role only. SECURITY DEFINER + fixed search_path so PostgREST
-- cannot call this via the anon/authenticated JWT and silently update
-- zero rows under RLS. If RLS ever gets added to transcripts, this
-- function still works for legitimate admin calls.
CREATE OR REPLACE FUNCTION rename_tag(old_tag text, new_tag text)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  exact_count int := 0;
  child_count int := 0;
BEGIN
  IF old_tag IS NULL OR new_tag IS NULL OR old_tag = '' OR new_tag = '' THEN
    RAISE EXCEPTION 'old_tag and new_tag must be non-empty';
  END IF;

  -- Exact matches
  UPDATE transcripts
  SET tags = array_replace(tags, old_tag, new_tag)
  WHERE old_tag = ANY(tags);
  GET DIAGNOSTICS exact_count = ROW_COUNT;

  -- Child matches (tags beginning with "old_tag/")
  UPDATE transcripts
  SET tags = ARRAY(
    SELECT CASE
             WHEN t LIKE old_tag || '/%'
               THEN new_tag || substring(t from length(old_tag) + 1)
             ELSE t
           END
    FROM unnest(tags) t
  )
  WHERE EXISTS (
    SELECT 1 FROM unnest(tags) t WHERE t LIKE old_tag || '/%'
  );
  GET DIAGNOSTICS child_count = ROW_COUNT;

  -- Note: if a single row contains both the exact tag and a child of it
  -- (e.g. both 'ai' and 'ai/claude-code'), it is counted once in each
  -- update, so the returned total can exceed the number of distinct rows
  -- touched. Data is still correct; the number is informational only.
  RETURN exact_count + child_count;
END;
$$;

REVOKE ALL ON FUNCTION rename_tag(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rename_tag(text, text) TO service_role;

-- =========================================================================
-- Verification (run these SELECTs after commit to confirm health)
-- =========================================================================

-- Expected: 277 rows, tags col present and defaulting to {}.
-- SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE tags = '{}') AS with_empty_tags FROM transcripts;

-- Expected: 209 legacy_skip rows (all previously pending, created before 2026-04-01).
-- SELECT visual_status, COUNT(*) FROM transcripts GROUP BY visual_status ORDER BY 1;

-- Expected: url_normalized populated for every row.
-- SELECT COUNT(*) AS missing FROM transcripts WHERE url_normalized IS NULL;

-- Expected: search_vector populated for every row.
-- SELECT COUNT(*) AS missing FROM transcripts WHERE search_vector IS NULL;

-- Expected: rename_tag exists.
-- SELECT proname FROM pg_proc WHERE proname = 'rename_tag';

COMMIT;
