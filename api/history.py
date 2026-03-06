"""API route: List saved transcripts."""

import json
import os
from http.server import BaseHTTPRequestHandler
from supabase import create_client


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            result = (
                sb.table("transcripts")
                .select("id, url, title, generated_title, creator, thumbnail_url, duration, categories, status, review_status, review_notes, notes, attachments, created_at")
                .eq("status", "completed")
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )

            self._respond(200, {"transcripts": result.data})

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
