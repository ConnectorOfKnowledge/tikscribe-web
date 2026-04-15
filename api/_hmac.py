"""HMAC-SHA256 signing for internal edge-function → Vercel calls.

Signed string is `{unix_timestamp}.{raw_body}`. Timestamp guards against
replay; body guards against tampering. Fail-closed: any validation error
returns False, never raises to the caller of verify().
"""

import hmac
import hashlib
import time


WINDOW_SECONDS = 300


def sign(secret: str, body: bytes, timestamp: int | None = None) -> tuple[int, str]:
    if not secret:
        raise ValueError("secret must be non-empty")
    ts = int(timestamp) if timestamp is not None else int(time.time())
    message = f"{ts}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return ts, digest


def verify(
    secret: str,
    body: bytes,
    timestamp: int,
    signature: str,
    now: int | None = None,
) -> bool:
    if not secret or not signature or not isinstance(timestamp, int):
        return False
    current = int(now) if now is not None else int(time.time())
    if abs(current - timestamp) > WINDOW_SECONDS:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_with_rotation(
    secrets_in_order: tuple[str, ...],
    body: bytes,
    timestamp: int,
    signature: str,
    now: int | None = None,
) -> bool:
    """Accept a request if ANY of the provided secrets validates it.

    Used during HMAC rotation: primary secret first, then `_NEXT` or any
    historical secret still in the dual-read window. Secrets that are
    empty strings are skipped (so the caller can pass env-var values
    directly without pre-filtering).

    Fail-closed: no secrets provided → False.
    """
    for s in secrets_in_order:
        if not s:
            continue
        if verify(s, body, timestamp, signature, now=now):
            return True
    return False
