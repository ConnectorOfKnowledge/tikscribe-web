"""API route: Run Gemini visual analysis on a single transcript."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from api._shared import check_auth, set_cors_headers, client_ip
from api._rate_limit import check_ip_rate_limit, IP_MAX_PER_WINDOW, IP_WINDOW_MINUTES

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()

MAX_BODY_BYTES = 4096


def run_gemini_analysis(video_url: str) -> str | None:
    """Run full Gemini analysis on a video (audio + visual)."""
    if not GEMINI_KEY:
        return None

    try:
        from google import genai
        import urllib.request
        import base64

        client = genai.Client(api_key=GEMINI_KEY)

        req = urllib.request.Request(video_url, headers={"User-Agent": "tikscribe/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_length = int(resp.headers.get("Content-Length") or 0)
            if content_length > 20_000_000:
                return None
            video_bytes = resp.read()
            if len(video_bytes) > 20_000_000:
                return None

        video_b64 = base64.b64encode(video_bytes).decode("utf-8")

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {
                    "parts": [
                        {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                        {"text": (
                            "Watch and listen to this entire video. Provide a complete analysis:\n"
                            "1) TRANSCRIPT: Write out everything spoken in the video.\n"
                            "2) ON-SCREEN TEXT: Extract ALL text shown on screen "
                            "(titles, captions, bullet points, slides, URLs).\n"
                            "3) VISUAL DESCRIPTION: Describe what is visually happening.\n"
                            "4) KEY TAKEAWAYS: If there are step-by-step instructions, "
                            "tips, or actionable items, list them.\n"
                            "5) SUMMARY: A concise 2-3 sentence summary of the full content."
                        )},
                    ]
                }
            ],
        )
        return response.text
    except Exception as e:
        print(f"[WARN] Gemini analysis failed: {e}", file=sys.stderr)
        return None


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        origin = self.headers.get("Origin")

        auth_error = check_auth(self.headers)
        if auth_error:
            self._respond(401, {"error": auth_error}, origin)
            return

        try:
            content_length = int(self.headers.get("Content-Length") or 0)
            if content_length <= 0 or content_length > MAX_BODY_BYTES:
                self._respond(413, {"error": "Payload too large"}, origin)
                return

            body = json.loads(self.rfile.read(content_length))
            record_id = body.get("id")

            if not record_id:
                self._respond(400, {"error": "id is required"}, origin)
                return

            if not GEMINI_KEY:
                self._respond(500, {"error": "GEMINI_API_KEY not configured"}, origin)
                return

            from supabase import create_client
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            ip = client_ip(self.headers)
            allowed, rl_count = check_ip_rate_limit(sb, f"visual:{ip}")
            if not allowed:
                self._respond(429, {
                    "error": "Rate limit exceeded",
                    "limit": IP_MAX_PER_WINDOW,
                    "window_minutes": IP_WINDOW_MINUTES,
                    "current": rl_count,
                }, origin)
                return

            # Atomic claim via RPC: prevents concurrent Gemini runs on one row.
            # Returns empty if already processing / legacy_skip / missing.
            claim = sb.rpc("claim_visual_for_processing", {"p_id": record_id}).execute()
            claimed = (claim.data or [None])[0] if isinstance(claim.data, list) else claim.data
            if not claimed:
                self._respond(409, {"error": "Not eligible for visual analysis"}, origin)
                return

            direct_url = claimed.get("direct_url") or ""
            if not direct_url:
                sb.table("transcripts").update({
                    "visual_status": "failed",
                    "last_error": "no direct_url",
                }).eq("id", record_id).execute()
                self._respond(400, {"error": "No video URL available for this transcript"}, origin)
                return

            # Run Gemini (full audio + visual analysis)
            visual = run_gemini_analysis(direct_url)

            if visual:
                sb.table("transcripts").update({
                    "visual_summary": visual,
                    "visual_status": "completed",
                    "has_visual_content": True,
                }).eq("id", record_id).execute()

                self._respond(200, {
                    "success": True,
                    "id": record_id,
                    "visual_summary": visual[:200] + "..." if len(visual) > 200 else visual,
                }, origin)
            else:
                sb.table("transcripts").update({"visual_status": "failed"}).eq("id", record_id).execute()
                self._respond(200, {"success": False, "error": "Visual analysis returned no results"}, origin)

        except Exception as e:
            print(f"[ERROR] Visual analysis failed: {e}", file=sys.stderr)
            self._respond(500, {"error": str(e)}, origin)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        self.send_response(200)
        set_cors_headers(self, origin, "POST, OPTIONS")
        self.end_headers()

    def _respond(self, status, data, origin=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        set_cors_headers(self, origin, "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
