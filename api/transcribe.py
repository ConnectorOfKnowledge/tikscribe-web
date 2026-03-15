"""API route: Submit a URL for transcription."""

import json
import os
import sys
import subprocess
import traceback
from http.server import BaseHTTPRequestHandler


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
ASSEMBLYAI_KEY = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()


def get_tiktok_info(url: str) -> dict:
    """Get TikTok video info and proxied download URL via tikwm.com API."""
    import urllib.request
    import urllib.parse

    data = urllib.parse.urlencode({"url": url, "hd": 1}).encode()
    req = urllib.request.Request(
        "https://tikwm.com/api/",
        data=data,
        headers={
            "User-Agent": "tikscribe/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())

    if result.get("code") != 0 or "data" not in result:
        raise RuntimeError(f"TikTok info failed: {result.get('msg', 'no data')}")

    d = result["data"]

    # Get proxied download URL (accessible from any IP)
    play_url = d.get("hdplay") or d.get("play") or ""
    if play_url and not play_url.startswith("http"):
        play_url = "https://tikwm.com" + play_url

    if not play_url:
        raise RuntimeError("Could not get TikTok download URL")

    return {
        "title": d.get("title", "Untitled"),
        "creator": (d.get("author") or {}).get("nickname", "Unknown"),
        "thumbnail_url": d.get("origin_cover") or d.get("cover", ""),
        "duration": int(d.get("duration", 0)),
        "description": d.get("title", ""),
        "direct_url": play_url,
    }


def get_ytdlp_info(url: str) -> dict:
    """Get video info and direct URL via yt-dlp (for YouTube, etc.)."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "no_check_formats": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Get direct media URL
    direct_url = info.get("url", "")
    if not direct_url:
        formats = info.get("formats", [])
        if formats:
            audio_fmts = [
                f for f in formats if f.get("acodec", "none") != "none"
            ]
            if audio_fmts:
                direct_url = audio_fmts[-1].get("url", "")
            else:
                direct_url = formats[-1].get("url", "")

    if not direct_url:
        raise RuntimeError("Could not extract media URL from yt-dlp")

    return {
        "title": info.get("title", "Untitled"),
        "creator": info.get("uploader", info.get("creator", "Unknown")),
        "thumbnail_url": info.get("thumbnail", ""),
        "duration": int(info.get("duration", 0)),
        "description": info.get("description", ""),
        "direct_url": direct_url,
    }


def get_video_info(url: str) -> dict:
    """Route to the right extractor based on URL."""
    if "tiktok.com" in url:
        return get_tiktok_info(url)
    else:
        return get_ytdlp_info(url)


IAB_SUPPORTED_LANGUAGES = {"de", "en", "es", "fr", "hi", "it", "nl", "pt"}


def submit_to_assemblyai(audio_url: str) -> str:
    """Submit audio URL to AssemblyAI for transcription.

    First attempts with iab_categories enabled. If AssemblyAI rejects the
    request due to an unsupported language, retries without iab_categories.
    """
    import urllib.request
    import urllib.error

    def _submit(payload: dict) -> str:
        req_data = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.assemblyai.com/v2/transcript",
            data=req_data,
            headers={
                "Authorization": ASSEMBLYAI_KEY,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result["id"]

    payload = {
        "audio_url": audio_url,
        "speech_models": ["universal-2"],
        "iab_categories": True,
        "auto_chapters": True,
    }

    try:
        return _submit(payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        # If the error is about iab_categories language support, retry without it
        if "iab_categories" in body.lower() and "language" in body.lower():
            payload["iab_categories"] = False
            try:
                return _submit(payload)
            except urllib.error.HTTPError as e2:
                body2 = e2.read().decode()
                raise RuntimeError(f"AssemblyAI error {e2.code}: {body2[:500]}")
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

            # Validate env vars
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

            # Step 1: Get video info + download URL
            step = "getting video info"
            info = get_video_info(url)
            title = info["title"]
            creator = info["creator"]
            thumbnail_url = info["thumbnail_url"]
            duration = info["duration"]
            description = info["description"]
            direct_url = info["direct_url"]

            # Step 2: Submit to AssemblyAI
            step = "submitting to AssemblyAI"
            aai_id = submit_to_assemblyai(direct_url)

            # Get optional notes, attachments, and rating
            notes = body.get("notes", "").strip() or None
            rating = body.get("rating")
            if rating is not None:
                rating = int(rating)
                if rating < 1 or rating > 5:
                    rating = None
            attachments = body.get("attachments", None)
            # Validate attachments (max 5, max 2MB each)
            if attachments:
                attachments = attachments[:5]
                for att in attachments:
                    if len(att.get("data", "")) > 2_800_000:  # ~2MB base64
                        att["data"] = att["data"][:100] + "...[truncated]"

            # Step 3: Save to Supabase
            step = "saving to Supabase"
            from supabase import create_client
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            insert_data = {
                "url": url,
                "title": title,
                "creator": creator,
                "thumbnail_url": thumbnail_url,
                "duration": duration,
                "description": description,
                "status": "processing",
                "assemblyai_id": aai_id,
            }
            if notes:
                insert_data["notes"] = notes
            if rating is not None:
                insert_data["rating"] = rating
            if attachments:
                insert_data["attachments"] = attachments

            record = sb.table("transcripts").insert(insert_data).execute()

            row_id = record.data[0]["id"]

            self._respond(200, {
                "id": row_id,
                "assemblyai_id": aai_id,
                "title": title,
                "status": "processing",
            })

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed at '{step}': {str(e)}\n{tb}", file=sys.stderr)
            error_msg = f"Failed at '{step}': {str(e)}"
            self._respond(500, {"error": error_msg})

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
