"""SalesOS approved-send worker.

Finds SalesOS outbound content a CSM has APPROVED but not yet sent, re-runs every
safety check, and delivers approved leads through Instantly. Blocked leads are
skipped with a clear reason and are NEVER marked sent.

Run:
    python -m src.integrations.salesos.send_approved                 # poll loop
    python -m src.integrations.salesos.send_approved --once          # drain once
    python -m src.integrations.salesos.send_approved --once --limit 10
    python -m src.integrations.salesos.send_approved --workspace-id 3
    python -m src.integrations.salesos.send_approved --client-id acme
    python -m src.integrations.salesos.send_approved --once --dry-run

Only active when SALESOS_INTEGRATION_MODE=true.
"""
from __future__ import annotations

import argparse
import logging
import time

from src.config import settings
from src.integrations.salesos import ensure_salesos_tables
from src.integrations.salesos.sending import send_approved_once

log = logging.getLogger(__name__)


def _main() -> None:
    parser = argparse.ArgumentParser(description="SalesOS approved-send worker.")
    parser.add_argument("--once", action="store_true", help="Drain approved-unsent once and exit.")
    parser.add_argument("--limit", type=int, default=10, help="Max sends per drain.")
    parser.add_argument("--workspace-id", type=int, default=None, help="Scope to one engine workspace.")
    parser.add_argument("--client-id", type=str, default=None, help="Scope to one SalesOS client.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would send; send nothing.")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()

    if not settings.salesos_integration_mode:
        print("[salesos-send] SALESOS_INTEGRATION_MODE is false — refusing to run. "
              "Set SALESOS_INTEGRATION_MODE=true to enable approved sends.")
        return

    from src.db import init_db
    init_db()
    ensure_salesos_tables()

    def _run() -> dict:
        return send_approved_once(
            limit=args.limit, workspace_id=args.workspace_id,
            client_id=args.client_id, dry_run=args.dry_run,
        )

    if args.once or args.dry_run:
        summary = _run()
        print(f"[salesos-send] found={summary['found']} sent={summary['sent']} "
              f"blocked={summary['blocked']}{' (dry-run)' if summary['dry_run'] else ''}")
        return

    print(f"[salesos-send] polling every {args.poll_seconds}s — Ctrl-C to stop.")
    while True:
        try:
            summary = _run()
            if summary["found"]:
                print(f"[salesos-send] found={summary['found']} sent={summary['sent']} "
                      f"blocked={summary['blocked']}")
        except KeyboardInterrupt:  # pragma: no cover
            print("[salesos-send] stopped.")
            break
        except Exception as exc:  # pragma: no cover
            log.warning("salesos_send_loop_error", extra={"error": f"{type(exc).__name__}: {exc}"})
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    _main()
