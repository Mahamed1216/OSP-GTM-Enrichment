from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db import Base


def now_utc() -> datetime:
    return datetime.utcnow()


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128))
    last_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(256), unique=True, index=True)
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

    # {source_name: {"success": bool, "error": str|None, "duration_ms": int}}
    source_status: Mapped[dict] = mapped_column(JSON, default=dict)
    enriched_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

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


class PromptConfig(Base):
    """User-edited overrides for the three content-generation system prompts.

    Persisted in the DB (not data/prompts_config.json) so Streamlit Cloud
    deploys don't wipe overrides — local disk is ephemeral there. One row
    per channel; saves upsert by channel.
    """
    __tablename__ = "prompt_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc, nullable=False
    )
