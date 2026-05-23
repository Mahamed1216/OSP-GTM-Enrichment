"""Lead-level operations: deletion with cascade across all dependent tables.

`delete_lead` removes a lead and every row that references it — directly
(Enrichment, Score, GeneratedContent) or transitively (ContentRating via
content, Engagement via content). Wrapped in a single transaction so the
operation is all-or-nothing.

Engagement has no SQLAlchemy cascade configured on its FK to
GeneratedContent, and GeneratedContent.superseded_by_id is a self-FK
with no `ondelete` clause. Both are handled explicitly below so the
delete works correctly under SQLite (FKs off by default) and Postgres
(FKs enforced).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from src.db import engine, session_scope
from src.models import (
    ContentRating,
    Engagement,
    Enrichment,
    GeneratedContent,
    Lead,
    Score,
)

log = logging.getLogger(__name__)


def reset_lead_sequence(session: Session) -> bool:
    """Reset the leads-id auto-increment to 1 if (and only if) the table is empty.

    Postgres: ``ALTER SEQUENCE leads_id_seq RESTART WITH 1``.
    SQLite:   ``DELETE FROM sqlite_sequence WHERE name='leads'`` (only effective
              for AUTOINCREMENT columns; otherwise a harmless no-op).

    Dialect is read from the bound engine rather than ``DATABASE_URL`` so this
    stays correct if a future runtime overrides the URL but the dialect is
    locked elsewhere. Caller owns the transaction — no ``session.commit()``
    here; the enclosing ``session_scope`` (or caller's explicit commit) carries it.

    Returns True if a reset was issued, False if the table was non-empty.
    """
    count = session.execute(select(func.count(Lead.id))).scalar() or 0
    if count > 0:
        return False

    dialect = engine.dialect.name
    if dialect == "postgresql":
        session.execute(text("ALTER SEQUENCE leads_id_seq RESTART WITH 1"))
    elif dialect == "sqlite":
        session.execute(text("DELETE FROM sqlite_sequence WHERE name='leads'"))
    else:
        # Unknown dialect — log and skip rather than guess.
        log.warning("reset_lead_sequence_skipped", extra={"dialect": dialect})
        return False
    log.info("reset_lead_sequence", extra={"dialect": dialect})
    return True


def delete_lead(lead_id: int) -> dict[str, Any]:
    """Delete a lead and every dependent row. Idempotent.

    Returns:
        On success:
            {"success": True, "lead_id": int,
             "deleted_counts": {"leads": 1, "enrichments": int, "scores": int,
                                "generated_contents": int, "content_ratings": int,
                                "engagements": int}}
        On missing lead:
            {"success": False, "lead_id": int, "reason": "not found"}

    Raises only on genuinely unexpected DB errors; the missing-lead
    case is a normal idempotent return, not an exception.
    """
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return {"success": False, "lead_id": lead_id, "reason": "not found"}

        content_ids = list(
            session.execute(
                select(GeneratedContent.id).where(GeneratedContent.lead_id == lead_id)
            ).scalars()
        )

        # Count what we're about to remove BEFORE deletion so the response
        # reports accurate numbers even after the cascade fires.
        counts = {
            "enrichments": int(
                session.execute(
                    select(func.count(Enrichment.id)).where(Enrichment.lead_id == lead_id)
                ).scalar()
                or 0
            ),
            "scores": int(
                session.execute(
                    select(func.count(Score.id)).where(Score.lead_id == lead_id)
                ).scalar()
                or 0
            ),
            "generated_contents": len(content_ids),
            "content_ratings": int(
                session.execute(
                    select(func.count(ContentRating.id)).where(
                        ContentRating.generated_content_id.in_(content_ids)
                    )
                ).scalar()
                or 0
            )
            if content_ids
            else 0,
            "engagements": int(
                session.execute(
                    select(func.count(Engagement.id)).where(
                        Engagement.content_id.in_(content_ids)
                    )
                ).scalar()
                or 0
            )
            if content_ids
            else 0,
            "leads": 1,
        }

        if content_ids:
            # Engagement has no cascade — clean up explicitly.
            session.execute(
                delete(Engagement).where(Engagement.content_id.in_(content_ids))
            )
            # Null out self-FK supersede chain so Postgres FK enforcement
            # doesn't block the bulk delete of content rows. SQLAlchemy's
            # cascade on Lead.contents handles the actual content deletion
            # in dependency-safe order on session.delete(lead).
            session.execute(
                update(GeneratedContent)
                .where(GeneratedContent.lead_id == lead_id)
                .values(superseded_by_id=None)
            )

        session.delete(lead)
        session.flush()

    log.info(
        "lead_deleted",
        extra={"lead_id": lead_id, "deleted_counts": counts},
    )
    return {"success": True, "lead_id": lead_id, "deleted_counts": counts}


def clear_delivery_error(content_id: int) -> dict[str, Any]:
    """Phase 9b: reset a GeneratedContent row's failed-send state so the
    lead can be re-sent. Clears delivery_status, error_message,
    delivered_at, and delivery_id. Idempotent — returns success=False
    if the content_id doesn't exist."""
    with session_scope() as session:
        content = session.get(GeneratedContent, content_id)
        if content is None:
            return {"success": False, "content_id": content_id, "reason": "not found"}

        prev_status = content.delivery_status
        content.delivery_status = None
        content.error_message = None
        content.delivered_at = None
        content.delivery_id = None
        session.flush()

    log.info(
        "delivery_error_cleared",
        extra={"content_id": content_id, "previous_status": prev_status},
    )
    return {"success": True, "content_id": content_id, "previous_status": prev_status}
