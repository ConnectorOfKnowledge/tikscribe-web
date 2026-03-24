"""API route: Update review status for a transcript."""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from supabase import create_client
from api._shared import check_auth, set_cors_headers


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()

VALID_STATUSES = {"reviewed", "backburner", "attached", None}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        origin = self.headers.get("Origin")

        # Auth check
        auth_error = check_auth(self.headers)
        if auth_error:
            self._respond(401, {"error": auth_error}, origin)
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))

            record_id = body.get("id")
            review_status = body.get("review_status")
            review_notes = body.get("review_notes", "")

            if not record_id:
                self._respond(400, {"error": "id is required"}, origin)
                return

            if review_status not in VALID_STATUSES:
                self._respond(400, {
                    "error": f"Invalid review_status. Must be one of: reviewed, backburner, attached, or null"
                }, origin)
                return

            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            update_data = {"review_status": review_status}
            if review_notes:
                update_data["review_notes"] = review_notes

            # Support editing notes, attachments, and rating
            if "notes" in body:
                update_data["notes"] = body["notes"]
            if "attachments" in body:
                update_data["attachments"] = body["attachments"]
            if "rating" in body:
                rating = body["rating"]
                if rating is not None:
                    rating = int(rating)
                    if rating < 1 or rating > 5:
                        rating = None
                update_data["rating"] = rating

            result = (
                sb.table("transcripts")
                .update(update_data)
                .eq("id", record_id)
                .execute()
            )

            if not result.data:
                self._respond(404, {"error": "Transcript not found"}, origin)
                return

            self._respond(200, {
                "success": True,
                "id": record_id,
                "review_status": review_status,
                "review_notes": review_notes,
            }, origin)

        except Exception as e:
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
