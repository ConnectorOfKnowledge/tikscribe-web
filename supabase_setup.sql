-- tikscribe: transcripts table
-- Run this in Supabase SQL Editor (cortex-os project)

CREATE TABLE IF NOT EXISTS transcripts (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    generated_title TEXT,          -- AI-generated descriptive title
    creator TEXT,
    thumbnail_url TEXT,
    transcript TEXT,
    segments JSONB,                -- timestamped chapters/segments
    categories TEXT[],             -- auto-detected topic categories
    language TEXT DEFAULT 'en',
    duration INTEGER,
    description TEXT,
    status TEXT DEFAULT 'processing',  -- processing, completed, error
    assemblyai_id TEXT,
    notes TEXT,                     -- user notes added during submission
    attachments JSONB,             -- uploaded screenshots [{name, type, size, data}]
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),  -- 5=must address, 4=group, 3=maybe, 1-2=rarely
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Migration: Add notes, attachments, and rating columns if table already exists
-- ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS notes TEXT;
-- ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS attachments JSONB;
-- ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS rating INTEGER CHECK (rating >= 1 AND rating <= 5);

-- Index for fast history queries
CREATE INDEX IF NOT EXISTS idx_transcripts_status ON transcripts(status);
CREATE INDEX IF NOT EXISTS idx_transcripts_created ON transcripts(created_at DESC);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER transcripts_updated_at
    BEFORE UPDATE ON transcripts
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
