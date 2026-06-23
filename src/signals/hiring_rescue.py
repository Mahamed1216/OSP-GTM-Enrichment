"""C-tier hiring rescue — batch runner + CLI.

Processes ONLY:
  - the given workspace,
  - C-tier leads,
  - leads missing hiring-signal enrichment (unless --force re-runs them),
  - up to `limit` leads (default 25).

CLI:
    python -m src.signals.hiring_rescue --workspace-id 1 --limit 25 [--force]

Never sends email and never pushes to Instantly — it only researches the
hiring signal, stores it, and applies a local tier uplift.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from src.db import session_scope
from src.models import LeadSignal, Score
from src.signals.hiring import enrich_hiring_signal
from src.signals.store import HIRING

log = logging.getLogger(__name__)


@dataclass
class RescueReport:
    workspace_id: int
    limit: int
    force: bool = False
    processed_count: int = 0
    high_signal_count: int = 0
    medium_signal_count: int = 0
    low_signal_count: int = 0
    no_signal_count: int = 0
    bumped_to_B_count: int = 0
    bumped_to_A_count: int = 0
    skipped_count: int = 0
    errors: int = 0
    error_detail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def select_c_tier_lead_ids(
    workspace_id: int,
    *,
    limit: int = 25,
    force: bool = False,
) -> list[int]:
    """Return C-tier lead ids in the workspace for the rescue run.

    Without `force`, leads that already have a hiring LeadSignal are excluded
    (don't waste Tavily credits). With `force`, all C-tier leads are eligible.
    """
    with session_scope() as session:
        stmt = (
            select(Score.lead_id)
            .where(Score.tier == "C")
            .where(Score.workspace_id == workspace_id)
            .order_by(Score.lead_id.desc())
        )
        if not force:
            existing = select(LeadSignal.lead_id).where(
                LeadSignal.signal_type == HIRING,
                LeadSignal.workspace_id == workspace_id,
            )
            stmt = stmt.where(Score.lead_id.notin_(existing))
        rows = session.execute(stmt.limit(limit)).scalars().all()
        return [int(r) for r in rows]


async def run_hiring_rescue(
    workspace_id: int,
    *,
    limit: int = 25,
    force: bool = False,
) -> RescueReport:
    """Run the hiring rescue for one workspace and return a report."""
    report = RescueReport(workspace_id=workspace_id, limit=limit, force=force)
    lead_ids = select_c_tier_lead_ids(workspace_id, limit=limit, force=force)

    for lead_id in lead_ids:
        try:
            outcome = await enrich_hiring_signal(
                lead_id, workspace_id=workspace_id, force=force,
            )
        except Exception as exc:  # pragma: no cover - enrich_hiring_signal is graceful
            report.errors += 1
            report.error_detail.append(f"lead {lead_id}: {type(exc).__name__}: {exc}")
            continue

        if outcome.get("error"):
            report.errors += 1
            report.error_detail.append(f"lead {lead_id}: {outcome['error']}")
            continue
        if outcome.get("skipped"):
            report.skipped_count += 1
            continue

        report.processed_count += 1
        strength = (outcome.get("strength") or "none").lower()
        if strength == "high":
            report.high_signal_count += 1
        elif strength == "medium":
            report.medium_signal_count += 1
        elif strength == "low":
            report.low_signal_count += 1
        else:
            report.no_signal_count += 1

        uplift = outcome.get("uplift") or {}
        if uplift.get("applied"):
            if uplift.get("new_tier") == "B":
                report.bumped_to_B_count += 1
            elif uplift.get("new_tier") == "A":
                report.bumped_to_A_count += 1

    log.info("hiring_rescue_complete", extra=report.as_dict())
    return report


def run_hiring_rescue_sync(
    workspace_id: int,
    *,
    limit: int = 25,
    force: bool = False,
) -> RescueReport:
    """Synchronous entry point for the Streamlit UI button."""
    import asyncio

    coro = run_hiring_rescue(workspace_id, limit=limit, force=force)
    try:
        loop = asyncio.get_running_loop()
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _cli_main() -> int:
    import argparse
    import asyncio

    from src.db import init_db
    from src.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="OSP C-tier hiring rescue — run once.")
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--force", action="store_true",
        help="Re-research leads that already have a hiring signal.",
    )
    args = parser.parse_args()

    setup_logging()
    init_db()

    report = asyncio.run(
        run_hiring_rescue(args.workspace_id, limit=args.limit, force=args.force)
    )

    print("\n=== Hiring rescue report ===")
    print(f"workspace_id      : {report.workspace_id}")
    print(f"limit             : {report.limit}  (force={report.force})")
    print(f"processed_count   : {report.processed_count}")
    print(f"high_signal_count : {report.high_signal_count}")
    print(f"medium_signal     : {report.medium_signal_count}")
    print(f"low_signal_count  : {report.low_signal_count}")
    print(f"no_signal_count   : {report.no_signal_count}")
    print(f"bumped_to_B_count : {report.bumped_to_B_count}")
    print(f"bumped_to_A_count : {report.bumped_to_A_count}")
    print(f"skipped_count     : {report.skipped_count}")
    print(f"errors            : {report.errors}")
    for detail in report.error_detail:
        print(f"  - {detail}")
    print("No emails sent. No Instantly push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
