// TickScribe processing webhook.
//
// Triggered by either:
//   (a) Supabase DB webhook on transcripts INSERT
//   (b) pg_cron safety net every 30 min (same endpoint, source='safety_net')
//
// Responsibilities:
//   1. Atomic claim via claim_transcript_for_processing RPC
//      (handles attempt cap, status gate, legacy_skip filter)
//   2. HMAC-sign a forwarded request to the Vercel /api/process_one endpoint
//      which does the actual AssemblyAI + Gemini work
//   3. Return 200 immediately — fire-and-forget. If the forward fails, the
//      pg_cron safety net retries in <= 30 min.
//
// Auth: Supabase-managed JWT check is the default for Edge Functions. The
// webhook signs requests with the project's anon/service JWT, and pg_cron
// uses the service role from Vault. Both satisfy the default check.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL   = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY    = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const HMAC_SECRET    = Deno.env.get("TIKSCRIBE_WEBHOOK_HMAC") ?? "";
const FORWARD_URL    = Deno.env.get("TIKSCRIBE_PROCESS_URL") ?? "";
const FORWARD_ACK_MS = 2500; // just the ack, not the processing duration

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function hexEncode(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function signHmac(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return hexEncode(sig);
}

function log(msg: string, extra: Record<string, unknown> = {}) {
  console.log(JSON.stringify({ fn: "process-transcript", msg, ...extra }));
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  if (!HMAC_SECRET || !FORWARD_URL || !SERVICE_KEY) {
    log("misconfigured", {
      has_hmac: !!HMAC_SECRET,
      has_forward: !!FORWARD_URL,
      has_service_key: !!SERVICE_KEY,
    });
    return new Response("Server misconfigured", { status: 500 });
  }

  let payload: { record?: { id?: string }; source?: string };
  try {
    payload = await req.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const id = payload?.record?.id;
  if (typeof id !== "string" || !UUID_RE.test(id)) {
    return new Response("Missing or invalid record id", { status: 400 });
  }

  const source = payload.source === "safety_net" ? "safety_net" : "webhook";

  const supabase = createClient(SUPABASE_URL, SERVICE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data, error } = await supabase.rpc("claim_transcript_for_processing", {
    p_id: id,
  });

  if (error) {
    log("claim_error", { id, error: error.message });
    return new Response("Claim failed", { status: 500 });
  }

  const claimed = Array.isArray(data) ? data[0] : data;
  if (!claimed) {
    log("skipped_ineligible", { id, source });
    return new Response(JSON.stringify({ id, skipped: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const body = JSON.stringify({ id, source });
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = await signHmac(HMAC_SECRET, `${timestamp}.${body}`);

  // The forwarded call runs the full AssemblyAI + Gemini pipeline on Vercel
  // and can take up to 270s to return. The edge function does NOT wait for
  // that -- it aborts the fetch after FORWARD_ACK_MS so Deno doesn't keep
  // the webhook caller waiting. The abort is the expected, successful
  // path: "claim made + forward started + we're done here". Real transport
  // failures (DNS, TLS, immediate 5xx) throw a different error name and
  // are tagged separately below.
  //
  // Recovery sequence for stranded 'processing' rows:
  //   1. pg_cron release_stuck_transcript_rows runs every 10 min. After 15
  //      min of no updated_at bump, it moves the row back to 'queued'
  //      (or 'failed' if attempts exhausted).
  //   2. pg_cron tikscribe-safety-net runs every 30 min and fires this
  //      edge function for 'queued' rows older than 5 min.
  // So a row stranded here gets picked up within ~30-40 min worst case.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FORWARD_ACK_MS);

  try {
    const res = await fetch(FORWARD_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-TikScribe-Timestamp": String(timestamp),
        "X-TikScribe-Signature": signature,
      },
      body,
      signal: controller.signal,
    });
    clearTimeout(timer);
    // Drain the response body. Deno will otherwise cancel the underlying
    // stream when this Response handle is garbage-collected, which can
    // race with Vercel committing the request and cause process_one to
    // see a client disconnect before body parse.
    try { await res.text(); } catch { /* ignore drain errors */ }
    log("forward_ack", { id, source, upstream_status: res.status });
  } catch (e) {
    clearTimeout(timer);
    const name = e instanceof Error ? e.name : "unknown";
    if (name === "AbortError" || name === "TimeoutError") {
      // Expected: the downstream function hasn't finished yet. Real failures
      // (DNS, TLS, immediate upstream 5xx) throw different error names.
      log("forward_in_flight_ack_timeout", { id, source });
    } else {
      log("forward_transport_error", { id, source, err: name });
    }
  }

  return new Response(JSON.stringify({ id, forwarded: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
