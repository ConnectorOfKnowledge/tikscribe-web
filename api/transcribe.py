"""API route: Submit a URL for transcription."""

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
ASSEMBLYAI_KEY = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()


def get_video_info_and_url(url: str) -> tuple:
    """Extract video metadata and direct media URL using yt-dlp as a library."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Get direct media URL from info dict
    direct_url = info.get("url", "")
    if not direct_url:
        formats = info.get("formats", [])
        if formats:
            # Prefer formats that have audio
            audio_fmts = [
                f for f in formats if f.get("acodec", "none") != "none"
            ]
            if audio_fmts:
                direct_url = audio_fmts[-1].get("url", "")
            else:
                direct_url = formats[-1].get("url", "")

    if not direct_url:
        raise RuntimeError("Could not extract direct media URL from yt-dlp")

    return info, direct_url


def submit_to_assemblyai(audio_url: str) -> str:
    """Submit audio URL to AssemblyAI for transcription."""
    import urllib.request
    import urllib.error

    data = json.dumps({
        "audio_url": audio_url,
        "speech_models": ["universal-2"],
        "iab_categories": True,
        "auto_chapters": True,
        "summarization": True,
        "summary_type": "headline",
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

            # Step 1: Get video info + direct URL (single yt-dlp call)
            step = "extracting video info (yt-dlp)"
            info, direct_url = get_video_info_and_url(url)
            title = info.get("title", "Untitled")
            creator = info.get("uploader", info.get("creator", "Unknown"))
            thumbnail_url = info.get("thumbnail", "")
            duration = int(info.get("duration", 0))
            description = info.get("description", "")

            # Step 2: Submit to AssemblyAI
            step = "submitting to AssemblyAI"
            aai_id = submit_to_assemblyai(direct_url)

            # Step 3: Save to Supabase
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
