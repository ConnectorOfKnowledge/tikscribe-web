# TickScribe — Pipeline Fix & Tag System Handoff

**Created:** 2026-04-14 by Alice (Lonnie's chief-of-staff session)
**For:** Builder session opening in `C:\Workspace\_projects\tikscribe-web`
**Parent docs:** `C:\Workspace\_docs\tikscribe-web\` (PROJECT_STATUS.md, TODO.md, CHANGES.md, IDEAS.md)
**Supabase project:** `dgnikbbugiuuwokwenlm`

---

## Objective

Fix TickScribe's processing cadence, replace its junk auto-generated category system with a hierarchical tag system, and backfill visual analysis on records from the last two weeks. Prepare the pipeline for a follow-up session that builds a search landing page on The Bridge v2.

This is NOT the triage session. Triage (going through 191 unreviewed transcripts) is a future Alice-driven workstream that happens after this fix lands.

---

## Current State

- **Total transcripts:** 277 (API history endpoint silently caps at 50 — do not trust `/api/history` for counts)
- **Date range:** 2026-03-05 to 2026-04-14
- **Review status:** 191 unreviewed, 75 reviewed, 11 backburner, 0 attached
- **Visual status:** 6 completed, 35 failed, **236 pending** (236 are legacy — they predate the visual feature and should be marked skipped, not processed)
- **Duplicates:** 3 URLs with 2-3 copies each (trivial but worth deduping at insert going forward)
- **Cron:** daily at 6 AM UTC via `vercel.json` — too infrequent; users wait 6-24 hours for processing
- **Stack:** Python backend, AssemblyAI for audio, Gemini 2.5 Flash for visual, Capacitor Android app, Vercel hosting

## Decisions Already Made (do not re-litigate)

1. Tags use a **flat `text[]` column** with path-prefix convention for hierarchy: `ai`, `ai/claude-code`, `ai/openclaw`, `front-end-design/vibe-coding-bible`, etc. Flat storage, parent-matches-children via `LIKE 'parent/%'`. Two levels max.
2. Keep all 277 records. No deletes.
3. The auto-generated `categories` column gets renamed to `legacy_categories` and hidden from UI. Do not drop it; data may be referenced later during triage.
4. 236 pre-visual-era records are NOT backfill candidates. Mark them `visual_status='legacy_skip'` in the migration.
5. The 41 recent records with `visual_status IN ('pending', 'failed')` AND `created_at >= '2026-04-01'` ARE backfill candidates.
6. Cron moves from Vercel daily to Supabase database webhook + pg_cron 30-min safety net.
7. The Bridge search page is OUT OF SCOPE. Separate session, 48 hours after this work is soaked in production.

---

## Rollout Order (enforce strictly)

1. **Schema migration** — everything DB-side, one atomic migration
2. **Webhook + pg_cron safety net** — stop the daily-cron lag bleed
3. **Visual backfill on 41 recent records** — dry-run first, then full run
4. **Android share intent: tag picker UI**
5. **Web UI: pending-review page updates** (sort, tag pills)
6. **Update CLAUDE.md in project root** with the new patterns

---

## Detailed Work Plan

### Step 0 — Prerequisites (5 min)

- Verify `pg_cron` extension is enabled in the Supabase dashboard for project `dgnikbbugiuuwokwenlm`. If not, enable it.
- Verify Vercel project is connected to GitHub for auto-deploy on `main`.
- Confirm current `GEMINI_API_KEY` env var is set in Vercel and is not near quota.
- Create a CLAUDE.md in the project root (none exists today). Template: project purpose, stack, Supabase project ID, key files, deploy pipeline.

### Step 1 — Schema Migration (~30 min)

One migration file. All or nothing. Name it descriptively (e.g., `20260414_tag_system_and_reliability.sql`).

```sql
-- 1. Rename deprecated column
ALTER TABLE transcripts RENAME COLUMN categories TO legacy_categories;

-- 2. New tags column
ALTER TABLE transcripts ADD COLUMN tags text[] NOT NULL DEFAULT '{}';

-- 3. Enforce lowercase tags at DB level
ALTER TABLE transcripts ADD CONSTRAINT tags_lowercase CHECK (
  NOT EXISTS (SELECT 1 FROM unnest(tags) t WHERE t != lower(t))
);

-- 4. Processing reliability columns
ALTER TABLE transcripts ADD COLUMN processing_attempts int NOT NULL DEFAULT 0;
ALTER TABLE transcripts ADD COLUMN last_error text;
-- `updated_at` should already exist; if not, add it with a trigger

-- 5. Mark legacy records so they don't show in backfill queries
UPDATE transcripts
SET visual_status = 'legacy_skip'
WHERE visual_status = 'pending'
  AND created_at < '2026-04-01';

-- 6. Normalized URL column for dedup (optional but recommended)
ALTER TABLE transcripts ADD COLUMN url_normalized text
  GENERATED ALWAYS AS (lower(regexp_replace(url, '[?&](utm_[^&]*|fbclid|gclid)=[^&]*', '', 'g'))) STORED;
CREATE UNIQUE INDEX idx_transcripts_url_normalized ON transcripts(url_normalized);

-- 7. FTS generated column + GIN index
ALTER TABLE transcripts ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(generated_title, title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(creator, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(notes, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(visual_summary, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(transcript, '')), 'D')
  ) STORED;
CREATE INDEX idx_transcripts_search_vector ON transcripts USING GIN(search_vector);

-- 8. Tag helper function (for renames without manual migration)
CREATE OR REPLACE FUNCTION rename_tag(old_tag text, new_tag text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE affected int;
BEGIN
  UPDATE transcripts
  SET tags = array_replace(tags, old_tag, new_tag)
  WHERE old_tag = ANY(tags);
  GET DIAGNOSTICS affected = ROW_COUNT;
  -- Also handle children: rename `old_tag/*` to `new_tag/*`
  UPDATE transcripts
  SET tags = ARRAY(
    SELECT CASE WHEN t LIKE old_tag || '/%' THEN new_tag || substring(t from length(old_tag) + 1) ELSE t END
    FROM unnest(tags) t
  )
  WHERE EXISTS (SELECT 1 FROM unnest(tags) t WHERE t LIKE old_tag || '/%');
  RETURN affected;
END $$;
```

**Acceptance:** Migration runs without errors; row counts unchanged; all 236 legacy records now `visual_status='legacy_skip'`; `SELECT tags FROM transcripts LIMIT 1` returns `{}`.

### Step 2 — Webhook + pg_cron Safety Net (~45 min)

**2a. Edge Function** at `supabase/functions/process-transcript/index.ts`:
- Receives webhook payload with `id` of newly inserted row
- Looks up the row, calls existing processing logic (audio + visual)
- Sets `processing_attempts += 1` on start; `status='processing'` with `updated_at=now()`
- On success: sets final `status='completed'` and `visual_status` appropriately
- On failure: sets `status='failed'`, writes error to `last_error`, routes to `visual_status='failed'`
- If `processing_attempts >= 3`: mark `status='failed'` with `last_error='max retries exceeded'` and do NOT retry
- Uses `ANTHROPIC_API_KEY` (new dedicated key, NOT reused from other projects) ONLY if we later swap Gemini → Claude; for now, keeps Gemini

**2b. Database webhook:** In Supabase dashboard, Database → Webhooks → Create:
- Table: `transcripts`
- Events: INSERT
- Target: the edge function URL with service-role auth header

**2c. pg_cron safety net** (every 30 min):

```sql
SELECT cron.schedule(
  'transcript-safety-net',
  '*/30 * * * *',
  $$
  -- Pick up rows that webhook missed OR crashed mid-processing
  SELECT net.http_post(
    url := '<edge-function-url>',
    body := jsonb_build_object('record', jsonb_build_object('id', id))
  )
  FROM transcripts
  WHERE (
    (status = 'queued' AND created_at < NOW() - INTERVAL '5 minutes')
    OR (status = 'processing' AND updated_at < NOW() - INTERVAL '15 minutes')
  )
  AND processing_attempts < 3
  LIMIT 20;  -- cap per run to prevent runaway
  $$
);
```

**2d. Retire the Vercel daily cron.** Remove the entry from `vercel.json`. Deploy.

**Acceptance:** Submit a test TikTok via the existing flow. Webhook fires within 10 seconds. Processing completes. Shut off webhook, submit another; pg_cron picks it up within 30 min. Both cases verified in logs.

### Step 3 — Visual Backfill (~20 min including dry-run)

Script at `scripts/backfill_visual.py`.

**3a. Dry-run mode** (default): pick 5 records matching the filter, process them, print outputs, do not update DB. Confirm outputs look sensible with Lonnie before flipping the switch.

**3b. Full run:** Process all 41 matching records with:
- **Concurrency cap: 3 parallel** (Gemini 2.5 Flash hits 429s under burst)
- **Exponential backoff** on 429/5xx: 2s, 8s, 30s, then abort that record
- Per-record timeout: 120s
- Progress logging every record
- Final report: total processed, succeeded, failed, skipped

**Filter:**
```sql
WHERE created_at >= '2026-04-01'
  AND visual_status IN ('pending', 'failed')
```

**Acceptance:** All 41 records either have `visual_status='completed'` with non-empty `visual_summary`, or `visual_status='failed'` with explanatory `last_error`. No records stuck in `processing`.

### Step 4 — Android Share Intent: Tag Picker (~90 min)

File: `android/` Capacitor project. Share target form currently captures URL, notes, attachments, rating.

**Add: Tag picker** below notes field.
- Fetch existing tags from DB on form open: `SELECT DISTINCT unnest(tags) FROM transcripts ORDER BY 1`
- Group by parent (split on `/`) — show `ai` collapsible with children indented, same for `front-end-design`, `business`, `bus-life`, plus flat `education`
- Tap chip to select/deselect
- Type-to-add with autosuggest: filters existing tags, shows "Create new tag: `<text>`" at bottom
- Case-insensitive, normalized to lowercase-hyphens on save (`Claude Code` → `claude-code`)
- Selected tags appear as chips above the list; tap × to remove
- Submit sends `tags: ["ai", "ai/claude-code"]` as array to `/api/transcribe`

**API change:** `/api/transcribe` endpoint accepts `tags` field, validates (all lowercase, no spaces), writes to `tags` column on insert.

**Seed tags to ensure present on first load** (insert into a `tag_seeds` view or just document as defaults):
- `ai`, `ai/claude-code`, `ai/openclaw`, `ai/openjarvis`, `ai/gemini`, `ai/agents`, `ai/mcp`
- `front-end-design`, `front-end-design/vibe-coding-bible`, `front-end-design/components`, `front-end-design/css`
- `business`, `business/subtl`, `business/marketing`
- `bus-life`, `bus-life/engine`, `bus-life/electrical`, `bus-life/cooking`
- `education`

**Acceptance:** From the Android share sheet, share a TikTok; tag picker appears; tap 2 parents + 1 child; submit; DB row has `tags = ['ai', 'ai/claude-code', 'education']` (or whatever was selected).

### Step 5 — Web UI: Pending Review Page (~45 min)

Existing page (find it in the web UI, likely `pages/review.tsx` or similar).

**Changes:**
- Default sort: `created_at DESC`
- Remove `legacy_categories` from display entirely (hide the column/field)
- Show `tags` as colored pills (one color per top-level parent — pick 6 distinct palette colors)
- Click pill → X button appears, click X removes the tag
- "+ Add tag" button opens an autocomplete dropdown of existing tags, or type to create new
- Tag changes save immediately on blur (optimistic UI, rollback on error)
- Filter bar at top: text input (queries FTS `search_vector`), tag filter tree (same tree shape as Android picker), date range

**Acceptance:** Open pending-review page; items are sorted newest first; tag pills are editable; filtering by `ai/claude-code` narrows to only those items.

### Step 6 — Observability & Housekeeping (~20 min)

- Add a Supabase view `v_tag_usage` for quick tag-count queries: `SELECT tag, COUNT(*) FROM transcripts, unnest(tags) tag GROUP BY tag ORDER BY 2 DESC`
- Add a view `v_processing_health` for queue/processing status distribution
- Update `PROJECT_STATUS.md` at `C:\Workspace\_docs\tikscribe-web\PROJECT_STATUS.md` with what shipped
- Update `CHANGES.md` with a dated entry

---

## Out of Scope (DO NOT DO)

- Search landing page on The Bridge (separate session, 48h from now)
- Triage of 191 unreviewed transcripts (Alice-driven, future workstream)
- Replacing Gemini with Claude vision (future consideration based on backfill outcomes)
- Deleting any records
- Touching The Bridge v2 repo
- Auto-tagging legacy records (future triage phase)

---

## Known Gotchas

1. **Supabase API `/api/history` silently caps at 50 records.** Do not use for counts. Query the DB directly via MCP or service-role.
2. **Three open worktrees exist in The Bridge v2 repo** — unrelated to this work but do not start Bridge work before reconciling them.
3. **Android Capacitor app requires a rebuild + ADB push** to test tag picker changes on Lonnie's phone. Per CLAUDE.md: `npx expo run:android` or `./gradlew assembleRelease` locally (never EAS cloud — queue times), push via `C:\Users\Conne\AppData\Local\Android\Sdk\platform-tools\adb.exe`. Ask first before any ADB command.
4. **Never reuse API keys across projects.** If Claude API ever replaces Gemini, create a new key named `tikscribe-visual-analysis` — don't use Alice's or Bridge's key.
5. **Backfill concurrency matters.** Do not run 41 records in parallel; Gemini 2.5 Flash throttles aggressively.
6. **Path-prefix tag hierarchy has a rename cost.** If Lonnie wants to rename a parent tag, use the `rename_tag(old, new)` function we created. Document this clearly in CLAUDE.md.

---

## Testing Plan

1. Run DB migration in a Supabase branch first, not production. Verify row counts, column presence, constraints.
2. Merge migration to main branch.
3. Deploy edge function + webhook.
4. Submit one test TikTok → watch logs → verify processed in <10 seconds.
5. Run backfill dry-run → show outputs to Lonnie → get green light.
6. Run backfill full → verify all 41 records have `visual_summary`.
7. Build Android app, push to Lonnie's phone, share a TikTok with 3 tags.
8. Open web UI, confirm sort + pills + filters work.
9. Update docs.

---

## When Done

1. Commit with clear message: `feat: tag system, webhook processing, visual backfill`
2. Push to master (Cloudflare/Vercel auto-deploys)
3. Update `C:\Workspace\_docs\tikscribe-web\PROJECT_STATUS.md` + `CHANGES.md`
4. Update this `HANDOFF.md` → rename to `HANDOFF-completed-2026-04-14.md` or delete entirely (next handoff will overwrite)
5. Report back to Alice session summarizing what was done and any surprises.
6. Flag for Alice: "ready to start Bridge search page session in 48 hours if stable."

---

## Quality Standards & Required Skill Invocations (HARD RULES)

These are from Lonnie's global CLAUDE.md. They apply to every step below. Not optional.

### Before you touch code

1. **Second Opinion Rule.** Before executing the plan — even though Alice already had it reviewed once — spawn a review agent in your session to audit YOUR execution approach. It should challenge your SQL, your rollout sequence, and your interpretation of this handoff. Tell Lonnie you're doing it so he knows. Incorporate feedback into a final plan. This catches drift between the handoff and what you actually do.
2. **Invoke `superpowers:using-superpowers`** at session start. It establishes skill discipline for the rest of the session.
3. **For UI work (Step 4 Android tag picker + Step 5 web UI):** invoke `frontend-design:frontend-design` BEFORE writing any UI code. This is a HARD RULE from Lonnie's CLAUDE.md, not a suggestion. It prevents "AI slop aesthetics" defaults.
4. **For any significant code work:** invoke `canon-coder` (the Vibe Coding Bible agent) — it loads Lonnie's methodology and quality standards. Especially relevant when building new components or features.

### During implementation

5. **`superpowers:test-driven-development`** before writing implementation code for any new function, migration, or feature. TDD applies to the visual backfill script and the edge function particularly.
6. **`superpowers:systematic-debugging`** if you hit any bug, test failure, or unexpected behavior. Do NOT propose fixes without following the systematic debugging protocol.
7. **`superpowers:dispatching-parallel-agents`** if you hit 2+ independent tasks (e.g., Android build + web UI changes can happen in parallel).

### Before deploying to production

8. **`serverless-security-audit`** — HARD RULE. The `/api/transcribe` endpoint accepts public input and triggers paid API calls (AssemblyAI, Gemini). Before deploying the webhook-based flow, run this skill. Think like an attacker: can someone spam the submit endpoint with no auth? Can they submit malicious URLs? Can they exhaust your Gemini quota? Do NOT skip this because "it's just a transcript service."
9. **`feature-dev:code-reviewer`** — HARD RULE before ANY deployment. Run it on the full set of changes before merging to master.
10. **`audit-code`** if you touch Playwright tests, E2E flows, or performance-critical paths.
11. **`api-key-security`** before rotating or adding any API key (a new dedicated key for the edge function would qualify).

### Verification

12. **`superpowers:verification-before-completion`** — before claiming any step is done, verify with actual commands (run the migration, hit the endpoint, process a test video end-to-end). Evidence before assertions. Especially important for Step 2 (webhook) and Step 3 (backfill) where silent failures are easy.
13. **`superpowers:requesting-code-review`** — when wrapping up major chunks of work for final sign-off.

### When reporting back to Lonnie

- Use the existing `HANDOFF.md` pattern to document what you did + what surprised you.
- If you violated or skipped any of the above skills, name it explicitly so Alice can flag it.
- Do NOT deploy to production without confirming Lonnie has seen the code-reviewer + security-audit outputs.

---

## Contact

Alice session (Lonnie's assistant) approved this plan after a parallel review by two agents. The plan incorporates their modifications. Questions that go beyond this scope should be raised back to Lonnie directly before proceeding. When in doubt, ask — Lonnie would rather answer a question than clean up an avoidable mistake.
