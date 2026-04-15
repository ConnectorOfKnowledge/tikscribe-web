# Edge Function Secrets — process-transcript

## Required secrets

| Name | Where | Source |
|------|-------|--------|
| `SUPABASE_URL` | Supabase edge fn env (auto-populated) | Supabase platform |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase edge fn env (auto-populated) | Supabase platform |
| `TIKSCRIBE_WEBHOOK_HMAC` | Supabase `supabase secrets set` + Vercel env | generated once |
| `TIKSCRIBE_PROCESS_URL` | Supabase `supabase secrets set` | deploy target URL |

## Generating the HMAC shared secret

Generate a fresh 32-byte hex secret on Lonnie's workstation. Never commit the
output. Never paste it in chat, PRs, issues, or logs.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Storing the secret

Lonnie's workstation does NOT have the Supabase CLI installed. All Supabase-
side ops go through the dashboard (or Alice's Supabase MCP). Vercel side can
use either the CLI or the dashboard — value must be byte-identical either way.

**Supabase side (edge function) — dashboard path:**
1. Open https://supabase.com/dashboard/project/dgnikbbugiuuwokwenlm/functions/secrets
2. Add `TIKSCRIBE_WEBHOOK_HMAC` — paste the clipboard value from the
   generate step above.
3. Add `TIKSCRIBE_PROCESS_URL` with value
   `https://tikscribe-web.vercel.app/api/process_one`
4. Save. Values are immediately available to the next edge-function
   invocation; no redeploy required for secrets alone (redeploy IS required
   to pick up function-code changes).

**Supabase side — CLI path (for future workstations that have the CLI):**
```bash
supabase secrets set TIKSCRIBE_WEBHOOK_HMAC=<paste_from_above>
supabase secrets set TIKSCRIBE_PROCESS_URL=https://tikscribe-web.vercel.app/api/process_one
```

**Vercel side — dashboard path (preferred when pasting from the same clipboard
to avoid retype/divergence):**
1. Open https://vercel.com/lonnies-projects-69515833/tikscribe-web/settings/environment-variables
2. Add `TIKSCRIBE_WEBHOOK_HMAC` with the **same** clipboard value used for
   Supabase. Scope: Production + Preview (leave Development unchecked).
3. Save. Next Vercel deploy picks it up automatically.

**Vercel side — CLI path:**
```bash
# From any shell with the clipboard value available in $SECRET:
printf "%s" "$SECRET" | vercel env add TIKSCRIBE_WEBHOOK_HMAC production
printf "%s" "$SECRET" | vercel env add TIKSCRIBE_WEBHOOK_HMAC preview
```

**Bearer for the public-facing /api/transcribe (separate from HMAC):**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Store that value in Vercel env as `TIKSCRIBE_API_KEY` (Production + Preview,
either dashboard or CLI), and write it into the `public/index.html` meta tag
(`tikscribe-api-key` content) for the web client. Android picks up the same
value in Step 4.

## Rotation runbook (quarterly or on suspected leak)

`/api/process_one` accepts signatures from EITHER `TIKSCRIBE_WEBHOOK_HMAC`
or `TIKSCRIBE_WEBHOOK_HMAC_NEXT`. During rotation, both values are present
in Vercel env so there's no request-rejection window.

1. Generate a new secret on the operator's workstation:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   Keep the value on the clipboard. Do not echo, log, or paste into chat.

2. Vercel: add `TIKSCRIBE_WEBHOOK_HMAC_NEXT=<new>` in Production + Preview.
   - Dashboard: Settings → Environment Variables → Add New
   - CLI: `printf "%s" "$NEW" | vercel env add TIKSCRIBE_WEBHOOK_HMAC_NEXT production`
   Redeploy Vercel (or wait for the next auto-deploy). Vercel now accepts
   signatures from BOTH old and new secrets.

3. Supabase: replace `TIKSCRIBE_WEBHOOK_HMAC` with the new value.
   - Dashboard: https://supabase.com/dashboard/project/dgnikbbugiuuwokwenlm/functions/secrets
     → edit `TIKSCRIBE_WEBHOOK_HMAC` → paste new value → save.
   - CLI (if available): `supabase secrets set TIKSCRIBE_WEBHOOK_HMAC=<new>`
   Re-deploy the edge function (dashboard redeploy button, or Alice MCP's
   `deploy_edge_function`, or `supabase functions deploy process-transcript`
   if the CLI is available). The edge function now signs with the new
   secret. Vercel accepts these because `_NEXT` matches.

4. Vercel: update `TIKSCRIBE_WEBHOOK_HMAC` to the new value (same clipboard).
   Dashboard or CLI, either works. Redeploy. Wait for Vercel to show the
   deploy as Ready before moving to step 5.

5. Vercel: DELETE `TIKSCRIBE_WEBHOOK_HMAC_NEXT`. Redeploy again.

6. Verify: submit a test URL, confirm processing completes, inspect edge
   function logs for `forward_ack` (not `forward_transport_error`).

Steps 4 and 5 must happen in this order. If `_NEXT` is deleted before
`HMAC` is updated, Vercel will have only the old secret while the edge
function is signing with the new one → 401s on every call until fixed.

Full cycle takes ~15 minutes. No downtime. No rejected requests.

## Blast radius if leaked

An attacker with the HMAC secret can POST arbitrary transcript ids to
`/api/process_one` and trigger reprocessing. They CANNOT:

- Submit new URLs (that goes through `/api/transcribe`, which has its
  own bearer + rate limit + circuit breaker)
- Read or delete data (the endpoint only calls AssemblyAI + Gemini)
- Escalate to Supabase service-role access (the secret is HMAC-only)

Worst case per 1000 forged requests: roughly **~$15** reprocessing cost
(AssemblyAI Universal-2 at ~$0.015 per TikTok-length clip + Gemini 2.5
Flash at ~$0.002 per inline video analysis × 1000). The circuit breaker
at 500 rows/24h does NOT gate process_one calls (it gates submissions at
/api/transcribe), so add a per-endpoint rate limit to process_one if this
becomes a concern. Follow-up item.
