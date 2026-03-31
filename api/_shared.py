"""Shared helpers for API routes: CORS and auth."""

import os

# Allowed origins for CORS
ALLOWED_ORIGINS = [
    "https://tikscribe-web.vercel.app",
    "capacitor://localhost",
    "http://localhost",
]

TIKSCRIBE_API_KEY = (os.environ.get("TIKSCRIBE_API_KEY") or "").strip()


def get_cors_origin(request_origin: str | None) -> str | None:
    """Return the origin if it's allowed, or None.

    Allows:
    - The production domain exactly
    - Any Vercel preview deployment (*.vercel.app)
    """
    if not request_origin:
        return None

    # Exact match on allowed list
    if request_origin in ALLOWED_ORIGINS:
        return request_origin

    # Allow Vercel preview deployments
    if request_origin.endswith(".vercel.app") and request_origin.startswith("https://"):
        return request_origin

    return None


def check_auth(headers) -> str | None:
    """Validate the Authorization header.

    Returns None if auth is valid, or an error message string if not.
    """
    if not TIKSCRIBE_API_KEY:
        # If no API key is configured, skip auth (avoids locking out
        # during initial setup before the env var is added)
        return None

    auth_header = headers.get("Authorization", "")
    if not auth_header:
        return "Missing Authorization header"

    # Accept "Bearer <key>" format
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    else:
        token = auth_header.strip()

    if token != TIKSCRIBE_API_KEY:
        return "Invalid API key"

    return None


def set_cors_headers(handler, request_origin: str | None, methods: str = "GET, POST, OPTIONS"):
    """Set CORS headers on a BaseHTTPRequestHandler response."""
    origin = get_cors_origin(request_origin)
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", methods)
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
