"""SalesOS shared-Supabase data-contract tables.

These ORM models implement the contract documented in
``docs/salesos_supabase_contract.md``. They are the engine's view of the
**shared SalesOS Supabase** tables: leads sourced by CSMs, the outbound-job
queue, and the enrichment/score/content/approval/delivery rows the engine
reads and writes back.

Why a ``salesos_`` prefix and a separate Base registration:
  - The engine already owns a ``leads`` table (its internal pipeline state).
    SalesOS's ``leads`` table is a DIFFERENT concept (CSM-sourced records), so
    the contract tables are prefixed to coexist in the same database without
    colliding.
  - SalesOS's real physical schema is not finalized. The engine never assumes
    it: in production these names map to SalesOS's tables either via Postgres
    VIEWS named ``salesos_*`` or by remapping here + in ``adapter.py``. The
    adapter is the only seam that touches these models, so the rest of the
    pipeline never needs to know SalesOS's physical schema.

Nothing here runs in standalone mode (``SALESOS_INTEGRATION_MODE=false``). The
tables are created lazily by ``ensure_salesos_tables()`` when the integration
workers/adapters run, so a standalone deployment never grows these tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db import Base
from src.models import now_utc


def _uuid() -> str:
    return uuid.uuid4().hex


# Status vocabularies (kept as module constants so adapter/worker/tests share
# the exact strings rather than re-typing literals).
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_SKIPPED = "skipped"

CONTENT_GENERATED = "generated"
CONTENT_PENDING_REVIEW = "pending_review"
CONTENT_EDITED = "edited"
CONTENT_APPROVED = "approved"
CONTENT_REJECTED = "rejected"
CONTENT_SENT = "sent"

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


class SalesOSLead(Base):
    """A lead sourced by a CSM in the SalesOS Leads tab (shared Supabase)."""

    __tablename__ = "salesos_leads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    # Tenant boundary: workspace_id is the engine-side mapping; client_id is the
    # SalesOS-native tenant id. Either can be used to scope worker runs.
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    client_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)

    external_contact_id: Mapped[Optional[str]] = mapped_column(String(256), index=True)
    source: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    last_name: Mapped[Optional[str]] = mapped_column(String(128))
    title: Mapped[Optional[str]] = mapped_column(String(256))
    email: Mapped[Optional[str]] = mapped_column(String(256), index=True)
    email_verified: Mapped[bool] = mapped_column(default=False)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(512))
    company_name: Mapped[Optional[str]] = mapped_column(String(256))
    company_domain: Mapped[Optional[str]] = mapped_column(String(256))
    company_website: Mapped[Optional[str]] = mapped_column(String(512))
    company_industry: Mapped[Optional[str]] = mapped_column(String(256))

    raw_source_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    source_signals: Mapped[Optional[list]] = mapped_column(JSON)
    # Source tier is stored SEPARATELY from the engine's computed tier and never
    # overwrites it (see lead_scores.tier).
    source_tier: Mapped[Optional[str]] = mapped_column(String(16))
    source_tier_score: Mapped[Optional[float]] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class OutboundJob(Base):
    """One unit of outbound processing work for a SalesOS lead.

    Also referred to as ``outbound_processing_runs`` in the contract. The
    processing worker claims ``queued`` jobs, sets ``running``, and finalizes
    ``completed`` / ``failed`` / ``skipped``.
    """

    __tablename__ = "salesos_outbound_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    client_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)

    status: Mapped[str] = mapped_column(String(16), default=JOB_QUEUED, index=True, nullable=False)
    requested_by: Mapped[Optional[str]] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    error: Mapped[Optional[str]] = mapped_column(Text)
    options: Mapped[Optional[dict]] = mapped_column(JSON)

    # Link to the engine's internal Lead row once imported (so results written
    # to the engine pipeline can be mirrored back to the SalesOS contract tables).
    engine_lead_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)

    lead: Mapped[SalesOSLead] = relationship()


class LeadEnrichment(Base):
    """Enrichment output mirrored back to SalesOS per lead."""

    __tablename__ = "salesos_lead_enrichments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True, unique=True)
    linkedin_profile: Mapped[Optional[dict]] = mapped_column(JSON)
    company_details: Mapped[Optional[dict]] = mapped_column(JSON)
    company_news: Mapped[Optional[list]] = mapped_column(JSON)
    industry_news: Mapped[Optional[list]] = mapped_column(JSON)
    buyer_account_research: Mapped[Optional[dict]] = mapped_column(JSON)
    tavily_metadata: Mapped[Optional[dict]] = mapped_column(JSON)
    source_status: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class LeadScore(Base):
    """Engine-computed score/tier mirrored back to SalesOS per lead."""

    __tablename__ = "salesos_lead_scores"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True, unique=True)
    score: Mapped[Optional[int]] = mapped_column(Integer)
    tier: Mapped[Optional[str]] = mapped_column(String(16))
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    signals_used: Mapped[Optional[list]] = mapped_column(JSON)
    model_version: Mapped[Optional[str]] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class OutboundContent(Base):
    """Generated outreach awaiting CSM review in the SalesOS Outbound tab."""

    __tablename__ = "salesos_outbound_content"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True)
    email_subject: Mapped[Optional[str]] = mapped_column(String(512))
    email_body: Mapped[Optional[str]] = mapped_column(Text)
    call_script: Mapped[Optional[str]] = mapped_column(Text)
    linkedin_message: Mapped[Optional[str]] = mapped_column(Text)
    content_status: Mapped[str] = mapped_column(String(32), default=CONTENT_GENERATED, index=True)
    safety_status: Mapped[Optional[str]] = mapped_column(String(32))
    blocked_reason: Mapped[Optional[str]] = mapped_column(String(64))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(32))
    model_version: Mapped[Optional[str]] = mapped_column(String(64))
    # Link to the engine's internal GeneratedContent row for the email, so the
    # send worker can delegate to the existing delivery primitives.
    engine_content_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class OutboundApproval(Base):
    """The CSM's review decision — the source of truth for 'may send'."""

    __tablename__ = "salesos_outbound_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True)
    content_id: Mapped[str] = mapped_column(ForeignKey("salesos_outbound_content.id"), index=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(128))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approval_status: Mapped[str] = mapped_column(String(16), default=APPROVAL_PENDING, index=True)
    edited_subject: Mapped[Optional[str]] = mapped_column(String(512))
    edited_body: Mapped[Optional[str]] = mapped_column(Text)
    edited_call_script: Mapped[Optional[str]] = mapped_column(Text)
    edited_linkedin_message: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class DeliveryEvent(Base):
    """Send + engagement outcomes written back after delivery / sync."""

    __tablename__ = "salesos_delivery_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("salesos_leads.id"), index=True)
    content_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("salesos_outbound_content.id"), index=True
    )
    destination: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    instantly_lead_id: Mapped[Optional[str]] = mapped_column(String(256))
    campaign_id: Mapped[Optional[str]] = mapped_column(String(128))
    error: Mapped[Optional[str]] = mapped_column(Text)
    engagement_status: Mapped[Optional[str]] = mapped_column(String(32))
    reply_status: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


# All contract table names — used by ensure_salesos_tables() and the db.py
# runtime migration registration.
SALESOS_TABLES: tuple[str, ...] = (
    "salesos_leads",
    "salesos_outbound_jobs",
    "salesos_lead_enrichments",
    "salesos_lead_scores",
    "salesos_outbound_content",
    "salesos_outbound_approvals",
    "salesos_delivery_events",
)
