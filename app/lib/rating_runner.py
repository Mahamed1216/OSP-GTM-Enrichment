"""Sync wrappers around the async rating + regenerate backend functions.

Streamlit is sync; the backend rating helpers are async for symmetry with the
rest of `src/feedback/*`. We bridge via run_async (which already calls
asyncio.run after nest_asyncio.apply at import time).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.lib.async_runner import run_async
from src.content.email import generate_email
from src.db import session_scope
from src.enrichment.waterfall import enrich_lead
from src.feedback.ratings import record_rating
from src.feedback.regenerate import regenerate_direct, regenerate_with_feedback
from src.leads import delete_lead
from src.models import GeneratedContent
from src.scoring import score_lead


def record_rating_sync(
    content_id: int,
    rating: str,
    feedback: str | None,
    rated_by: str | None = None,
) -> int:
    """Record a single rating. Returns the new ContentRating id.

    Raises (let the UI catch and surface via st.error):
      - ValueError on invalid rating value
      - LookupError if the content row does not exist
      - RatingAlreadyExistsError if the row already has a rating
    """
    return run_async(record_rating(content_id, rating, feedback, rated_by=rated_by))


def regenerate_sync(content_id: int) -> int:
    """Regenerate content with negative feedback prepended. Returns the new content id.

    Raises (let the UI catch and surface via st.error):
      - LookupError if the source row does not exist
      - RegenerateRefused if preconditions fail (already superseded, no rating,
        wrong rating direction, or empty feedback)
    """
    return run_async(regenerate_with_feedback(content_id))


def regenerate_direct_sync(content_id: int, feedback: str) -> int:
    """Redo content with an ad-hoc feedback string — no rating required.

    Used by the Lead Detail "Redo with feedback" textbox so the operator
    can re-roll an email without first submitting a thumbs-down rating.
    Returns the new content id; old row's superseded_by_id is set.
    """
    return run_async(regenerate_direct(content_id, feedback))


def rerun_enrichment_sync(lead_id: int, *, workspace_id: int | None = None) -> dict[str, Any]:
    """Re-run enrichment for a single lead. Returns the source_status dict.

    Wraps `src.enrichment.waterfall.enrich_lead`. Per-source errors are
    captured inside the waterfall and reported via the returned dict;
    only catastrophic failures (lead not found, DB unavailable) raise.
    """
    return run_async(enrich_lead(lead_id, workspace_id=workspace_id))


def rerun_scoring_sync(lead_id: int, *, workspace_id: int | None = None) -> dict[str, Any]:
    """Re-run scoring for a single lead. Returns a small summary dict
    ({score, tier}) so the UI can render the change without re-querying.

    `score_lead` upserts the Score row so re-running on an already-scored
    lead replaces the old score deterministically.
    """
    result = run_async(score_lead(lead_id, workspace_id=workspace_id))
    return {
        "score": int(result.score),
        "tier": result.tier,
        "rationale": result.rationale,
        "signals_used": list(result.signals_used or []),
    }


def regenerate_email_sync(lead_id: int, *, workspace_id: int | None = None) -> int:
    """Re-generate email for a single lead (no feedback). Returns the new
    GeneratedContent id.

    Mirrors the "Redo with feedback" semantics but skips the user-text
    argument — used by the Lead Actions panel's "Regenerate email"
    button. Any existing non-superseded email head row is marked
    superseded by the new row so the UI's head pointer flips to the
    fresh version automatically.
    """
    # Snapshot the current head email row (if any) so we can wire up
    # supersede AFTER the new row lands. Done in a single short session
    # to avoid holding a transaction open across the LLM call.
    with session_scope() as session:
        head = session.execute(
            select(GeneratedContent)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "email",
                GeneratedContent.superseded_by_id.is_(None),
            )
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        old_head_id = head.id if head else None

    run_async(generate_email(lead_id, workspace_id=workspace_id))

    # Identify the row we just wrote (newest non-superseded email for
    # this lead that isn't the old head) and wire supersede.
    with session_scope() as session:
        new_row = session.execute(
            select(GeneratedContent)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "email",
                GeneratedContent.superseded_by_id.is_(None),
            )
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        if new_row is None:
            raise RuntimeError(
                f"generate_email did not produce a row for lead {lead_id}"
            )
        new_id = int(new_row.id)
        if old_head_id is not None and old_head_id != new_id:
            old = session.get(GeneratedContent, old_head_id)
            if old is not None and old.superseded_by_id is None:
                old.superseded_by_id = new_id
    return new_id


def full_refresh_sync(lead_id: int, *, workspace_id: int | None = None) -> dict[str, Any]:
    """Enrichment → scoring → email regeneration for one lead.

    Stops the chain at the first failure and reports which step failed
    so the UI can surface a precise error. The completed steps are NOT
    rolled back — re-running picks up from the failed step on the next
    click.
    """
    out: dict[str, Any] = {"enrichment": None, "scoring": None, "email_id": None}
    try:
        out["enrichment"] = rerun_enrichment_sync(lead_id, workspace_id=workspace_id)
    except Exception as exc:
        out["failed_step"] = "enrichment"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        out["scoring"] = rerun_scoring_sync(lead_id, workspace_id=workspace_id)
    except Exception as exc:
        out["failed_step"] = "scoring"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        out["email_id"] = regenerate_email_sync(lead_id, workspace_id=workspace_id)
    except Exception as exc:
        out["failed_step"] = "email"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    return out


def delete_lead_sync(lead_id: int) -> dict[str, Any]:
    """Delete a lead and all dependent rows. Idempotent.

    The backend is already sync (pure SQLAlchemy), so no run_async needed —
    co-located here for symmetry with the other UI helpers.

    Returns the dict from src.leads.delete_lead:
      - success path: {"success": True, "lead_id", "deleted_counts": {...}}
      - missing lead: {"success": False, "lead_id", "reason": "not found"}
    """
    return delete_lead(lead_id)
