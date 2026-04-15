"""Backfill Gemini visual analysis on recent transcripts that missed it.

Target: rows where created_at >= 2026-04-01 AND visual_status IN
('pending', 'failed'). These are the records submitted after the visual
feature shipped but either never analyzed or analyzed and errored out.
Legacy records (<2026-04-01) are `visual_status='legacy_skip'` and are
deliberately NOT in scope.

Usage:
    # Dry-run: process 5 candidates, print Gemini outputs, DO NOT write to DB.
    python scripts/backfill_visual.py --dry-run

    # Full run: process ALL candidates, write visual_summary + visual_status.
    python scripts/backfill_visual.py --run

    # Override candidate count (dry or full).
    python scripts/backfill_visual.py --run --limit 10

Concurrency cap 3 (Gemini 2.5 Flash throttles aggressively under burst).
Per-record wall-clock cap 120s. Exponential backoff on 429/5xx: 2s, 8s,
30s, then give up on that record. Uses claim_visual_for_processing RPC
to avoid racing with /api/analyze_visual on the same row. If the
process crashes mid-flight, release_stuck_visual_rows resets stranded
rows after 15 min.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import pathlib
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env.local before importing api.* so supabase / gemini keys are ready.
env_path = PROJECT_ROOT / ".env.local"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip())

from supabase import create_client  # noqa: E402
from api.process_queue import (  # noqa: E402
    refresh_download_url,
    run_gemini_analysis,
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

CUTOFF = "2026-04-01"
CONCURRENCY = 3
RECORD_TIMEOUT_S = 120
BACKOFF_SCHEDULE = [2, 8, 30]
DRY_RUN_DEFAULT_LIMIT = 5


@dataclass
class Result:
    id: str
    status: str
    detail: str = ""
    summary_len: int = 0


def fetch_candidates(sb, limit: int | None) -> list[dict]:
    q = (
        sb.table("transcripts")
        .select("id, url, direct_url, visual_status, duration, creator, title, generated_title")
        .in_("visual_status", ["pending", "failed"])
        .gte("created_at", CUTOFF)
        .order("created_at")
    )
    if limit is not None:
        q = q.limit(limit)
    return (q.execute().data) or []


def process_one_record(
    sb, record: dict, dry_run: bool
) -> Result:
    record_id = record["id"]
    original_url = record.get("url") or ""
    label = record.get("generated_title") or record.get("title") or record_id[:8]

    # Refresh the direct_url. TikTok CDN URLs expire in hours so the one
    # stored at submit time is almost certainly dead.
    fresh = refresh_download_url(original_url)
    direct_url = fresh or record.get("direct_url") or ""

    if not direct_url:
        return Result(record_id, "skipped", detail="no direct_url after refresh")

    if not dry_run:
        claim = sb.rpc("claim_visual_for_processing", {"p_id": record_id}).execute()
        claimed = (claim.data or [None])[0] if isinstance(claim.data, list) else claim.data
        if not claimed:
            return Result(record_id, "skipped", detail="not eligible (already processing / terminal / legacy_skip)")

    # Run Gemini with exponential backoff on transient failures.
    last_error = None
    for attempt, backoff_s in enumerate([0] + BACKOFF_SCHEDULE):
        if backoff_s:
            time.sleep(backoff_s)
        try:
            summary = run_gemini_analysis(direct_url)
        except Exception as e:
            summary = None
            last_error = f"{type(e).__name__}: {e}"
        if summary:
            if dry_run:
                print(f"\n--- {label[:60]} ({record_id[:8]}) attempt={attempt} ---")
                print(summary[:600] + ("..." if len(summary) > 600 else ""))
                return Result(record_id, "completed_dry", summary_len=len(summary))
            sb.table("transcripts").update({
                "visual_status": "completed",
                "visual_summary": summary,
                "has_visual_content": True,
                "last_error": None,
            }).eq("id", record_id).execute()
            return Result(record_id, "completed", summary_len=len(summary))
        # run_gemini_analysis swallows errors and returns None. We retry the
        # record a few times in case the fail was transient (network blip,
        # Gemini 429). After the schedule is exhausted, we record failure.

    if dry_run:
        return Result(record_id, "failed_dry", detail=last_error or "no summary")

    sb.table("transcripts").update({
        "visual_status": "failed",
        "last_error": (last_error or "gemini returned empty")[:500],
    }).eq("id", record_id).execute()
    return Result(record_id, "failed", detail=last_error or "gemini returned empty")


def process_with_timeout(
    sb, record: dict, dry_run: bool
) -> Result:
    """Wrap process_one_record with a wall-clock cap.

    All DB writes happen inside the worker. On wall-clock timeout the
    parent does NOT write, to avoid racing a late-finishing worker whose
    success write would be silently clobbered. Rows stuck in
    visual_status='processing' past 15 min get reclaimed by the
    release_stuck_visual_rows pg_cron job — that is the recovery path.
    """
    record_id = record["id"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process_one_record, sb, record, dry_run)
        try:
            return future.result(timeout=RECORD_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            return Result(record_id, "timeout_orphaned",
                          detail=f"worker still running at {RECORD_TIMEOUT_S}s; "
                                 f"release_stuck_visual_rows will reclaim")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"  [{record_id[:8]}] unhandled: {e}\n{tb}", file=sys.stderr)
            return Result(record_id, "failed", detail=f"{type(e).__name__}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Process a small sample and print outputs. No DB writes.")
    group.add_argument("--run", action="store_true",
                       help="Process all candidates and write results to DB.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap candidate count (defaults: 5 for dry-run, all for run).")
    args = parser.parse_args()

    if not GEMINI_KEY:
        print("ERROR: GEMINI_API_KEY not set", file=sys.stderr)
        return 2

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    limit = args.limit
    if args.dry_run and limit is None:
        limit = DRY_RUN_DEFAULT_LIMIT

    candidates = fetch_candidates(sb, limit)
    mode = "DRY-RUN" if args.dry_run else "FULL RUN"
    print(f"{mode}: {len(candidates)} candidates, concurrency={CONCURRENCY}, "
          f"per-record timeout={RECORD_TIMEOUT_S}s")

    if not candidates:
        print("no candidates, done")
        return 0

    t0 = time.time()
    results: list[Result] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(process_with_timeout, sb, rec, args.dry_run): rec
            for rec in candidates
        }
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            rec = futures[future]
            try:
                res = future.result()
            except Exception as e:
                res = Result(rec["id"], "failed", detail=f"executor: {type(e).__name__}: {e}")
            results.append(res)
            elapsed = time.time() - t0
            print(f"  [{i:>3}/{len(candidates)}] {rec['id'][:8]} -> {res.status:<14} "
                  f"(summary={res.summary_len}, elapsed={elapsed:.0f}s) {res.detail[:80]}",
                  flush=True)

    elapsed = time.time() - t0
    tally = {}
    for r in results:
        tally[r.status] = tally.get(r.status, 0) + 1

    print("")
    print(f"=== {mode} complete in {elapsed:.0f}s ===")
    for status, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {status:<16} {n}")
    print(f"  TOTAL            {len(results)}")

    # Acceptance check for full runs: no row stuck in 'processing'.
    if args.run:
        stranded = (
            sb.table("transcripts")
            .select("id", count="exact")
            .eq("visual_status", "processing")
            .gte("created_at", CUTOFF)
            .execute()
        )
        n_stranded = len(stranded.data or [])
        if n_stranded:
            print(f"\nWARNING: {n_stranded} rows left in visual_status='processing'. "
                  f"release_stuck_visual_rows cron will reset them in <=15 min.")
        else:
            print("\nno rows stuck in visual_status='processing' — acceptance met")

    failed = sum(1 for r in results if r.status in ("failed", "failed_dry"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
