"""API route: Run Gemini visual analysis on a single transcript."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from api._shared import check_auth, set_cors_headers

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()


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
            content_length = int(self.headers.get("Content-Length", 0))
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

            # Get the record
            result = sb.table("transcripts").select("id, direct_url, visual_status").eq("id", record_id).execute()
            if not result.data:
                self._respond(404, {"error": "Transcript not found"}, origin)
                return

            record = result.data[0]
            direct_url = record.get("direct_url", "")

            if not direct_url:
                self._respond(400, {"error": "No video URL available for this transcript"}, origin)
                return

            # Update status to processing
            sb.table("transcripts").update({"visual_status": "processing"}).eq("id", record_id).execute()

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
