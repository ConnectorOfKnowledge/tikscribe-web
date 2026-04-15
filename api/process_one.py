"""API route: process a single transcript end-to-end.

Called ONLY by the Supabase edge function. Authenticated via HMAC-SHA256
over `{timestamp}.{body}`. Never exposed to browsers or public clients.

The existing /api/process_queue.py endpoint remains live during rollout
(per D4) as a belt-and-suspenders fallback. Once the webhook path has
been stable for 48h, process_queue + its cron entry can be retired.
"""

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler

from api._hmac import verify_with_rotation


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
HMAC_SECRET      = (os.environ.get("TIKSCRIBE_WEBHOOK_HMAC") or "").strip()
HMAC_SECRET_NEXT = (os.environ.get("TIKSCRIBE_WEBHOOK_HMAC_NEXT") or "").strip()

MAX_BODY_BYTES = 4096


def _best_effort_error_write(record_id: str, message: str) -> None:
    """Write last_error on the record. Silent if it fails -- we're already
    in an error path and don't want to mask the original failure.
    """
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb.table("transcripts").update({
            "last_error": message[:500],
            "updated_at": "now()",
        }).eq("id", record_id).execute()
    except Exception as e:
        print(f"[process_one] last_error write failed for {record_id}: {e}", file=sys.stderr)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not HMAC_SECRET or not SUPABASE_URL or not SUPABASE_KEY:
            self._respond(500, {"error": "Server misconfigured"})
            return

        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._respond(400, {"error": "Invalid body size"})
            return

        body = self.rfile.read(content_length)

        try:
            timestamp = int(self.headers.get("X-TikScribe-Timestamp", ""))
        except ValueError:
            self._respond(401, {"error": "Invalid timestamp"})
            return

        signature = self.headers.get("X-TikScribe-Signature", "")

        # Dual-secret verification: during rotation the edge function may
        # sign with either HMAC_SECRET or HMAC_SECRET_NEXT. Both are tried
        # in a single constant-time loop; fail-closed if both reject.
        if not verify_with_rotation(
            (HMAC_SECRET, HMAC_SECRET_NEXT), body, timestamp, signature
        ):
            self._respond(401, {"error": "Invalid signature"})
            return

        try:
            payload = json.loads(body)
        except Exception:
            self._respond(400, {"error": "Invalid JSON"})
            return

        record_id = payload.get("id")
        if not record_id or not isinstance(record_id, str):
            self._respond(400, {"error": "Missing id"})
            return

        try:
            self._process(record_id)
        except Exception as e:
            err_repr = f"{type(e).__name__}: {e}"
            print(f"[process_one] {record_id}: {err_repr}\n{traceback.format_exc()}", file=sys.stderr)
            # Leave a debug trail on the record so a transient Supabase or
            # AssemblyAI blip doesn't just burn one of the three attempts
            # without explanation.
            _best_effort_error_write(record_id, err_repr)
            self._respond(500, {"error": "Processing failed"})

    def _process(self, record_id: str):
        from supabase import create_client
        from api.process_queue import (
            complete_one,
            refresh_download_url,
            submit_to_assemblyai,
        )

        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        result = (
            sb.table("transcripts")
            .select("id, url, direct_url, assemblyai_id")
            .eq("id", record_id)
            .execute()
        )
        if not result.data:
            self._respond(404, {"error": "Record not found"})
            return

        record = result.data[0]
        aai_id = record.get("assemblyai_id")
        direct_url = record.get("direct_url") or ""
        original_url = record.get("url") or ""

        # Refresh once up front. TikTok CDN URLs expire after hours; YouTube
        # URLs don't need refresh (refresh_download_url returns None for
        # non-TikTok). Calling it inside the poll loop burned ~36 tikwm.com
        # requests per 3-minute AssemblyAI job.
        fresh = refresh_download_url(original_url)
        if fresh:
            direct_url = fresh
            sb.table("transcripts").update({"direct_url": fresh}).eq("id", record_id).execute()

        # Submit to AssemblyAI if we haven't yet. The atomic claim RPC has
        # already bumped processing_attempts and set status='processing', so
        # we deliberately do NOT bump retry_count here — that counter is only
        # for the legacy process_queue.py path. Double-counting would burn
        # two of three attempts on every new-flow try.
        if not aai_id:
            if not direct_url:
                sb.table("transcripts").update({
                    "status": "failed",
                    "last_error": "no download URL",
                }).eq("id", record_id).execute()
                self._respond(200, {"id": record_id, "status": "failed"})
                return
            try:
                aai_id = submit_to_assemblyai(direct_url)
                sb.table("transcripts").update({"assemblyai_id": aai_id}).eq("id", record_id).execute()
            except Exception as e:
                sb.table("transcripts").update({
                    "status": "failed",
                    "last_error": str(e)[:500],
                }).eq("id", record_id).execute()
                self._respond(200, {"id": record_id, "status": "failed"})
                return

        # Poll AssemblyAI for completion, then run Gemini.
        start = time.time()
        time.sleep(8)

        while True:
            elapsed = time.time() - start
            if elapsed > 270:
                self._respond(200, {"id": record_id, "status": "deferred_timeout"})
                return

            res = complete_one(
                sb, record_id, aai_id, direct_url,
                time_remaining=max(0.0, 280.0 - elapsed),
            )
            status = res.get("status")

            if status in ("completed", "failed", "check_failed"):
                self._respond(200, {"id": record_id, "status": status})
                return
            if status == "still_processing":
                time.sleep(5)
                continue

            self._respond(200, {"id": record_id, "status": status or "unknown"})
            return

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
