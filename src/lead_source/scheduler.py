"""Evergreen lead source scheduler — import + score + content for each enabled workspace.

Scheduling is deliberately external. Streamlit Cloud cannot reliably run
background jobs. Use one of:

  Option A — CLI (Render Cron / GitHub Actions):
      python -m src.lead_source.scheduler [--workspace-id N] [--dry-run]

  Option B — HTTP endpoint on the existing webhook server:
      POST /api/lead-source/run-scheduled
      Header: X-Job-Secret: <LEAD_SOURCE_JOB_SECRET env var>

Hard constraints (never relaxed):
  - Enrichment is NEVER called. The OSP Lead Engine has already enriched
    these contacts; calling our providers would burn credits needlessly.
  - No emails are auto-sent.
  - No Instantly push.
  - POST /api/v1/clients/{slug}/runs is never called.
  - Only enabled workspaces are touched.
  - Dedup prevents re-importing existing contacts.
  - Content generation is idempotent — skips leads that already have content.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# Name logged alongside every import/processing record.
EXTERNAL_SOURCE = "osp_lead_engine"


# ---------------------------------------------------------------------------
# Processing — scoring + content only, enrichment explicitly skipped
# ---------------------------------------------------------------------------

async def _score_one(lead_id: int, workspace_id: int) -> bool:
    """Score a single lead. Returns True on success."""
    from src.scoring import score_lead
    try:
        await score_lead(lead_id, workspace_id=workspace_id)
        return True
    except Exception as exc:
        log.warning(
            "auto_score_failed",
            extra={"lead_id": lead_id, "workspace_id": workspace_id, "error": str(exc)},
        )
        return False


async def _generate_content_one(lead_id: int, workspace_id: int) -> int:
    """Generate email + call_script + linkedin_msg for one lead.

    Each generator is idempotent — it silently skips if content already exists.
    Returns the count of successfully generated artifacts (0–3).
    """
    from src.content.email import generate_email
    from src.content.call_script import generate_call_script
    from src.content.linkedin_msg import generate_linkedin_msg

    generated = 0
    for fn, name in [
        (generate_email, "email"),
        (generate_call_script, "call_script"),
        (generate_linkedin_msg, "linkedin_msg"),
    ]:
        try:
            await fn(lead_id, workspace_id=workspace_id)
            generated += 1
        except Exception as exc:
            log.warning(
                "auto_content_failed",
                extra={
                    "lead_id": lead_id,
                    "workspace_id": workspace_id,
                    "kind": name,
                    "error": str(exc),
                },
            )
    return generated


async def process_imported_leads(
    lead_ids: list[int],
    workspace_id: int,
    *,
    import_log_id: int | None = None,
) -> dict[str, int]:
    """Score and generate content for newly imported leads.

    Enrichment is NEVER called — the external source is the enrichment provider.
    Content generation is idempotent (skips leads that already have content).

    Returns counts for logging/display.
    """
    from src.lead_source.ingest import _update_import_log_processing

    scored = 0
    content = 0
    enrichment_skipped = len(lead_ids)  # 100% of imported leads skip enrichment

    for lead_id in lead_ids:
        if await _score_one(lead_id, workspace_id):
            scored += 1
        content += await _generate_content_one(lead_id, workspace_id)

    log.info(
        "auto_process_complete",
        extra={
            "workspace_id": workspace_id,
            "lead_count": len(lead_ids),
            "scored": scored,
            "content": content,
            "enrichment_skipped": enrichment_skipped,
        },
    )

    if import_log_id is not None:
        _update_import_log_processing(
            import_log_id,
            scored_count=scored,
            content_generated_count=content,
            enrichment_skipped_count=enrichment_skipped,
        )

    return {
        "scored_count": scored,
        "content_generated_count": content,
        "enrichment_skipped_count": enrichment_skipped,
    }


# ---------------------------------------------------------------------------
# Per-workspace scheduler
# ---------------------------------------------------------------------------

async def run_workspace_auto_import(
    workspace_id: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one scheduled import cycle for a single workspace.

    Returns a summary dict with counts for logging and the HTTP response.
    """
    from src.lead_source.settings import (
        load_lead_source_config,
        update_auto_run_metadata,
        advance_import_cursor,
    )
    from src.lead_source.ingest import run_import

    cfg = load_lead_source_config(workspace_id)

    if not cfg.auto_import_enabled:
        log.info(
            "scheduler_workspace_skipped",
            extra={"workspace_id": workspace_id, "reason": "auto_import_disabled"},
        )
        return {"workspace_id": workspace_id, "skipped": True, "reason": "auto_import_disabled"}

    if not (cfg.api_base_url and cfg.client_slug and cfg.api_key):
        log.warning(
            "scheduler_workspace_skipped",
            extra={"workspace_id": workspace_id, "reason": "incomplete_config"},
        )
        return {"workspace_id": workspace_id, "skipped": True, "reason": "incomplete_config"}

    # Snapshot the cursor BEFORE the import so we can report it in the summary
    # and advance it correctly after. Manual imports (initial_offset=0) never
    # touch this field; only scheduled runs use and update it.
    current_offset = cfg.next_offset

    summary: dict[str, Any] = {
        "workspace_id": workspace_id,
        "skipped": False,
        "dry_run": dry_run,
        "offset_used": current_offset,
        "fetched": 0,
        "created": 0,
        "updated": 0,
        "import_skipped": 0,
        "errors": 0,
        "scored_count": 0,
        "content_generated_count": 0,
        "enrichment_skipped_count": 0,
        "next_offset": current_offset,
    }

    if dry_run:
        log.info("scheduler_dry_run", extra={"workspace_id": workspace_id})
        return summary

    try:
        import_result = run_import(
            workspace_id,
            cfg.client_slug,
            cfg.api_base_url,
            cfg.api_key,
            limit=cfg.daily_fetch_limit,
            icp=cfg.default_icp or None,
            status_filter=cfg.default_status_filter or None,
            include_suppressed=cfg.include_suppressed,
            auto_run=True,
            initial_offset=current_offset,
        )
        summary["fetched"] = import_result.fetched
        summary["created"] = import_result.created
        summary["updated"] = import_result.updated
        summary["import_skipped"] = import_result.skipped
        summary["errors"] = import_result.errors

        # Advance the cursor for the next scheduled run
        new_offset = advance_import_cursor(
            workspace_id,
            fetched_count=import_result.fetched,
            limit=cfg.daily_fetch_limit,
        )
        summary["next_offset"] = new_offset
        log.info(
            "scheduler_cursor_advanced",
            extra={
                "workspace_id": workspace_id,
                "offset_used": current_offset,
                "fetched": import_result.fetched,
                "next_offset": new_offset,
            },
        )

        if cfg.auto_process_enabled and import_result.created_lead_ids:
            process_result = await process_imported_leads(
                import_result.created_lead_ids,
                workspace_id,
                import_log_id=import_result.import_log_id,
            )
            summary.update(process_result)

        status = "completed"
    except Exception as exc:
        status = "failed"
        summary["error_detail"] = str(exc)
        log.warning(
            "scheduler_workspace_failed",
            extra={"workspace_id": workspace_id, "error": str(exc)},
        )

    update_auto_run_metadata(
        workspace_id,
        status=status,
        created=summary["created"],
        scored=summary.get("scored_count", 0),
        content=summary.get("content_generated_count", 0),
        fetched=summary["fetched"],
        skipped=summary["import_skipped"],
    )
    return summary


