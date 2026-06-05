from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db import Base


def now_utc() -> datetime:
    return datetime.utcnow()


def _default_workspace_id() -> int | None:
    """SQLAlchemy insert-time default for workspace_id columns.

    Uses raw SQL via the shared engine to avoid ORM circular imports —
    src.workspace imports src.models, so models cannot import workspace.

    Safe in all phases:
      - Returns None when the workspaces table does not exist (fresh DB
        before create_all, or during test setUp without seeding).
      - Returns None when no default workspace is seeded yet.
      - Returns the default OSP workspace id once Phase 1 seeding runs.
    Exceptions are silently swallowed so no insert ever fails because of
    a missing workspace row.

    Uses AUTOCOMMIT isolation so the connection never starts its own transaction
    and closing it never issues a rollback. Without this, the StaticPool (used
    in tests) shares one physical connection, and the default close-time rollback
    corrupts the outer session's pending INSERTs.
    """
    try:
        from sqlalchemy import text as _text
        from src.db import engine as _engine
        conn = _engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        with conn:
            row = conn.execute(
                _text(
                    "SELECT id FROM workspaces "
                    "WHERE is_default = :flag LIMIT 1"
                ),
                {"flag": True},
            ).first()
        return int(row[0]) if row else None
    except Exception:
        return None


