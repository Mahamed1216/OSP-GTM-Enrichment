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


async def regenerate_direct(
    generated_content_id: int, feedback_text: str
) -> int:
    """Regenerate using an ad-hoc feedback string — no rating required.

    Backs the Lead Detail "Redo with feedback" textbox. The original
    `regenerate_with_feedback` requires a persisted thumbs-down rating
    with non-empty feedback; that's appropriate for the rating-driven
    learning loop but it locks the operator out after the first rating
    is submitted (rating widget hides; button disappears). This entry
    point sidesteps the rating ledger entirely.

    Preconditions:
      - source row exists (LookupError otherwise)
      - source.superseded_by_id IS NULL (cycle/double-regen guard — same
        as the rating-driven path; we still set the supersede pointer)
      - feedback_text is non-empty after strip
    The same `_KIND_DISPATCH` generator runs, so it picks up the LIVE DB
    prompt overlay (no caching) and stamps the current prompt fingerprint
    on the new row.
    """
    feedback = (feedback_text or "").strip()
    if not feedback:
        raise RegenerateRefused("Empty feedback — provide guidance for the rewrite.")

    with session_scope() as session:
        source = session.get(GeneratedContent, generated_content_id)
        if source is None:
            raise LookupError(f"GeneratedContent {generated_content_id} not found")
        if source.superseded_by_id is not None:
            raise RegenerateRefused(
                f"GeneratedContent {generated_content_id} is already superseded "
                f"by {source.superseded_by_id}"
            )
        kind = source.kind
        lead_id = source.lead_id
        workspace_id = source.workspace_id  # inherit workspace from source row

    generator = _KIND_DISPATCH.get(kind)
    if generator is None:
        raise RegenerateRefused(f"Unknown content kind: {kind!r}")

    log.info(
        "regenerate_direct_started",
        extra={"content_id": generated_content_id, "kind": kind, "lead_id": lead_id},
    )
    await generator(lead_id, regeneration_feedback=feedback, workspace_id=workspace_id)

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
            raise RuntimeError(
                f"Generator for kind={kind!r} did not produce a new GeneratedContent row "
                f"for lead {lead_id}"
            )
        new_id = new_row.id
        source = session.get(GeneratedContent, generated_content_id)
        source.superseded_by_id = new_id

    log.info(
        "regenerate_direct_complete",
        extra={
            "old_content_id": generated_content_id,
            "new_content_id": new_id,
            "kind": kind,
            "lead_id": lead_id,
        },
    )
    return new_id


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
        workspace_id = source.workspace_id  # inherit workspace from source row

    generator = _KIND_DISPATCH.get(kind)
    if generator is None:
        raise RegenerateRefused(f"Unknown content kind: {kind!r}")

    log.info(
        "regenerate_started",
        extra={"content_id": generated_content_id, "kind": kind, "lead_id": lead_id},
    )
    await generator(lead_id, regeneration_feedback=feedback, workspace_id=workspace_id)

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
