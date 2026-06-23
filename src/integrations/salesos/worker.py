"""SalesOS outbound processing worker.

Polls the shared Supabase ``outbound_jobs`` queue, safely claims queued jobs,
runs each lead through the EXISTING engine pipeline (enrichment, buyer research,
signals, scoring, content), and writes results back to the SalesOS contract
tables. It NEVER sends email or pushes to Instantly — sending is the separate,
approval-gated ``send_approved`` worker.

Run:
    python -m src.integrations.salesos.worker                 # poll loop
    python -m src.integrations.salesos.worker --once          # drain once and exit
    python -m src.integrations.salesos.worker --once --limit 10
    python -m src.integrations.salesos.worker --workspace-id 3
    python -m src.integrations.salesos.worker --client-id acme
    python -m src.integrations.salesos.worker --once --dry-run # list, claim nothing

Only active when SALESOS_INTEGRATION_MODE=true (the worker warns and exits
otherwise, so a standalone deployment can't accidentally run it).
"""
from __future__ import annotations

import argparse
import logging
import time

from src.config import settings
from src.integrations.salesos import ensure_salesos_tables
from src.integrations.salesos.adapter import claim_and_process, list_queued_job_ids

log = logging.getLogger(__name__)


def drain_once(
    *, limit: int = 10, workspace_id: int | None = None,
    client_id: str | None = None, dry_run: bool = False,
) -> dict:
    """Process up to `limit` queued jobs once. Returns a summary dict."""
    job_ids = list_queued_job_ids(limit=limit, workspace_id=workspace_id, client_id=client_id)

    if dry_run:
        for jid in job_ids:
            print(f"[dry-run] would process job {jid}")
        return {"queued": len(job_ids), "processed": 0, "results": [], "dry_run": True}

    results = []
    processed = 0
    for jid in job_ids:
        res = claim_and_process(jid)
        results.append(res)
        if res.get("status") == "completed":
            processed += 1
    return {"queued": len(job_ids), "processed": processed, "results": results, "dry_run": False}


def _main() -> None:
    parser = argparse.ArgumentParser(description="SalesOS outbound processing worker.")
    parser.add_argument("--once", action="store_true", help="Drain queued jobs once and exit.")
    parser.add_argument("--limit", type=int, default=10, help="Max jobs per drain.")
    parser.add_argument("--workspace-id", type=int, default=None, help="Scope to one engine workspace.")
    parser.add_argument("--client-id", type=str, default=None, help="Scope to one SalesOS client.")
    parser.add_argument("--dry-run", action="store_true", help="List claimable jobs; claim/process nothing.")
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()

    if not settings.salesos_integration_mode:
        print("[salesos-worker] SALESOS_INTEGRATION_MODE is false — refusing to run. "
              "Set SALESOS_INTEGRATION_MODE=true to enable the integration worker.")
        return

    from src.db import init_db
    init_db()
    ensure_salesos_tables()

    def _run() -> dict:
        return drain_once(
            limit=args.limit, workspace_id=args.workspace_id,
            client_id=args.client_id, dry_run=args.dry_run,
        )

    if args.once or args.dry_run:
        summary = _run()
        print(f"[salesos-worker] queued={summary['queued']} processed={summary['processed']}"
              f"{' (dry-run)' if summary['dry_run'] else ''}")
        return

    print(f"[salesos-worker] polling every {args.poll_seconds}s — Ctrl-C to stop.")
    while True:
        try:
            summary = _run()
            if summary["queued"]:
                print(f"[salesos-worker] queued={summary['queued']} processed={summary['processed']}")
        except KeyboardInterrupt:  # pragma: no cover
            print("[salesos-worker] stopped.")
            break
        except Exception as exc:  # pragma: no cover
            log.warning("salesos_worker_loop_error", extra={"error": f"{type(exc).__name__}: {exc}"})
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    _main()
