"""API route: Process queued transcripts (called by Vercel cron).

Picks up items with status='queued', runs AssemblyAI audio transcription
and Gemini visual analysis, then marks them completed.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
ASSEMBLYAI_KEY = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
CRON_SECRET = (os.environ.get("CRON_SECRET") or "").strip()


def submit_to_assemblyai(audio_url: str) -> str:
    """Submit audio URL to AssemblyAI for transcription."""
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
        if "iab_categories" in body.lower() and "language" in body.lower():
            payload["iab_categories"] = False
            try:
                return _submit(payload)
            except urllib.error.HTTPError as e2:
                body2 = e2.read().decode()
                raise RuntimeError(f"AssemblyAI error {e2.code}: {body2[:500]}")
        raise RuntimeError(f"AssemblyAI error {e.code}: {body[:500]}")


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

    for label, score in sorted(summary.items(), key=lambda x: x[1], reverse=True):
        if score > 0.3 and len(categories) < 5:
            parts = label.split(">")
            short = parts[-1]
            readable = ""
            for i, c in enumerate(short):
                if c.isupper() and i > 0 and not short[i - 1].isupper():
                    readable += " "
                readable += c
            if readable not in categories:
                categories.append(readable)

    return categories


def run_gemini_analysis(video_url: str) -> str | None:
    """Run full Gemini analysis on a video (audio + visual). Returns summary or None."""
    if not GEMINI_KEY:
        return None

    try:
        from google import genai
        import urllib.request
        import base64

        client = genai.Client(api_key=GEMINI_KEY)

        req = urllib.request.Request(video_url, headers={"User-Agent": "tikscribe/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_length = int(resp.headers.get("Content-Length") or 0)
            if content_length > 20_000_000:
                return None
            video_bytes = resp.read()
            if len(video_bytes) > 20_000_000:
                return None

        video_b64 = base64.b64encode(video_bytes).decode("utf-8")

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {
                    "parts": [
                        {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                        {"text": (
                            "Watch and listen to this entire video. Provide a complete analysis:\n"
                            "1) TRANSCRIPT: Write out everything spoken in the video.\n"
                            "2) ON-SCREEN TEXT: Extract ALL text shown on screen "
                            "(titles, captions, bullet points, slides, URLs).\n"
                            "3) VISUAL DESCRIPTION: Describe what is visually happening.\n"
                            "4) KEY TAKEAWAYS: If there are step-by-step instructions, "
                            "tips, or actionable items, list them.\n"
                            "5) SUMMARY: A concise 2-3 sentence summary of the full content."
                        )},
                    ]
                }
            ],
        )
        return response.text
    except Exception as e:
        print(f"[WARN] Gemini failed ({type(e).__name__}): {e}", file=sys.stderr)
        return None


def submit_one(sb, record: dict) -> dict:
    """Submit a single queued transcript to AssemblyAI. Fast (~1-2s per item).

    Only called for records without an existing assemblyai_id (the handler
    short-circuits records that already have one).
    """
    record_id = record["id"]

    direct_url = record.get("direct_url", "")
    if not direct_url:
        sb.table("transcripts").update({"status": "failed"}).eq("id", record_id).execute()
        return {"id": record_id, "status": "failed", "error": "No download URL"}

    retry_count = (record.get("retry_count") or 0) + 1
    sb.table("transcripts").update({
        "status": "processing",
        "retry_count": retry_count,
    }).eq("id", record_id).execute()

    try:
        aai_id = submit_to_assemblyai(direct_url)
        sb.table("transcripts").update({"assemblyai_id": aai_id}).eq("id", record_id).execute()
    except Exception as e:
        sb.table("transcripts").update({"status": "failed"}).eq("id", record_id).execute()
        return {"id": record_id, "status": "failed", "error": str(e)}

    return {"id": record_id, "status": "submitted", "assemblyai_id": aai_id}


def complete_one(sb, record_id: str, aai_id: str, direct_url: str,
                  time_remaining: float = 999) -> dict:
    """Check AssemblyAI result and finalize a transcript.

    Two-step save: transcript data is written first, then Gemini visual
    analysis is attempted separately. This prevents a Gemini timeout from
    losing the already-completed transcript.
    """
    try:
        aai_result = check_assemblyai(aai_id)
    except Exception as e:
        print(f"[WARN] AssemblyAI check failed ({type(e).__name__}): {e}", file=sys.stderr)
        return {"id": record_id, "status": "check_failed", "error": str(e)}

    if aai_result.get("status") == "error":
        sb.table("transcripts").update({"status": "failed"}).eq("id", record_id).execute()
        return {"id": record_id, "status": "failed", "error": "AssemblyAI transcription failed"}

    if aai_result.get("status") != "completed":
        return {"id": record_id, "status": "still_processing"}

    transcript_text = aai_result.get("text", "")
    categories = extract_categories(aai_result)
    generated_title = ""
    chapters = aai_result.get("chapters", [])
    if chapters:
        generated_title = chapters[0].get("headline", "")

    segments = []
    for chapter in chapters:
        segments.append({
            "start": chapter.get("start", 0) / 1000,
            "end": chapter.get("end", 0) / 1000,
            "text": chapter.get("summary", ""),
            "headline": chapter.get("headline", ""),
        })

    # Step 1: Save transcript data immediately (safe checkpoint)
    sb.table("transcripts").update({
        "status": "completed",
        "transcript": transcript_text,
        "generated_title": generated_title,
        "categories": categories,
        "segments": segments,
        "language": aai_result.get("language_code", "en"),
    }).eq("id", record_id).execute()

    # Step 2: Gemini visual analysis (only if enough time remains)
    if time_remaining > 60:
        visual = run_gemini_analysis(direct_url)
        if visual:
            sb.table("transcripts").update({
                "visual_summary": visual,
                "visual_status": "completed",
                "has_visual_content": True,
            }).eq("id", record_id).execute()
        else:
            sb.table("transcripts").update({
                "visual_status": "skipped" if not GEMINI_KEY else "failed",
            }).eq("id", record_id).execute()
    else:
        sb.table("transcripts").update({
            "visual_status": "deferred",
        }).eq("id", record_id).execute()

    return {"id": record_id, "status": "completed"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Cron endpoint: process queued transcripts.

        Two-phase approach to maximize throughput within the 300s timeout:
        Phase 1 (~10-20s): Submit all queued items to AssemblyAI (instant per item).
        Phase 2 (~250s): Poll submitted items and finalize with Gemini analysis.
        Items not finished in this window stay as 'processing' for the next run.
        """
        import time
        start_time = time.time()

        auth = self.headers.get("Authorization", "")
        if not CRON_SECRET or auth != f"Bearer {CRON_SECRET}":
            self._respond(401, {"error": "Unauthorized"})
            return

        try:
            from supabase import create_client
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            # Fetch all queued + stuck-processing items (up to 20)
            queued = (
                sb.table("transcripts")
                .select("id, url, direct_url, retry_count, assemblyai_id")
                .in_("status", ["queued", "processing"])
                .lt("retry_count", 3)
                .order("created_at")
                .limit(20)
                .execute()
            )

            if not queued.data:
                self._respond(200, {"processed": 0, "message": "No queued items"})
                return

            results = []
            newly_submitted = 0

            # Phase 1: Submit all items to AssemblyAI (fast, ~1-2s each)
            submitted = []  # (record_id, aai_id, direct_url)
            for record in queued.data:
                aai_id = record.get("assemblyai_id")
                if aai_id:
                    submitted.append((record["id"], aai_id, record.get("direct_url", "")))
                    continue

                res = submit_one(sb, record)
                results.append(res)
                if res.get("assemblyai_id"):
                    submitted.append((record["id"], res["assemblyai_id"], record.get("direct_url", "")))
                    newly_submitted += 1

            # Phase 2: Poll and complete submitted items
            # Only sleep if we freshly submitted items (resumed ones are already done)
            if newly_submitted > 0:
                time.sleep(10)

            for record_id, aai_id, direct_url in submitted:
                elapsed = time.time() - start_time
                time_remaining = 300 - elapsed
                if time_remaining < 30:
                    results.append({"id": record_id, "status": "deferred_timeout"})
                    continue

                res = complete_one(sb, record_id, aai_id, direct_url,
                                   time_remaining=time_remaining)
                results.append(res)

                # If still processing, poll up to 6 more times (30s)
                retries = 0
                while res["status"] == "still_processing" and retries < 6:
                    if (time.time() - start_time) > 250:
                        break
                    time.sleep(5)
                    time_remaining = 300 - (time.time() - start_time)
                    res = complete_one(sb, record_id, aai_id, direct_url,
                                       time_remaining=time_remaining)
                    results[-1] = res
                    retries += 1

            self._respond(200, {
                "processed": len([r for r in results if r.get("status") == "completed"]),
                "total": len(results),
                "results": results,
            })

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Queue processing failed: {e}\n{tb}", file=sys.stderr)
            self._respond(500, {"error": str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
