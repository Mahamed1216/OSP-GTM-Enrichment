"""Sync wrappers around the async rating + regenerate backend functions.

Streamlit is sync; the backend rating helpers are async for symmetry with the
rest of `src/feedback/*`. We bridge via run_async (which already calls
asyncio.run after nest_asyncio.apply at import time).
"""
from __future__ import annotations

from typing import Any

from app.lib.async_runner import run_async
from src.feedback.ratings import record_rating
from src.feedback.regenerate import regenerate_direct, regenerate_with_feedback
from src.leads import delete_lead


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


def delete_lead_sync(lead_id: int) -> dict[str, Any]:
    """Delete a lead and all dependent rows. Idempotent.

    The backend is already sync (pure SQLAlchemy), so no run_async needed —
    co-located here for symmetry with the other UI helpers.

    Returns the dict from src.leads.delete_lead:
      - success path: {"success": True, "lead_id", "deleted_counts": {...}}
      - missing lead: {"success": False, "lead_id", "reason": "not found"}
    """
    return delete_lead(lead_id)
