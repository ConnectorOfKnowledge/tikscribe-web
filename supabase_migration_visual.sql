-- TikScribe Visual Processing + Queue-Based Architecture Migration
-- Run this against the cortex-os Supabase project (dgnikbbugiuuwokwenlm)

-- Store the direct download URL so the background processor can use it
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS direct_url TEXT;

-- Gemini visual analysis results
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS visual_summary TEXT;
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS visual_status TEXT DEFAULT 'pending';
-- visual_status values: pending, processing, completed, failed, skipped

-- Flag for visual-only videos (audio transcript empty but visual has content)
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS has_visual_content BOOLEAN DEFAULT FALSE;

-- Bridge review tracking (shared between TikScribe web UI and The Bridge)
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS reviewed_via TEXT;
-- reviewed_via values: 'tikscribe', 'bridge'

-- Retry tracking for failed processing (cap at 3 retries)
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;
