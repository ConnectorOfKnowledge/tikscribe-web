"""API route: Update review status for a transcript."""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from supabase import create_client


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()

VALID_STATUSES = {"reviewed", "backburner", "attached", None}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))

            record_id = body.get("id")
            review_status = body.get("review_status")
            review_notes = body.get("review_notes", "")

            if not record_id:
                self._respond(400, {"error": "id is required"})
                return

            if review_status not in VALID_STATUSES:
                self._respond(400, {
                    "error": f"Invalid review_status. Must be one of: reviewed, backburner, attached, or null"
                })
                return

            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            update_data = {"review_status": review_status}
            if review_notes:
                update_data["review_notes"] = review_notes

            result = (
                sb.table("transcripts")
                .update(update_data)
                .eq("id", record_id)
                .execute()
            )

            if not result.data:
                self._respond(404, {"error": "Transcript not found"})
                return

            self._respond(200, {
                "success": True,
                "id": record_id,
                "review_status": review_status,
                "review_notes": review_notes,
            })

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