# ---------------------------------------------------------------------------
# Multi-workspace orchestration
# ---------------------------------------------------------------------------

async def run_all_enabled_workspaces(
    *,
    workspace_id: int | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run the scheduler for all active workspaces that have auto_import enabled.

    If `workspace_id` is specified, only that workspace is processed.
    """
    from src.workspace import get_active_workspaces
    from src.lead_source.settings import load_lead_source_config

    if workspace_id is not None:
        return [await run_workspace_auto_import(workspace_id, dry_run=dry_run)]

    workspaces = get_active_workspaces()
    results = []
    for ws in workspaces:
        ws_id = ws["id"]
        cfg = load_lead_source_config(ws_id)
        if cfg.auto_import_enabled:
            result = await run_workspace_auto_import(ws_id, dry_run=dry_run)
            results.append(result)
        else:
            log.debug("scheduler_skip_disabled", extra={"workspace_id": ws_id})

    log.info(
        "scheduler_all_workspaces_done",
        extra={"processed": len(results), "dry_run": dry_run},
    )
    return results


# ---------------------------------------------------------------------------
# Sync wrapper — for Streamlit UI and CLI
# ---------------------------------------------------------------------------

def run_import_and_process_sync(workspace_id: int) -> dict[str, Any]:
    """Synchronous entry point for the Streamlit UI button.

    Runs import + auto-process. Applies nest_asyncio when an existing event
    loop is running (Streamlit context), falls back to asyncio.run() otherwise.
    """
    import asyncio

    coro = run_workspace_auto_import(workspace_id)
    try:
        loop = asyncio.get_running_loop()
        # Already inside a running loop (e.g. Streamlit) — use nest_asyncio
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except RuntimeError:
        # No running loop — use asyncio.run()
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# CLI entry point (Option A)
# ---------------------------------------------------------------------------

def _cli_main() -> None:
    """python -m src.lead_source.scheduler [--workspace-id N] [--dry-run]"""
    import argparse
    import asyncio
    from src.db import init_db

    parser = argparse.ArgumentParser(
        description="OSP Lead Source evergreen scheduler — run-once mode."
    )
    parser.add_argument("--workspace-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()

    results = asyncio.run(
        run_all_enabled_workspaces(
            workspace_id=args.workspace_id,
            dry_run=args.dry_run,
        )
    )

    for r in results:
        ws = r.get("workspace_id")
        if r.get("skipped"):
            print(f"[SKIP] workspace={ws}  reason={r.get('reason')}")
        else:
            print(
                f"[OK]   workspace={ws}  "
                f"created={r.get('created',0)}  "
                f"scored={r.get('scored_count',0)}  "
                f"content={r.get('content_generated_count',0)}  "
                f"enrichment_skipped={r.get('enrichment_skipped_count',0)}"
            )

    if not results:
        print("[INFO] No workspaces had auto_import_enabled=true.")


if __name__ == "__main__":
    _cli_main()
