"""Regenerate generated content using SDR thumbs-down feedback as guidance.

Creates a NEW GeneratedContent row (does not overwrite). Marks the source
row's `superseded_by_id` to point at the new row. Refuses to operate on rows
that are already superseded — that's the cycle/double-regen guard.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from src.content.call_script import generate_call_script
from src.content.email import generate_email
from src.content.linkedin_msg import generate_linkedin_msg
from src.db import session_scope
from src.models import ContentRating, GeneratedContent

log = logging.getLogger(__name__)


_KIND_DISPATCH = {
    "email": generate_email,
    "call_script": generate_call_script,
    "linkedin_msg": generate_linkedin_msg,
}


class RegenerateRefused(ValueError):
    """Raised when preconditions for regeneration are not met."""


async def regenerate_with_feedback(generated_content_id: int) -> int:
    """Regenerate content using its thumbs-down rating's feedback. Returns new content id.

    Preconditions:
      - source row exists (else LookupError)
      - source.superseded_by_id IS NULL  (cycle/double-regen guard)
      - source has a rating
      - rating.rating == "down"
      - rating.feedback_text is non-empty (after strip)

    Raises RegenerateRefused (subclass of ValueError) on any precondition failure.
    """
    with session_scope() as session:
        source = session.get(GeneratedContent, generated_content_id)
        if source is None:
            raise LookupError(f"GeneratedContent {generated_content_id} not found")

        if source.superseded_by_id is not None:
            raise RegenerateRefused(
                f"GeneratedContent {generated_content_id} is already superseded "
                f"by {source.superseded_by_id}"
            )

        rating = session.execute(
            select(ContentRating).where(
                ContentRating.generated_content_id == generated_content_id
            )
        ).scalar_one_or_none()
        if rating is None:
            raise RegenerateRefused(
                f"GeneratedContent {generated_content_id} has no rating to regenerate from"
            )
        if rating.rating != "down":
            raise RegenerateRefused(
                "Only thumbs-down ratings drive regeneration "
                f"(got rating={rating.rating!r})"
            )
        feedback = (rating.feedback_text or "").strip()
        if not feedback:
            raise RegenerateRefused(
                "Thumbs-down rating has no feedback_text — cannot regenerate without guidance"
            )

        # Pull primitive fields out before leaving the session.
        kind = source.kind
        lead_id = source.lead_id

    generator = _KIND_DISPATCH.get(kind)
    if generator is None:
        raise RegenerateRefused(f"Unknown content kind: {kind!r}")

    log.info(
        "regenerate_started",
        extra={"content_id": generated_content_id, "kind": kind, "lead_id": lead_id},
    )
    await generator(lead_id, regeneration_feedback=feedback)

    # Look up the new row (single-threaded per session, deterministic newest-by-id).
    with session_scope() as session:
        new_row = session.execute(
            select(GeneratedContent)
            .where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == kind,
                GeneratedContent.id != generated_content_id,
                GeneratedContent.superseded_by_id.is_(None),
            )
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()

        if new_row is None:
            # The generator must have just inserted a row; if not, something is very wrong.
            raise RuntimeError(
                f"Generator for kind={kind!r} did not produce a new GeneratedContent row "
                f"for lead {lead_id}"
            )
        new_id = new_row.id

        # Wire the supersede pointer on the source.
        source = session.get(GeneratedContent, generated_content_id)
        source.superseded_by_id = new_id

    log.info(
        "regenerate_complete",
        extra={
            "old_content_id": generated_content_id,
            "new_content_id": new_id,
            "kind": kind,
            "lead_id": lead_id,
        },
    )
    return new_id
