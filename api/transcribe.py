"""API route: Submit a URL for transcription."""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler
from supabase import create_client


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
ASSEMBLYAI_KEY = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()


def get_video_info(url: str) -> dict:
    """Extract video metadata and direct URL using yt-dlp."""
    result = subprocess.run(
        ["python", "-m", "yt_dlp", "--dump-json", "--no-download", url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not process URL: {result.stderr[:200]}")
    return json.loads(result.stdout)


def get_direct_url(url: str) -> str:
    """Get direct media URL using yt-dlp."""
    result = subprocess.run(
        ["python", "-m", "yt_dlp", "--get-url", "--no-playlist", url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not extract media URL: {result.stderr[:200]}")
    # May return multiple URLs (video + audio), take the first
    urls = result.stdout.strip().split("\n")
    return urls[0]


def submit_to_assemblyai(audio_url: str) -> str:
    """Submit audio URL to AssemblyAI for transcription. Returns transcript ID."""
    import urllib.request

    data = json.dumps({
        "audio_url": audio_url,
        "iab_categories": True,       # Auto-categorize
        "auto_chapters": True,        # Chapter summaries
        "summarization": True,
        "summary_type": "headline",   # Short descriptive title
    }).encode()

    req = urllib.request.Request(
        "https://api.assemblyai.com/v2/transcript",
        data=data,
        headers={
            "Authorization": ASSEMBLYAI_KEY,
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        return result["id"]


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Parse request body
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            url = body.get("url", "").strip()

            if not url:
                self._respond(400, {"error": "URL is required"})
                return

            # Get video info
            info = get_video_info(url)
            title = info.get("title", "Untitled")
            creator = info.get("uploader", "Unknown")
            thumbnail_url = info.get("thumbnail", "")
            duration = int(info.get("duration", 0))
            description = info.get("description", "")

            # Get direct media URL for AssemblyAI
            direct_url = get_direct_url(url)

            # Submit to AssemblyAI
            aai_id = submit_to_assemblyai(direct_url)

            # Save to Supabase
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            record = sb.table("transcripts").insert({
                "url": url,
                "title": title,
                "creator": creator,
                "thumbnail_url": thumbnail_url,
                "duration": duration,
                "description": description,
                "status": "processing",
                "assemblyai_id": aai_id,
            }).execute()

            row_id = record.data[0]["id"]

            self._respond(200, {
                "id": row_id,
                "assemblyai_id": aai_id,
                "title": title,
                "status": "processing",
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
