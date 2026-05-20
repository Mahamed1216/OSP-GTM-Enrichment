"""SDR ratings on generated content. One rating per content row (re-rating rejected)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, select

from src.config import settings
from src.db import session_scope
from src.models import ContentRating, GeneratedContent, Score

log = logging.getLogger(__name__)

Rating = Literal["up", "down"]
_VALID_RATINGS = ("up", "down")


class RatingAlreadyExistsError(ValueError):
    """Raised when a content row already has a rating. UI-side guard prevents this
    in normal use (buttons disable after rating); the backend still rejects as
    defense in depth so process_ratings idempotency stays simple."""


async def record_rating(
    generated_content_id: int,
    rating: str,
    feedback_text: str | None,
    rated_by: str | None = None,
) -> int:
    """Insert a single rating for a GeneratedContent row.

    Returns the new ContentRating row id. Raises:
      - ValueError on invalid rating value
      - LookupError if the content row does not exist
      - RatingAlreadyExistsError if the row is already rated
    """
    if rating not in _VALID_RATINGS:
        raise ValueError(f"rating must be one of {_VALID_RATINGS}, got {rating!r}")

    rated_by = rated_by or settings.rater_id
    feedback_text = (feedback_text or "").strip() or None

    with session_scope() as session:
        target = session.get(GeneratedContent, generated_content_id)
        if target is None:
            raise LookupError(f"GeneratedContent {generated_content_id} not found")

        existing = session.execute(
            select(ContentRating).where(
                ContentRating.generated_content_id == generated_content_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise RatingAlreadyExistsError(
                f"GeneratedContent {generated_content_id} already rated"
            )

        row = ContentRating(
            generated_content_id=generated_content_id,
            rating=rating,
            feedback_text=feedback_text,
            rated_by=rated_by,
        )
        session.add(row)
        session.flush()
        new_id = row.id

    log.info(
        "rating_recorded",
        extra={
            "rating_id": new_id,
            "content_id": generated_content_id,
            "rating": rating,
            "has_feedback": feedback_text is not None,
        },
    )
    return new_id


def get_rating(generated_content_id: int) -> dict | None:
    """Return the rating dict for a content row, or None if unrated."""
    with session_scope() as session:
        row = session.execute(
            select(ContentRating).where(
                ContentRating.generated_content_id == generated_content_id
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "generated_content_id": row.generated_content_id,
            "rating": row.rating,
            "feedback_text": row.feedback_text,
            "rated_by": row.rated_by,
            "rated_at": row.rated_at,
        }


def get_unrated_content(
    content_type: str | None = None,
    tier: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return unrated, non-superseded content rows for the review queue.

    Ordered by tier (A → B → C → unscored), then created_at ascending (oldest first).
    """
    tier_priority = case(
        (Score.tier == "A", 0),
        (Score.tier == "B", 1),
        (Score.tier == "C", 2),
        else_=3,
    )

    stmt = (
        select(
            GeneratedContent.id,
            GeneratedContent.lead_id,
            GeneratedContent.kind,
            GeneratedContent.subject,
            GeneratedContent.created_at,
            Score.tier,
        )
        .outerjoin(ContentRating, ContentRating.generated_content_id == GeneratedContent.id)
        .outerjoin(Score, Score.lead_id == GeneratedContent.lead_id)
        .where(
            and_(
                GeneratedContent.superseded_by_id.is_(None),
                ContentRating.id.is_(None),
            )
        )
        .order_by(tier_priority.asc(), GeneratedContent.created_at.asc())
        .limit(limit)
    )

    if content_type is not None:
        stmt = stmt.where(GeneratedContent.kind == content_type)
    if tier is not None:
        stmt = stmt.where(Score.tier == tier)

    with session_scope() as session:
        rows = session.execute(stmt).all()

    return [
        {
            "id": r.id,
            "lead_id": r.lead_id,
            "kind": r.kind,
            "subject": r.subject,
            "created_at": r.created_at,
            "tier": r.tier,
        }
        for r in rows
    ]


def get_rating_trends(days: int = 30) -> dict[str, list[dict]]:
    """Per-content-type daily up_rate over the last `days`.

    Returns {kind: [{date, up_count, down_count, up_rate}, ...]} sorted by date asc.
    Empty days are omitted (caller decides how to fill).
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(
            GeneratedContent.kind,
            func.date(ContentRating.rated_at).label("day"),
            func.sum(case((ContentRating.rating == "up", 1), else_=0)).label("up"),
            func.sum(case((ContentRating.rating == "down", 1), else_=0)).label("down"),
        )
        .join(GeneratedContent, GeneratedContent.id == ContentRating.generated_content_id)
        .where(ContentRating.rated_at >= cutoff)
        .group_by(GeneratedContent.kind, func.date(ContentRating.rated_at))
        .order_by(GeneratedContent.kind.asc(), func.date(ContentRating.rated_at).asc())
    )

    out: dict[str, list[dict]] = {}
    with session_scope() as session:
        rows = session.execute(stmt).all()

    for kind, day, up, down in rows:
        up_n = int(up or 0)
        down_n = int(down or 0)
        total = up_n + down_n
        out.setdefault(kind, []).append(
            {
                "date": day,
                "up_count": up_n,
                "down_count": down_n,
                "up_rate": (up_n / total) if total else 0.0,
            }
        )
    return out
