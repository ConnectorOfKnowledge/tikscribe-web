"""One-time backfill: populate direct_url for existing transcripts."""

import os
import json
import urllib.request
import urllib.parse
from dotenv import load_dotenv

load_dotenv(".env.local")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


def get_tiktok_url(url):
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
    if result.get("code") != 0:
        return None
    d = result["data"]
    play_url = d.get("hdplay") or d.get("play") or ""
    if play_url and not play_url.startswith("http"):
        play_url = "https://tikwm.com" + play_url
    return play_url or None


def get_ytdlp_url(url):
    import yt_dlp
    ydl_opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "no_check_formats": True, "socket_timeout": 15,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    direct = info.get("url", "")
    if not direct:
        fmts = info.get("formats", [])
        audio = [f for f in fmts if f.get("acodec", "none") != "none"]
        if audio:
            direct = audio[-1].get("url", "")
        elif fmts:
            direct = fmts[-1].get("url", "")
    return direct or None


def get_direct_url(url):
    if "tiktok.com" in url:
        return get_tiktok_url(url)
    else:
        return get_ytdlp_url(url)


def main():
    from supabase import create_client
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Get all transcripts without a direct_url
    result = sb.table("transcripts").select("id, url").is_("direct_url", "null").execute()
    items = result.data or []
    print(f"Found {len(items)} transcripts without direct_url")

    success = 0
    failed = 0
    for i, item in enumerate(items):
        url = item.get("url", "")
        print(f"[{i+1}/{len(items)}] {url[:60]}...", end=" ")
        try:
            direct = get_direct_url(url)
            if direct:
                sb.table("transcripts").update({"direct_url": direct}).eq("id", item["id"]).execute()
                print("OK")
                success += 1
            else:
                print("NO URL")
                failed += 1
        except Exception as e:
            print(f"FAIL: {e}")
            failed += 1

    print(f"\nDone: {success} updated, {failed} failed")


if __name__ == "__main__":
    main()
