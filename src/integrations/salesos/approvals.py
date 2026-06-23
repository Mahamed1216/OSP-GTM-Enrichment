"""SalesOS approval lookups — the source of truth for 'may send'.

Kept deliberately dependency-light (imports only db + the SalesOS contract
models) so the shared delivery eligibility gate and the Instantly delivery path
can lazily import it WITHOUT creating an import cycle
(delivery → approvals → models, never delivery → approvals → delivery).

An engine GeneratedContent row is linked to its SalesOS ``outbound_content`` via
``OutboundContent.engine_content_id``; the CSM's decision lives in
``outbound_approvals``. Content is sendable only when an approval row for that
content has ``approval_status == 'approved'``.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

log = logging.getLogger(__name__)


def is_engine_content_approved(engine_content_id: int | None) -> bool:
    """Return True iff the SalesOS CSM has approved this engine content row.

    Never raises (a lookup failure degrades to 'not approved' so a broken
    integration can never accidentally let an unapproved send through).
    """
    if engine_content_id is None:
        return False
    try:
        from src.db import session_scope
        from src.integrations.salesos.models import (
            APPROVAL_APPROVED,
            OutboundApproval,
            OutboundContent,
        )

        with session_scope() as session:
            content = session.execute(
                select(OutboundContent).where(
                    OutboundContent.engine_content_id == engine_content_id
                )
            ).scalars().first()
            if content is None:
                return False
            approval = session.execute(
                select(OutboundApproval).where(
                    OutboundApproval.content_id == content.id,
                    OutboundApproval.approval_status == APPROVAL_APPROVED,
                )
            ).scalars().first()
            return approval is not None
    except Exception as exc:  # pragma: no cover - defensive; never fail-open
        log.warning("salesos_approval_lookup_failed",
                    extra={"engine_content_id": engine_content_id,
                           "error": f"{type(exc).__name__}: {exc}"})
        return False


def engine_leads_missing_approval(
    session, lead_ids: list[int], latest_content_by_lead: dict[int, int]
) -> set[int]:
    """Return the subset of `lead_ids` whose latest email content is NOT approved.

    `latest_content_by_lead` maps engine lead_id → latest email GeneratedContent.id.
    A lead with no known content id is treated as missing approval. Uses the
    passed-in session so the caller's filter runs in one transaction.

    Fail-closed: any error (e.g. contract tables not yet created) is treated as
    "no approvals found", so a broken integration blocks sends rather than
    letting unapproved content through.
    """
    try:
        from src.integrations.salesos.models import (
            APPROVAL_APPROVED,
            OutboundApproval,
            OutboundContent,
        )

        content_ids = [cid for cid in latest_content_by_lead.values() if cid is not None]
        approved_engine_content: set[int] = set()
        if content_ids:
            rows = session.execute(
                select(OutboundContent.engine_content_id)
                .join(OutboundApproval, OutboundApproval.content_id == OutboundContent.id)
                .where(
                    OutboundContent.engine_content_id.in_(content_ids),
                    OutboundApproval.approval_status == APPROVAL_APPROVED,
                )
            ).scalars().all()
            approved_engine_content = {r for r in rows if r is not None}
    except Exception as exc:  # pragma: no cover - fail-closed
        log.warning("salesos_approval_batch_lookup_failed",
                    extra={"error": f"{type(exc).__name__}: {exc}"})
        return set(lead_ids)

    missing: set[int] = set()
    for lid in lead_ids:
        cid = latest_content_by_lead.get(lid)
        if cid is None or cid not in approved_engine_content:
            missing.add(lid)
    return missing
