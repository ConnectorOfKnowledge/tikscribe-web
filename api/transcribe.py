"""API route: Submit a URL for transcription."""

import json
import os
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
ASSEMBLYAI_KEY = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()


def get_oembed_metadata(url: str) -> dict:
    """Get video metadata via oEmbed API (works for TikTok, YouTube, etc.)."""
    import urllib.request
    import urllib.parse
    import urllib.error

    # Try TikTok oEmbed
    if "tiktok.com" in url:
        oembed_url = f"https://www.tiktok.com/oembed?url={urllib.parse.quote(url)}"
    elif "youtube.com" in url or "youtu.be" in url:
        oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
    else:
        return {}

    try:
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "tikscribe/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def get_direct_url(url: str) -> str:
    """Get direct media URL using yt-dlp subprocess (avoids format probing)."""
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp",
         "--get-url", "--no-playlist", "--no-check-formats",
         "--format", "best", url],
        capture_output=True, text=True, timeout=45
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp URL extract failed: {result.stderr[:500]}")
    urls = result.stdout.strip().split("\n")
    return urls[0]


def get_video_metadata(url: str) -> dict:
    """Get video metadata using yt-dlp subprocess."""
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp",
         "--dump-json", "--no-download", "--no-playlist",
         "--no-check-formats", url],
        capture_output=True, text=True, timeout=45
    )
    if result.returncode != 0:
        return {}  # Fall back to oEmbed
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def submit_to_assemblyai(audio_url: str) -> str:
    """Submit audio URL to AssemblyAI for transcription."""
    import urllib.request
    import urllib.error

    data = json.dumps({
        "audio_url": audio_url,
        "speech_models": ["universal-2"],
        "iab_categories": True,
        "auto_chapters": True,
    }).encode()

    req = urllib.request.Request(
        "https://api.assemblyai.com/v2/transcript",
        data=data,
        headers={
            "Authorization": ASSEMBLYAI_KEY,
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result["id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"AssemblyAI error {e.code}: {body[:500]}")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        step = "init"
        try:
            # Parse request body
            step = "parsing request"
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            url = body.get("url", "").strip()

            if not url:
                self._respond(400, {"error": "URL is required"})
                return

            # Validate env vars are set
            step = "checking config"
            missing = []
            if not ASSEMBLYAI_KEY:
                missing.append("ASSEMBLYAI_API_KEY")
            if not SUPABASE_URL:
                missing.append("SUPABASE_URL")
            if not SUPABASE_KEY:
                missing.append("SUPABASE_SERVICE_KEY")

            if missing:
                self._respond(500, {
                    "error": f"Server misconfigured - missing: {', '.join(missing)}"
                })
                return

            # Step 1: Get metadata (try yt-dlp first, fall back to oEmbed)
            step = "getting metadata"
            info = get_video_metadata(url)
            if not info:
                oembed = get_oembed_metadata(url)
                info = {
                    "title": oembed.get("title", "Untitled"),
                    "uploader": oembed.get("author_name", "Unknown"),
                    "thumbnail": oembed.get("thumbnail_url", ""),
                    "duration": 0,
                    "description": "",
                }

            title = info.get("title", "Untitled")
            creator = info.get("uploader", info.get("creator", "Unknown"))
            thumbnail_url = info.get("thumbnail", "")
            duration = int(info.get("duration", 0))
            description = info.get("description", "")

            # Step 2: Get direct media URL
            step = "extracting direct URL (yt-dlp)"
            direct_url = get_direct_url(url)

            # Step 3: Submit to AssemblyAI
            step = "submitting to AssemblyAI"
            aai_id = submit_to_assemblyai(direct_url)

            # Step 4: Save to Supabase
            step = "saving to Supabase"
            from supabase import create_client
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
            tb = traceback.format_exc()
            error_msg = f"Failed at '{step}': {str(e)}"
            self._respond(500, {"error": error_msg, "traceback": tb[-800:]})

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
