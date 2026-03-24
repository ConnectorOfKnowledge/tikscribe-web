"""API route: List saved transcripts."""

import json
import os
from http.server import BaseHTTPRequestHandler
from supabase import create_client
from api._shared import check_auth, set_cors_headers


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        origin = self.headers.get("Origin")

        # Auth check
        auth_error = check_auth(self.headers)
        if auth_error:
            self._respond(401, {"error": auth_error}, origin)
            return

        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            result = (
                sb.table("transcripts")
                .select("id, url, title, generated_title, creator, thumbnail_url, duration, categories, status, review_status, review_notes, notes, attachments, rating, created_at")
                .eq("status", "completed")
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )

            self._respond(200, {"transcripts": result.data}, origin)

        except Exception as e:
            self._respond(500, {"error": str(e)}, origin)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        self.send_response(200)
        set_cors_headers(self, origin, "GET, OPTIONS")
        self.end_headers()

    def _respond(self, status, data, origin=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        set_cors_headers(self, origin, "GET, OPTIONS")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
