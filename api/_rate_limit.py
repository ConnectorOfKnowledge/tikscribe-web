"""Rate-limit + circuit-breaker helpers backed by Supabase RPCs.

All functions fail CLOSED on any error. If the DB is unreachable, callers
should reject the request (503) rather than allow it through, because the
protected endpoints trigger paid API work.
"""

import sys


IP_WINDOW_MINUTES = 5
IP_MAX_PER_WINDOW = 5
DAILY_SUBMISSION_CAP = 500


def _rpc_single(sb, name: str, params: dict | None = None):
    """Call an RPC and return the first row (or None). Never raises."""
    try:
        result = sb.rpc(name, params or {}).execute()
        data = result.data
        if isinstance(data, list):
            return data[0] if data else None
        return data
    except Exception as e:
        print(f"[rate_limit] RPC {name} failed: {e}", file=sys.stderr)
        return None


def check_ip_rate_limit(sb, ip: str) -> tuple[bool, int]:
    """Returns (allowed, current_count). Fail-CLOSED."""
    row = _rpc_single(sb, "rate_limit_check", {
        "p_key": f"transcribe:ip:{ip}",
        "p_window_minutes": IP_WINDOW_MINUTES,
        "p_max_count": IP_MAX_PER_WINDOW,
    })
    if not row:
        return False, 0
    return bool(row.get("allowed")), int(row.get("current_count") or 0)


def check_daily_submission_cap(sb) -> tuple[bool, int]:
    """Returns (allowed, current_count). Fail-CLOSED."""
    count = _rpc_single(sb, "daily_submission_count", {})
    if count is None:
        return False, 0
    try:
        count_int = int(count)
    except (TypeError, ValueError):
        return False, 0
    return count_int < DAILY_SUBMISSION_CAP, count_int
