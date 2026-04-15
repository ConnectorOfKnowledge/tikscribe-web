# TickScribe Web — Project Context for Claude

## Purpose
TikTok (and eventually other short-form) transcript + visual-analysis pipeline. Users share URLs from the Android app or web; the backend downloads audio, runs AssemblyAI for transcripts and Gemini 2.5 Flash for visual summaries, then stores results in Supabase for later review.

## Stack
- **Backend:** Python serverless functions on Vercel (`api/*.py`)
- **Web UI:** Vanilla HTML/CSS/JS in `public/`
- **Mobile:** Capacitor Android app in `android/` (share intent target)
- **DB:** Supabase Postgres (project `dgnikbbugiuuwokwenlm`, internal name "cortex-os")
- **Audio:** AssemblyAI
- **Visual:** Gemini 2.5 Flash via `google-genai`
- **Hosting:** Vercel (auto-deploy on push to `master`)

## Key Paths
- `api/transcribe.py` — URL submission endpoint
- `api/process_queue.py` — background processor (previously the daily cron)
- `api/analyze_visual.py` — Gemini visual analysis
- `api/history.py` — lists transcripts (⚠️ silently caps at 50 — do NOT use for counts)
- `api/review.py` — mark reviewed / edit fields
- `api/status.py` — single-transcript status poll
- `api/_shared.py` — CORS + auth helpers
- `supabase_setup.sql` — original schema
- `supabase_migration_visual.sql` — visual-processing columns
- `migrations/` — newer migrations live here (tag system, reliability, FTS)

## Data Model (transcripts table)
- Identity: `id`, `url`, `url_normalized` (generated, unique)
- Content: `title`, `generated_title`, `creator`, `thumbnail_url`, `transcript`, `segments`, `notes`, `attachments`, `rating`, `description`, `language`, `duration`
- Visual: `direct_url`, `visual_summary`, `visual_status`, `has_visual_content`
- Review: `reviewed_at`, `reviewed_via`
- Tags: `tags text[]` (hierarchical via `parent/child` path-prefix), `legacy_categories` (deprecated — hidden from UI, retained for reference)
- Reliability: `status`, `processing_attempts`, `last_error`, `retry_count`, `updated_at`, `created_at`
- Search: `search_vector tsvector` (generated, GIN-indexed)

## Tag System
Tags are stored as a flat `text[]` with path-prefix hierarchy (two levels max):
- Parents: `ai`, `front-end-design`, `business`, `bus-life`, `education`
- Children: `ai/claude-code`, `front-end-design/vibe-coding-bible`, etc.
- All tags are lowercase, hyphen-separated (enforced by CHECK constraint)
- Parent-matches-children via `tags && ARRAY['ai'] OR EXISTS(unnest(tags) t WHERE t LIKE 'ai/%')`
- Rename a tag (and its children) with: `SELECT rename_tag('old', 'new');`

## Processing Pipeline (post-2026-04-14 rework)
1. **Ingest** (`api/transcribe.py`): inserts row with `status='queued'`
2. **Webhook:** Supabase DB webhook fires on INSERT → calls edge function `process-transcript`
3. **Edge function:** sets `status='processing'`, runs audio + visual, sets final `status`
4. **pg_cron safety net** (every 30 min): picks up rows the webhook missed or that crashed mid-flight. Caps at `processing_attempts < 3` and batches ≤ 20 per run.

## Deploy Pipeline
- Push to `master` on GitHub → Vercel auto-deploys
- No manual deploy steps. If Vercel deploy fails, check `vercel.json` and Vercel logs.
- Database changes: run SQL in Supabase dashboard SQL editor OR via migrations folder if using Supabase CLI.

## Environment Variables (live in Vercel)
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `ASSEMBLYAI_API_KEY`
- `GEMINI_API_KEY`
- `CRON_SECRET` — used by the legacy daily-cron endpoint; retires 48h after Step 2 soak
- `TIKSCRIBE_API_KEY` — **required (fail-closed)**, bearer on all `/api/*` routes except `/api/process_one`
- `TIKSCRIBE_WEBHOOK_HMAC` — **required**, HMAC-SHA256 secret shared with the Supabase edge function, used on `/api/process_one` only

## Secret rotation
HMAC + bearer rotation procedure lives at `supabase/functions/process-transcript/SECRETS.md`. Rotate quarterly or on leak.

## Known Gotchas
- `/api/history` silently caps at 50 rows. Query the DB directly for counts or audits.
- TikTok download URLs expire quickly — `direct_url` gets refreshed at process time.
- Gemini 2.5 Flash throttles under burst — keep backfill concurrency ≤ 3.
- Never reuse API keys across projects. Each project gets its own restricted key.

## Not In Scope For This Repo
- The Bridge v2 dashboard / search UI lives in a separate repo.
- Triage workflow (reviewing 191 unreviewed transcripts) is an Alice-session workstream, not a build task.
