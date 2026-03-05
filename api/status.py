"""API route: Check transcription status and retrieve result."""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from supabase import create_client


SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
ASSEMBLYAI_KEY = os.environ.get("ASSEMBLYAI_API_KEY")


def check_assemblyai(aai_id: str) -> dict:
    """Check AssemblyAI transcription status."""
    import urllib.request

    req = urllib.request.Request(
        f"https://api.assemblyai.com/v2/transcript/{aai_id}",
        headers={"Authorization": ASSEMBLYAI_KEY},
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def extract_categories(aai_result: dict) -> list:
    """Extract top IAB categories from AssemblyAI result."""
    categories = []
    iab = aai_result.get("iab_categories_result", {})
    summary = iab.get("summary", {})

    # Get top categories (relevance > 0.3)
    for label, score in sorted(summary.items(), key=lambda x: x[1], reverse=True):
        if score > 0.3 and len(categories) < 5:
            # IAB labels look like "Technology&Computing>ArtificialIntelligence"
            # Take the most specific part
            parts = label.split(">")
            short = parts[-1]
            # Add spaces before capitals: "ArtificialIntelligence" -> "Artificial Intelligence"
            readable = ""
            for i, c in enumerate(short):
                if c.isupper() and i > 0 and not short[i-1].isupper():
                    readable += " "
                readable += c
            if readable not in categories:
                categories.append(readable)

    return categories


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # Parse query params
            query = parse_qs(urlparse(self.path).query)
            record_id = query.get("id", [None])[0]

            if not record_id:
                self._respond(400, {"error": "id parameter is required"})
                return

            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            # Get record from Supabase
            result = sb.table("transcripts").select("*").eq("id", record_id).execute()

            if not result.data:
                self._respond(404, {"error": "Transcript not found"})
                return

            record = result.data[0]

            # If already completed, return it
            if record["status"] == "completed":
                self._respond(200, record)
                return

            # Check AssemblyAI status
            aai_id = record.get("assemblyai_id")
            if not aai_id:
                self._respond(500, {"error": "No AssemblyAI ID found"})
                return

            aai_result = check_assemblyai(aai_id)
            aai_status = aai_result.get("status")

            if aai_status == "completed":
                # Extract data
                transcript_text = aai_result.get("text", "")
                categories = extract_categories(aai_result)

                # Get generated title from summary or auto_chapters
                generated_title = aai_result.get("summary", "")
                if not generated_title:
                    chapters = aai_result.get("chapters", [])
                    if chapters:
                        generated_title = chapters[0].get("headline", "")

                # Build segments from words
                segments = []
                for chapter in aai_result.get("chapters", []):
                    segments.append({
                        "start": chapter.get("start", 0) / 1000,
                        "end": chapter.get("end", 0) / 1000,
                        "text": chapter.get("summary", ""),
                        "headline": chapter.get("headline", ""),
                    })

                # Update Supabase
                update_data = {
                    "status": "completed",
                    "transcript": transcript_text,
                    "generated_title": generated_title,
                    "categories": categories,
                    "segments": segments,
                    "language": aai_result.get("language_code", "en"),
                }

                sb.table("transcripts").update(update_data).eq("id", record_id).execute()

                # Return full record
                record.update(update_data)
                self._respond(200, record)

            elif aai_status == "error":
                error_msg = aai_result.get("error", "Transcription failed")
                sb.table("transcripts").update({
                    "status": "error"
                }).eq("id", record_id).execute()
                self._respond(200, {"status": "error", "error": error_msg})

            else:
                # Still processing
                self._respond(200, {"status": "processing", "id": record_id})

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