class Workspace(Base):
    """A named operating context for the app (e.g. one per client or campaign).

    Phase 1: foundational table only. No foreign keys from other tables yet —
    existing data continues to work unchanged. Phase 2 will add workspace_id
    columns to the lead/prompt/analytics tables and wire up the UI selectors.

    The default OSP workspace is seeded automatically by `init_db()` so the
    app is never left without a workspace after the first startup.
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Instantly integration — nullable so workspaces without a campaign can
    # still exist. The campaign-ID resolver falls back to the env var when
    # this is NULL (see src.workspace.get_campaign_id_for_workspace).
    instantly_campaign_id: Mapped[Optional[str]] = mapped_column(String(128))
    instantly_api_key: Mapped[Optional[str]] = mapped_column(String(256))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Phase 4 hotfix: workspace-scoped ICP/company settings stored as JSON.
    # NULL means "not yet saved — fall back to file or code defaults".
    # Populated on first save from the Settings page, or at startup by
    # backfill_osp_icp_config() which seeds the OSP workspace from the
    # existing data/icp_config.json file so OSP settings are never lost.
    icp_config: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        # Phase 6: allow same email in different workspaces; dedupe within workspace only.
        UniqueConstraint("email", "workspace_id", name="uq_leads_email_workspace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128))
    last_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(256))
    company: Mapped[Optional[str]] = mapped_column(String(256))
    company_domain: Mapped[Optional[str]] = mapped_column(String(256))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(512))
    company_linkedin_url: Mapped[Optional[str]] = mapped_column(String(512))
    industry: Mapped[Optional[str]] = mapped_column(String(256))

    # Cached email verification result so re-runs don't re-charge the verifier
    email_verification_status: Mapped[Optional[str]] = mapped_column(String(32))
    email_verification_provider: Mapped[Optional[str]] = mapped_column(String(32))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    # Phase 2: workspace scoping. Nullable so existing rows survive migration;
    # backfilled to the default OSP workspace id by init_db().
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)

    enrichment: Mapped[Optional["Enrichment"]] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    score: Mapped[Optional["Score"]] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    contents: Mapped[list["GeneratedContent"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class Enrichment(Base):
    __tablename__ = "enrichments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True)

    linkedin_profile: Mapped[Optional[dict]] = mapped_column(JSON)
    linkedin_posts: Mapped[Optional[list]] = mapped_column(JSON)
    company_details: Mapped[Optional[dict]] = mapped_column(JSON)
    company_posts: Mapped[Optional[list]] = mapped_column(JSON)
    company_news: Mapped[Optional[list]] = mapped_column(JSON)
    industry_news: Mapped[Optional[list]] = mapped_column(JSON)
    # Buyer-account discovery: who would BUY the lead's company's product.
    # Schema matches BuyerAccountResult from src.enrichment.buyer_accounts.
    buyer_accounts: Mapped[Optional[dict]] = mapped_column(JSON)

    # {source_name: {"success": bool, "error": str|None, "duration_ms": int}}
    source_status: Mapped[dict] = mapped_column(JSON, default=dict)
    enriched_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)

    lead: Mapped[Lead] = relationship(back_populates="enrichment")


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True)
    score: Mapped[int] = mapped_column(Integer)
    tier: Mapped[str] = mapped_column(String(1))
    rationale: Mapped[str] = mapped_column(Text)
    signals_used: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)

    lead: Mapped[Lead] = relationship(back_populates="score")


class GeneratedContent(Base):
    """One row per generated artifact (email, call script, LinkedIn DM).

    Delivery state lives HERE, not on Lead, because each generated email
    is its own send attempt — the same lead can have multiple content
    rows over time (regeneration, version chain via superseded_by_id)
    with different delivery outcomes. Phase 5 added delivered_at /
    delivery_provider / delivery_id / skip_reason; Phase 9 added
    delivery_status ("sent" | "error" | "in_progress" | NULL) and
    error_message. skip_reason captures pre-send guard refusals;
    delivery_status + error_message capture API-attempt outcomes.
    """
    __tablename__ = "generated_contents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # email | call_script | linkedin_msg
    subject: Mapped[Optional[str]] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    signals_cited: Mapped[list] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(32))
    # SHA256-prefix of the effective system prompt at generation time.
    # Used by bulk-regen resume mode to detect "this row was generated under
    # the current prompt". prompt_version alone is insufficient because
    # editing the overlay in the Prompts UI doesn't bump the version
    # constant. Nullable: pre-fingerprint rows are correctly treated as
    # not-current and will be picked up by resume.
    prompt_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    delivery_provider: Mapped[Optional[str]] = mapped_column(String(32))
    delivery_id: Mapped[Optional[str]] = mapped_column(String(256))
    skip_reason: Mapped[Optional[str]] = mapped_column(String(64))
    # Phase 9: actual send-attempt outcome. skip_reason = "refused to try";
    # delivery_status = "what happened when we tried".
    # Values: "sent" | "error" | "in_progress" | NULL.
    delivery_status: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # Phase 5: version chain — points to the row that replaces this one (nullable HEAD).
    superseded_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("generated_contents.id"), nullable=True
    )

    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)

    lead: Mapped[Lead] = relationship(back_populates="contents")
    ratings: Mapped[list["ContentRating"]] = relationship(
        back_populates="generated_content", cascade="all, delete-orphan"
    )


class Engagement(Base):
    __tablename__ = "engagements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("generated_contents.id"), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    opened: Mapped[bool] = mapped_column(Boolean, default=False)
    clicked: Mapped[bool] = mapped_column(Boolean, default=False)
    replied: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_sentiment: Mapped[Optional[str]] = mapped_column(String(32))
    bounced: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[Optional[dict]] = mapped_column(JSON)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)


class ContentRating(Base):
    __tablename__ = "content_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_content_id: Mapped[int] = mapped_column(
        ForeignKey("generated_contents.id"), nullable=False, index=True
    )
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # "up" | "down"
    feedback_text: Mapped[Optional[str]] = mapped_column(Text)
    rated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    rated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)

    generated_content: Mapped["GeneratedContent"] = relationship(back_populates="ratings")


class WinningExample(Base):
    __tablename__ = "winning_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_context: Mapped[dict] = mapped_column(JSON)
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    reply_rate: Mapped[float] = mapped_column(Float, default=0.0)
    manually_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)
    # Phase 6: content type (email | call_script | linkedin_msg). Nullable for
    # legacy rows; defaults to "email" at read time (all legacy examples are emails).
    content_type: Mapped[Optional[str]] = mapped_column(String(32))


class InstantlyAnalyticsSnapshot(Base):
    """Raw + parsed result of one `/campaigns/analytics` poll.

    Instantly is the source of truth for the campaign-level KPIs shown on
    the Engagement page (sent / opens / replies / bounces). The local
    Engagement table can drift — leads pushed via the UI or imported
    manually into Instantly never get a local `delivery_id`, so the
    per-lead sync would silently miss them. Keeping each snapshot's raw
    JSON also lets the self-improving loop diagnose without re-hitting
    the API.
    """
    __tablename__ = "instantly_analytics_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)
    leads_count: Mapped[int] = mapped_column(Integer, default=0)
    contacted_count: Mapped[int] = mapped_column(Integer, default=0)
    emails_sent_count: Mapped[int] = mapped_column(Integer, default=0)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_open_count: Mapped[Optional[int]] = mapped_column(Integer)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    bounced_count: Mapped[int] = mapped_column(Integer, default=0)
    click_count: Mapped[int] = mapped_column(Integer, default=0)
    unsubscribed_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    # v2 fields — Instantly tracks positive replies (interested/booked)
    # and opportunities separately from total reply_count. NULL when the
    # snapshot was created before this schema addition (raw still holds them).
    positive_reply_count: Mapped[Optional[int]] = mapped_column(Integer)
    opportunity_count: Mapped[Optional[int]] = mapped_column(Integer)
    conversion_count: Mapped[Optional[int]] = mapped_column(Integer)
    # Which raw JSON key produced the positive_reply_count / opportunity_count.
    # e.g. "analytics:positive_reply_count", "leads:status=2", "leads:no_match".
    # NULL for rows created before this column was added.
    raw_positive_reply_source: Mapped[Optional[str]] = mapped_column(String(128))
    raw_opportunity_source: Mapped[Optional[str]] = mapped_column(String(128))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)


class PromptRecommendation(Base):
    """Self-improvement-loop recommendation, gated on human approval.

    The loop diagnoses a bottleneck (open / reply / bounce / delivery)
    from the latest analytics snapshot, drafts a recommendation, and
    persists it here. No prompt is ever modified until status flips to
    `approved`; on approval, `proposed_addendum` is appended to the
    `prompt_configs` overlay so only FUTURE generations pick it up. The
    previous overlay text is stashed in `previous_prompt_snapshot` so
    the operator can roll back.

    Length notes — the values written here that bit us once:
      - status: "ready_for_approval" = 19 chars → declared 32 (was 16).
      - loop_status: same vocabulary as `status` for new rows but kept
        separate so the approval lifecycle (`status`) and the loop's
        diagnostic state (`loop_status`) can diverge over time.
      - proposed_addendum / previous_prompt_snapshot: TEXT because a
        long prompt overlay or a multi-paragraph addendum will blow
        through any VARCHAR cap.
    """
    __tablename__ = "prompt_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bottleneck: Mapped[str] = mapped_column(String(32))  # open_rate|reply_rate|bounce_rate|delivery|none
    channel: Mapped[Optional[str]] = mapped_column(String(32))  # email|None for non-prompt actions
    diagnosis: Mapped[str] = mapped_column(Text)
    current_metric_label: Mapped[str] = mapped_column(String(64))
    current_metric_value: Mapped[float] = mapped_column(Float, default=0.0)
    target_metric_value: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_change: Mapped[str] = mapped_column(Text)
    expected_impact: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(16))  # low | medium | high
    # `proposed_prompt` is the legacy column from the first loop revision.
    # `proposed_addendum` is the canonical column going forward. New code
    # always writes to addendum; `list_recommendations` reads addendum
    # then falls back to prompt for old rows.
    proposed_prompt: Mapped[Optional[str]] = mapped_column(Text)
    proposed_addendum: Mapped[Optional[str]] = mapped_column(Text)
    previous_prompt_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    # Approval lifecycle: pending | approved | rejected | draft | ready_for_approval
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # Loop's diagnostic state at save time: wait | diagnose_only | draft | ready_for_approval
    loop_status: Mapped[Optional[str]] = mapped_column(String(32))
    confidence: Mapped[Optional[str]] = mapped_column(String(16))  # insufficient | low | standard
    # JSON-encoded copy of the kpi_view dict at diagnose time. Lets the
    # history expander show "what the metrics looked like when this
    # recommendation was drafted" without joining back to the snapshot
    # table (which may have rolled forward since).
    metric_snapshot: Mapped[Optional[dict]] = mapped_column(JSON)
    # FK to the InstantlyAnalyticsSnapshot row this recommendation was based on.
    # NULL for rows created before this column was added. Used to detect staleness
    # when a newer snapshot arrives before the operator approves or rejects.
    analytics_snapshot_id: Mapped[Optional[int]] = mapped_column(Integer)
    approved_by: Mapped[Optional[str]] = mapped_column(String(64))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    drafted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)


class PromptConfig(Base):
    """User-edited overrides for the three content-generation system prompts.

    Persisted in the DB (not data/prompts_config.json) so Streamlit Cloud
    deploys don't wipe overrides — local disk is ephemeral there. One row
    per (channel, workspace_id) pair so each workspace has independent prompts.

    Audit columns (`prompt_version`, `prompt_fingerprint`, `updated_by`,
    `is_active`) are populated by `save_overlay` so the editor can show
    "Last saved by X at Y" and the experiment tracker can correlate
    GeneratedContent.prompt_fingerprint with the overlay that produced it.

    Phase 4: unique constraint changed from (channel) to (channel, workspace_id)
    so each workspace can have its own prompt for each channel.
    """
    __tablename__ = "prompt_configs"
    __table_args__ = (
        UniqueConstraint(
            "channel", "workspace_id",
            name="uq_prompt_configs_channel_workspace",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )
    prompt_version: Mapped[Optional[str]] = mapped_column(String(32))
    prompt_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)


class ReplyDraft(Base):
    """Reply draft produced by the Manual Draft Tester. Draft-only — nothing sent automatically."""
    __tablename__ = "reply_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id)
    lead_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    inbound_reply: Mapped[str] = mapped_column(Text, nullable=False)
    original_outbound_email: Mapped[Optional[str]] = mapped_column(Text)
    lead_context: Mapped[Optional[str]] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_body: Mapped[str] = mapped_column(Text, nullable=False)
    human_review_notes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )


class ReplyThread(Base):
    """One inbound reply synced from Instantly, with auto-generated draft.

    Status lifecycle:
      needs_review → (operator approves) → approved → (send attempt)
                   → sent | failed | manual_send_required
      needs_review → skipped  (operator dismissed)
      needs_human_review  (unsubscribe/angry/placeholder text)

    Deduplication: (workspace_id, campaign_id, dedup_key) must be unique.
      dedup_key = "msg:{message_id}"        if message_id available
               = "lead:{instantly_lead_id}" if lead_id available
               = "hash:{sha256(email+reply[:200])[:32]}" fallback
    """
    __tablename__ = "reply_threads"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "campaign_id", "dedup_key",
            name="uq_reply_threads_workspace_campaign_dedup",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, default=_default_workspace_id, index=True)
    lead_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    instantly_lead_id: Mapped[Optional[str]] = mapped_column(String(256))
    prospect_email: Mapped[str] = mapped_column(String(256), nullable=False)
    prospect_name: Mapped[Optional[str]] = mapped_column(String(256))
    company_name: Mapped[Optional[str]] = mapped_column(String(256))
    thread_id: Mapped[Optional[str]] = mapped_column(String(256))
    message_id: Mapped[Optional[str]] = mapped_column(String(256))
    inbound_reply_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_outbound_email: Mapped[Optional[str]] = mapped_column(Text)
    reply_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    classification: Mapped[str] = mapped_column(String(64), nullable=False, default="needs_human_review")
    recommended_action: Mapped[str] = mapped_column(String(64), nullable=False, default="route_to_human")
    draft_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    human_review_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="needs_review", index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    send_error: Mapped[Optional[str]] = mapped_column(Text)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )
