"""Shared helpers for API routes: CORS, auth, URL allowlist."""

import os
from urllib.parse import urlparse


ALLOWED_ORIGINS = {
    "https://tikscribe-web.vercel.app",
    "capacitor://localhost",
    "http://localhost",
}

ALLOWED_URL_HOSTS = {
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
}

TIKSCRIBE_API_KEY = (os.environ.get("TIKSCRIBE_API_KEY") or "").strip()


def get_cors_origin(request_origin: str | None) -> str | None:
    """Return the origin if allowlisted, else None.

    Null/missing origin is blocked (was permissive before — see audit C-1/H-2).
    Capacitor sends `capacitor://localhost` reliably, so legitimate Android
    traffic still passes via the exact-match allowlist.
    """
    if not request_origin or request_origin == "null":
        return None
    if request_origin in ALLOWED_ORIGINS:
        return request_origin
    if request_origin.endswith(".vercel.app") and request_origin.startswith("https://"):
        return request_origin
    return None


def check_auth(headers) -> str | None:
    """Fail-CLOSED bearer check. Returns None on success, error string on failure.

    Previous behavior was fail-open when `TIKSCRIBE_API_KEY` was unset, which
    effectively disabled auth in production. See audit finding C-1.
    """
    if not TIKSCRIBE_API_KEY:
        return "Server auth not configured"

    auth_header = headers.get("Authorization", "")
    if not auth_header:
        return "Missing Authorization header"

    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header.strip()

    if token != TIKSCRIBE_API_KEY:
        return "Invalid API key"

    return None


def set_cors_headers(handler, request_origin: str | None, methods: str = "GET, POST, OPTIONS"):
    origin = get_cors_origin(request_origin)
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", methods)
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")


def is_allowed_url(url) -> bool:
    """HTTPS-only host allowlist for submitted video URLs. Blocks SSRF."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if "@" in (parsed.netloc or ""):
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_URL_HOSTS


def client_ip(headers) -> str:
    """Best-effort client IP on Vercel. Falls back to a sentinel if unknown.

    Per Vercel docs, `x-vercel-forwarded-for` is the recommended header and
    its leftmost entry is the untrusted client. `x-forwarded-for` alone is
    identical to `x-real-ip` on Vercel and both are re-written by the
    platform, so they're safe but less specific.
    """
    raw = (
        headers.get("x-vercel-forwarded-for")
        or headers.get("X-Vercel-Forwarded-For")
        or headers.get("x-forwarded-for")
        or headers.get("X-Forwarded-For")
        or ""
    )
    ip = raw.split(",")[0].strip() if raw else ""
    return ip or "unknown"
