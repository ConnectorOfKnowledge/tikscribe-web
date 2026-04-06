"""API route: Submit a URL for transcription (queue-based, instant return)."""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from api._shared import check_auth, set_cors_headers


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()


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
    """Get video info via yt-dlp (for YouTube, etc.). No download needed."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "no_check_formats": True,
        "socket_timeout": 15,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

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

    return {
        "title": info.get("title", "Untitled"),
        "creator": info.get("uploader", info.get("creator", "Unknown")),
        "thumbnail_url": info.get("thumbnail", ""),
        "duration": int(info.get("duration", 0)),
        "description": info.get("description", ""),
        "direct_url": direct_url or "",
    }


def get_video_info(url: str) -> dict:
    """Route to the right extractor based on URL."""
    if "tiktok.com" in url:
        return get_tiktok_info(url)
    else:
        return get_ytdlp_info(url)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        origin = self.headers.get("Origin")

        auth_error = check_auth(self.headers)
        if auth_error:
            self._respond(401, {"error": auth_error}, origin)
            return

        step = "init"
        try:
            step = "parsing request"
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            url = body.get("url", "").strip()

            if not url:
                self._respond(400, {"error": "URL is required"}, origin)
                return

            step = "checking config"
            if not SUPABASE_URL or not SUPABASE_KEY:
                self._respond(500, {"error": "Server misconfigured"}, origin)
                return

            # Get basic video info (title, creator, thumbnail) -- fast, 2-3s
            step = "getting video info"
            info = get_video_info(url)

            # Get optional notes, attachments, and rating
            notes = body.get("notes", "").strip() or None
            rating = body.get("rating")
            if rating is not None:
                rating = int(rating)
                if rating < 1 or rating > 5:
                    rating = None
            attachments = body.get("attachments", None)
            if attachments:
                attachments = attachments[:5]
                for att in attachments:
                    if len(att.get("data", "")) > 2_800_000:
                        att["data"] = att["data"][:100] + "...[truncated]"

            # Save to Supabase as queued -- processing happens in background cron
            step = "saving to queue"
            from supabase import create_client
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            insert_data = {
                "url": url,
                "title": info["title"],
                "creator": info["creator"],
                "thumbnail_url": info["thumbnail_url"],
                "duration": info["duration"],
                "description": info["description"],
                "direct_url": info["direct_url"],
                "status": "queued",
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
                "title": info["title"],
                "status": "queued",
            }, origin)

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed at '{step}': {str(e)}\n{tb}", file=sys.stderr)
            self._respond(500, {"error": f"Failed at '{step}': {str(e)}"}, origin)

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
