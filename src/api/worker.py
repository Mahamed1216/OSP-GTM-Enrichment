"""Async worker for the internal API.

Processes queued api_runs (created by POST /api/v1/leads/process in async mode)
through the existing pipeline. Run as a long-lived process or a one-shot batch:

    python -m src.api.worker                 # poll loop (default)
    python -m src.api.worker --once          # drain queued runs once and exit
    python -m src.api.worker --poll-seconds 10

Deploy on AWS as an ECS service (loop) or an EventBridge-scheduled task (--once).
Never sends email, never pushes to Instantly — it only orchestrates processing.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from src.api.processing import process_run
from src.api.run_store import list_queued_run_ids

log = logging.getLogger(__name__)


async def drain_once(batch: int = 10) -> int:
    """Process all currently-queued runs once. Returns the count processed."""
    run_ids = list_queued_run_ids(limit=batch)
    for run_id in run_ids:
        try:
            await process_run(run_id)
        except Exception as exc:  # pragma: no cover - process_run captures its own errors
            log.warning("worker_run_error",
                        extra={"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
    return len(run_ids)


def _main() -> None:
    parser = argparse.ArgumentParser(description="OSP internal-API async worker.")
    parser.add_argument("--once", action="store_true", help="Drain queued runs once and exit.")
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--batch", type=int, default=10)
    args = parser.parse_args()

    from src.db import init_db
    init_db()

    if args.once:
        n = asyncio.run(drain_once(batch=args.batch))
        print(f"[OK] processed {n} queued run(s).")
        return

    print(f"[worker] polling every {args.poll_seconds}s — Ctrl-C to stop.")
    while True:
        try:
            n = asyncio.run(drain_once(batch=args.batch))
            if n:
                print(f"[worker] processed {n} run(s).")
        except KeyboardInterrupt:  # pragma: no cover
            print("[worker] stopped.")
            break
        except Exception as exc:  # pragma: no cover
            log.warning("worker_loop_error", extra={"error": f"{type(exc).__name__}: {exc}"})
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    _main()
