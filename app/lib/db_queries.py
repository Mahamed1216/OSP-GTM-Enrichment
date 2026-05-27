"""Read-only SQLAlchemy queries for the UI. Each function opens its own session."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import and_, case, func, select

from src.db import session_scope
from src.feedback.ratings import get_rating_trends, get_unrated_content
from src.models import (
    Engagement,
    Enrichment,
    GeneratedContent,
    InstantlyAnalyticsSnapshot,
    Lead,
    Score,
)


def _email_content_subq():
    """Subquery: latest email GeneratedContent row per lead."""
    return (
        select(
            GeneratedContent.lead_id.label("lead_id"),
            func.max(GeneratedContent.id).label("content_id"),
            func.max(
                case((GeneratedContent.delivered_at.is_not(None), 1), else_=0)
            ).label("any_delivered"),
        )
        .where(GeneratedContent.kind == "email")
        .group_by(GeneratedContent.lead_id)
        .subquery()
    )


def kpi_counts() -> dict[str, int]:
    with session_scope() as session:
        leads_total = session.execute(select(func.count(Lead.id))).scalar() or 0
        enriched = session.execute(select(func.count(Enrichment.id))).scalar() or 0
        scored = session.execute(select(func.count(Score.id))).scalar() or 0
        # `sent` was previously COUNT(delivered_at IS NOT NULL), which counts
        # leads we PUSHED to Instantly — not leads Instantly has actually sent
        # an email to. The campaign cadence means a chunk of pushes are still
        # queued at any moment, so that count ran ~2x the live Instantly figure.
        # The truth lives in Engagement.sent (synced from emails_sent_count > 0).
        eng_bool_stmt = (
            select(
                func.sum(case((Engagement.sent.is_(True), 1), else_=0)).label("sent"),
                func.sum(case((Engagement.replied.is_(True), 1), else_=0)).label("replied"),
                func.sum(case((Engagement.opened.is_(True), 1), else_=0)).label("opened"),
                func.sum(case((Engagement.clicked.is_(True), 1), else_=0)).label("clicked"),
                func.sum(case((Engagement.bounced.is_(True), 1), else_=0)).label("bounced"),
            )
            .join(GeneratedContent, Engagement.content_id == GeneratedContent.id)
            .where(GeneratedContent.kind == "email")
        )
        row = session.execute(eng_bool_stmt).one()
        return {
            "leads_total": int(leads_total),
            "enriched": int(enriched),
            "scored": int(scored),
            "sent": int(row.sent or 0),
            "replied": int(row.replied or 0),
            "opened": int(row.opened or 0),
            "clicked": int(row.clicked or 0),
            "bounced": int(row.bounced or 0),
        }


def tier_distribution() -> dict[str, int]:
    """Counts per tier including unscored.

    Returns A/B/C/D plus an "—" bucket for leads that have no Score row yet.
    Keyed with the dash so consumers can iterate in order and render the
    "no-tier" group as a neutral track.
    """
    with session_scope() as session:
        rows = session.execute(
            select(Score.tier, func.count(Score.id)).group_by(Score.tier)
        ).all()
        leads_total = session.execute(select(func.count(Lead.id))).scalar() or 0
    out: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    for tier, count in rows:
        if tier and tier.upper() in out:
            out[tier.upper()] = int(count)
    scored_total = sum(out.values())
    out["—"] = max(0, int(leads_total) - scored_total)
    return out


def recent_activity(limit: int = 8) -> list[dict[str, Any]]:
    """Most-recent events across enrich/gen/send/reply/rate, newest first.

    Pulls up to `limit` events from each source then merges + truncates so a
    burst of one event type can't crowd out the rest. Each entry is a plain
    dict: {kind, text, when}.
    """
    from src.models import ContentRating

    events: list[dict[str, Any]] = []

    with session_scope() as session:
        enrichments = session.execute(
            select(
                Enrichment.enriched_at,
                Lead.first_name,
                Lead.last_name,
                Lead.company,
            )
            .join(Lead, Lead.id == Enrichment.lead_id)
            .order_by(Enrichment.enriched_at.desc())
            .limit(limit)
        ).all()
        for r in enrichments:
            name = f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip() or "(no name)"
            company = (r.company or "").strip()
            text = f"Enriched {name}" + (f" at {company}" if company else "")
            events.append({"kind": "enrich", "text": text, "when": r.enriched_at})

        gens = session.execute(
            select(
                GeneratedContent.created_at,
                GeneratedContent.kind,
                Lead.first_name,
                Lead.last_name,
            )
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .where(GeneratedContent.superseded_by_id.is_(None))
            .order_by(GeneratedContent.created_at.desc())
            .limit(limit)
        ).all()
        kind_label = {"email": "email", "call_script": "call script", "linkedin_msg": "LinkedIn DM"}
        for r in gens:
            name = f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip() or "(no name)"
            events.append({
                "kind": "gen",
                "text": f"Generated {kind_label.get(r.kind, r.kind)} for {name}",
                "when": r.created_at,
            })

        sends = session.execute(
            select(
                GeneratedContent.delivered_at,
                Lead.first_name,
                Lead.last_name,
            )
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .where(GeneratedContent.delivered_at.is_not(None))
            .order_by(GeneratedContent.delivered_at.desc())
            .limit(limit)
        ).all()
        for r in sends:
            name = f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip() or "(no name)"
            events.append({
                "kind": "send",
                "text": f"Pushed email for {name} to Instantly",
                "when": r.delivered_at,
            })

        replies = session.execute(
            select(
                Engagement.synced_at,
                Lead.first_name,
                Lead.last_name,
                Lead.company,
            )
            .join(GeneratedContent, Engagement.content_id == GeneratedContent.id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .where(Engagement.replied.is_(True))
            .order_by(Engagement.synced_at.desc())
            .limit(limit)
        ).all()
        for r in replies:
            name = f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip() or "(no name)"
            company = (r.company or "").strip()
            text = f"Reply from {name}" + (f" at {company}" if company else "")
            events.append({"kind": "reply", "text": text, "when": r.synced_at})

        ratings = session.execute(
            select(
                ContentRating.rated_at,
                ContentRating.rating,
                Lead.first_name,
                Lead.last_name,
            )
            .join(GeneratedContent, GeneratedContent.id == ContentRating.generated_content_id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .order_by(ContentRating.rated_at.desc())
            .limit(limit)
        ).all()
        for r in ratings:
            name = f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip() or "(no name)"
            verb = "Thumbs-up" if r.rating == "up" else "Thumbs-down"
            tail = " — redo" if r.rating == "down" else ""
            events.append({
                "kind": "rate",
                "text": f"{verb} on {name}{tail}",
                "when": r.rated_at,
            })

    events.sort(key=lambda e: e["when"] or datetime.min, reverse=True)
    return events[:limit]


def ready_to_send_count() -> int:
    """Leads with active email content that hasn't been delivered yet.

    Used to drive the Dashboard promo CTA. Counts distinct leads with at
    least one active (non-superseded) email GeneratedContent whose
    delivered_at is null AND delivery_status is not 'in_progress'/'error'.
    """
    with session_scope() as session:
        stmt = (
            select(func.count(func.distinct(GeneratedContent.lead_id)))
            .where(
                GeneratedContent.kind == "email",
                GeneratedContent.superseded_by_id.is_(None),
                GeneratedContent.delivered_at.is_(None),
                func.coalesce(GeneratedContent.delivery_status, "") != "in_progress",
            )
        )
        return int(session.execute(stmt).scalar() or 0)


def list_leads() -> pd.DataFrame:
    """Lead list with joined score/enrichment/email-delivery/reply flags."""
    email_sub = _email_content_subq()
    stmt = (
        select(
            Lead.id.label("id"),
            Lead.first_name,
            Lead.last_name,
            Lead.company,
            Lead.title,
            Score.tier,
            Score.score,
            Enrichment.id.label("enrichment_id"),
            email_sub.c.any_delivered,
            func.coalesce(
                func.max(case((Engagement.replied.is_(True), 1), else_=0)), 0
            ).label("any_reply"),
        )
        .select_from(Lead)
        .outerjoin(Score, Score.lead_id == Lead.id)
        .outerjoin(Enrichment, Enrichment.lead_id == Lead.id)
        .outerjoin(email_sub, email_sub.c.lead_id == Lead.id)
        .outerjoin(
            GeneratedContent,
            and_(
                GeneratedContent.lead_id == Lead.id,
                GeneratedContent.kind == "email",
            ),
        )
        .outerjoin(Engagement, Engagement.content_id == GeneratedContent.id)
        .group_by(
            Lead.id,
            Lead.first_name,
            Lead.last_name,
            Lead.company,
            Lead.title,
            Score.tier,
            Score.score,
            Enrichment.id,
            email_sub.c.any_delivered,
        )
        .order_by(Lead.id.asc())
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()

    records: list[dict[str, Any]] = []
    for row in rows:
        records.append(
            {
                "id": row.id,
                "Name": f"{row.first_name} {row.last_name}".strip(),
                "Company": row.company or "",
                "Title": row.title or "",
                "Tier": row.tier or "",
                "Score": int(row.score) if row.score is not None else None,
                "Enriched": bool(row.enrichment_id is not None),
                "Sent": bool(row.any_delivered),
                "Replied": bool(row.any_reply),
            }
        )
    return pd.DataFrame.from_records(
        records,
        columns=["id", "Name", "Company", "Title", "Tier", "Score", "Enriched", "Sent", "Replied"],
    )


def get_scored_lead_ids(lead_ids: list[int]) -> set[int]:
    """Return the subset of lead_ids that already have a Score row.

    Used by the pipeline runner to skip leads whose scoring already succeeded.
    """
    if not lead_ids:
        return set()
    with session_scope() as session:
        rows = session.execute(
            select(Score.lead_id).where(Score.lead_id.in_(lead_ids))
        ).all()
    return {r[0] for r in rows}


def get_all_lead_ids() -> list[int]:
    """Return every lead ID currently in the database, ordered ascending.

    Used by the Run Pipeline page to snapshot the lead set before and after
    a CSV ingest so newly-inserted IDs can be identified by set diff.
    """
    with session_scope() as session:
        rows = session.execute(select(Lead.id).order_by(Lead.id.asc())).all()
    return [r[0] for r in rows]


def get_unenriched_lead_ids() -> list[int]:
    """Return IDs of all leads with no Enrichment row, ordered by id."""
    enriched_subq = select(Enrichment.lead_id).subquery()
    with session_scope() as session:
        rows = session.execute(
            select(Lead.id)
            .where(Lead.id.notin_(select(enriched_subq.c.lead_id)))
            .order_by(Lead.id.asc())
        ).all()
    return [r[0] for r in rows]


def get_unscored_lead_ids() -> list[int]:
    """Return IDs of leads that have enrichment but no Score row yet."""
    enriched_subq = select(Enrichment.lead_id).subquery()
    scored_subq = select(Score.lead_id).subquery()
    with session_scope() as session:
        rows = session.execute(
            select(Lead.id)
            .where(
                Lead.id.in_(select(enriched_subq.c.lead_id)),
                Lead.id.notin_(select(scored_subq.c.lead_id)),
            )
            .order_by(Lead.id.asc())
        ).all()
    return [r[0] for r in rows]


def get_content_pending_lead_ids() -> list[int]:
    """Return IDs of leads that have a Score but no GeneratedContent row yet."""
    scored_subq = select(Score.lead_id).subquery()
    content_subq = select(GeneratedContent.lead_id).subquery()
    with session_scope() as session:
        rows = session.execute(
            select(Lead.id)
            .where(
                Lead.id.in_(select(scored_subq.c.lead_id)),
                Lead.id.notin_(select(content_subq.c.lead_id)),
            )
            .order_by(Lead.id.asc())
        ).all()
    return [r[0] for r in rows]


def get_leads_with_content_by_tier(tiers: list[str]) -> list[int]:
    """Return lead IDs whose Score.tier is in `tiers` AND that have at least
    one active (non-superseded) GeneratedContent row. Used by the bulk
    regenerate UI to scope a tier-targeted refresh."""
    if not tiers:
        return []
    with session_scope() as session:
        rows = session.execute(
            select(Lead.id)
            .join(Score, Score.lead_id == Lead.id)
            .join(GeneratedContent, GeneratedContent.lead_id == Lead.id)
            .where(
                Score.tier.in_(tiers),
                GeneratedContent.superseded_by_id.is_(None),
            )
            .distinct()
        ).all()
    return sorted(r[0] for r in rows)


def get_content_lead_ids(lead_ids: list[int], kind: str) -> set[int]:
    """Return the subset of lead_ids that already have a GeneratedContent row
    of the given kind with non-empty body (and non-empty subject for email)."""
    if not lead_ids:
        return set()
    stmt = select(GeneratedContent.lead_id).where(
        GeneratedContent.lead_id.in_(lead_ids),
        GeneratedContent.kind == kind,
        GeneratedContent.body.is_not(None),
        GeneratedContent.body != "",
    )
    if kind == "email":
        stmt = stmt.where(
            GeneratedContent.subject.is_not(None),
            GeneratedContent.subject != "",
        )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return {r[0] for r in rows}


def get_lead_full(lead_id: int) -> dict[str, Any] | None:
    """Bundle Lead + Enrichment + Score + GeneratedContent[] + Engagement[] as plain dicts.

    Returns None when the lead does not exist. All ORM attributes are read inside
    the session so the caller never touches detached objects.
    """
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return None

        lead_dict = {
            "id": lead.id,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "title": lead.title,
            "company": lead.company,
            "company_domain": lead.company_domain,
            "linkedin_url": lead.linkedin_url,
            "company_linkedin_url": lead.company_linkedin_url,
            "industry": lead.industry,
            "email_verification_status": lead.email_verification_status,
            "email_verification_provider": lead.email_verification_provider,
            "email_verified_at": lead.email_verified_at,
            "created_at": lead.created_at,
        }

        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        enrichment_dict: dict[str, Any] | None = None
        if enrichment is not None:
            enrichment_dict = {
                "linkedin_profile": enrichment.linkedin_profile,
                "company_details": enrichment.company_details,
                "company_news": enrichment.company_news,
                "industry_news": enrichment.industry_news,
                "source_status": enrichment.source_status or {},
                "enriched_at": enrichment.enriched_at,
            }

        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        score_dict: dict[str, Any] | None = None
        if score is not None:
            score_dict = {
                "score": int(score.score),
                "tier": score.tier,
                "rationale": score.rationale,
                "signals_used": list(score.signals_used or []),
                "model": score.model,
                "scored_at": score.scored_at,
            }

        contents = (
            session.execute(
                select(GeneratedContent)
                .where(GeneratedContent.lead_id == lead_id)
                .order_by(GeneratedContent.kind.asc(), GeneratedContent.id.desc())
            )
            .scalars()
            .all()
        )
        contents_list: list[dict[str, Any]] = []
        for c in contents:
            contents_list.append(
                {
                    "id": c.id,
                    "kind": c.kind,
                    "subject": c.subject,
                    "body": c.body,
                    "signals_cited": list(c.signals_cited or []),
                    "prompt_version": c.prompt_version,
                    "model": c.model,
                    "created_at": c.created_at,
                    "delivered_at": c.delivered_at,
                    "delivery_provider": c.delivery_provider,
                    "delivery_id": c.delivery_id,
                    "skip_reason": c.skip_reason,
                    "delivery_status": c.delivery_status,
                    "error_message": c.error_message,
                    "superseded_by_id": c.superseded_by_id,
                }
            )

        content_ids = [c["id"] for c in contents_list]
        engagements_list: list[dict[str, Any]] = []
        if content_ids:
            engs = (
                session.execute(
                    select(Engagement)
                    .where(Engagement.content_id.in_(content_ids))
                    .order_by(Engagement.synced_at.desc())
                )
                .scalars()
                .all()
            )
            for e in engs:
                engagements_list.append(
                    {
                        "id": e.id,
                        "content_id": e.content_id,
                        "sent": bool(e.sent),
                        "delivered": bool(e.delivered),
                        "opened": bool(e.opened),
                        "clicked": bool(e.clicked),
                        "replied": bool(e.replied),
                        "reply_sentiment": e.reply_sentiment,
                        "bounced": bool(e.bounced),
                        "raw": e.raw,
                        "synced_at": e.synced_at,
                    }
                )

        return {
            "lead": lead_dict,
            "enrichment": enrichment_dict,
            "score": score_dict,
            "contents": contents_list,
            "engagements": engagements_list,
        }


def latest_instantly_snapshot() -> dict[str, Any] | None:
    """Most-recent Instantly analytics snapshot, as a plain dict.

    Returns None when no snapshot exists yet (e.g. the GitHub Actions job
    hasn't run since the table was added). Caller uses this for the
    "Source: Instantly campaign analytics" KPI row and the debug expander.
    """
    with session_scope() as session:
        snap = session.execute(
            select(InstantlyAnalyticsSnapshot)
            .order_by(InstantlyAnalyticsSnapshot.synced_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is None:
            return None
        return {
            "id": snap.id,
            "campaign_id": snap.campaign_id,
            "leads_count": int(snap.leads_count or 0),
            "contacted_count": int(snap.contacted_count or 0),
            "emails_sent_count": int(snap.emails_sent_count or 0),
            "open_count": int(snap.open_count or 0),
            "unique_open_count": (
                int(snap.unique_open_count)
                if snap.unique_open_count is not None
                else None
            ),
            "reply_count": int(snap.reply_count or 0),
            "bounced_count": int(snap.bounced_count or 0),
            "click_count": int(snap.click_count or 0),
            "unsubscribed_count": int(snap.unsubscribed_count or 0),
            "completed_count": int(snap.completed_count or 0),
            "raw": snap.raw or {},
            "synced_at": snap.synced_at,
        }


def local_sent_count() -> int:
    """Count of email GeneratedContent rows with delivery_status='sent'.

    Used to surface the Instantly-vs-local discrepancy on the Engagement
    page. Mirrors the count behind the "Sent folder" list.
    """
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count(GeneratedContent.id)).where(
                    GeneratedContent.kind == "email",
                    GeneratedContent.delivery_status == "sent",
                )
            ).scalar()
            or 0
        )


def last_sync_time() -> datetime | None:
    with session_scope() as session:
        ts = session.execute(select(func.max(Engagement.synced_at))).scalar()
    return ts


def recent_engagement(limit: int = 20) -> pd.DataFrame:
    """Most-recent successful email pushes, joined with optional engagement.

    Source is `GeneratedContent` (delivery_status='sent', kind='email') —
    NOT `Engagement` — so a lead that was just pushed shows up immediately
    without waiting for the next "Sync engagement from Instantly" run.
    Engagement is LEFT-joined so opens / replies / clicks surface once
    they've been synced from Instantly.

    Ordering: COALESCE(delivered_at, created_at) DESC so the newest send
    is always on top. Email lead/company/subject/body come back inline so
    the page can render the row + expand-to-email without a second query.
    """
    stmt = (
        select(
            GeneratedContent.id.label("content_id"),
            GeneratedContent.delivered_at.label("delivered_at"),
            GeneratedContent.created_at.label("created_at"),
            GeneratedContent.delivery_status.label("delivery_status"),
            GeneratedContent.kind.label("kind"),
            GeneratedContent.subject.label("subject"),
            GeneratedContent.body.label("body"),
            Lead.first_name.label("first_name"),
            Lead.last_name.label("last_name"),
            Lead.company.label("company"),
            Lead.email.label("email"),
            Engagement.synced_at.label("synced_at"),
            Engagement.sent.label("eng_sent"),
            Engagement.delivered.label("eng_delivered"),
            Engagement.opened.label("eng_opened"),
            Engagement.clicked.label("eng_clicked"),
            Engagement.replied.label("eng_replied"),
            Engagement.bounced.label("eng_bounced"),
        )
        .select_from(GeneratedContent)
        .join(Lead, Lead.id == GeneratedContent.lead_id)
        .outerjoin(Engagement, Engagement.content_id == GeneratedContent.id)
        .where(
            GeneratedContent.kind == "email",
            GeneratedContent.delivery_status == "sent",
        )
        .order_by(
            func.coalesce(
                GeneratedContent.delivered_at, GeneratedContent.created_at
            ).desc(),
            GeneratedContent.id.desc(),
        )
        .limit(limit)
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return pd.DataFrame.from_records(
        [
            {
                # Use the delivered_at timestamp when present (true "sent time"),
                # otherwise the engagement synced_at, otherwise content created_at.
                # The page's row label calls this "Sent" so it must be a send-time
                # value, not a sync-time value.
                "synced_at": r.delivered_at or r.synced_at or r.created_at,
                "delivered_at": r.delivered_at,
                "delivery_status": r.delivery_status or "sent",
                "lead": f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip(),
                "company": r.company or "",
                "email": r.email or "",
                "content_id": int(r.content_id),
                "kind": r.kind,
                "subject": r.subject or "",
                "body": r.body or "",
                "sent": True,  # delivery_status='sent' guarantees this
                "delivered": bool(r.eng_delivered) if r.eng_delivered is not None else False,
                "opened": bool(r.eng_opened) if r.eng_opened is not None else False,
                "clicked": bool(r.eng_clicked) if r.eng_clicked is not None else False,
                "replied": bool(r.eng_replied) if r.eng_replied is not None else False,
                "bounced": bool(r.eng_bounced) if r.eng_bounced is not None else False,
            }
            for r in rows
        ],
        columns=[
            "synced_at", "delivered_at", "delivery_status",
            "lead", "company", "email", "content_id", "kind",
            "subject", "body",
            "sent", "delivered", "opened", "clicked", "replied", "bounced",
        ],
    )


def sent_emails() -> pd.DataFrame:
    """All email GeneratedContent rows that were successfully pushed to Instantly.

    Filters on delivery_status = "sent" (the Phase 9 outcome flag for a
    successful API push), kind = "email". Sort newest-first by delivered_at
    so the Engagement "Sent folder" shows the most recent sends on top.
    Includes the full body so the page can expand each row without a
    second query.
    """
    stmt = (
        select(
            GeneratedContent.id.label("content_id"),
            GeneratedContent.delivered_at.label("sent_at"),
            GeneratedContent.subject.label("subject"),
            GeneratedContent.body.label("body"),
            Lead.first_name.label("first_name"),
            Lead.last_name.label("last_name"),
            Lead.company.label("company"),
        )
        .join(Lead, Lead.id == GeneratedContent.lead_id)
        .where(
            GeneratedContent.kind == "email",
            GeneratedContent.delivery_status == "sent",
        )
        .order_by(
            GeneratedContent.delivered_at.desc().nulls_last(),
            GeneratedContent.id.desc(),
        )
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return pd.DataFrame.from_records(
        [
            {
                "content_id": int(r.content_id),
                "sent_at": r.sent_at,
                "lead": f"{(r.first_name or '').strip()} {(r.last_name or '').strip()}".strip(),
                "company": r.company or "",
                "subject": r.subject or "",
                "body": r.body or "",
            }
            for r in rows
        ],
        columns=["content_id", "sent_at", "lead", "company", "subject", "body"],
    )


def reply_rate_series(days: int = 30) -> pd.DataFrame:
    """Aggregate replies / sends by date(synced_at) over the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with session_scope() as session:
        stmt = (
            select(
                func.date(Engagement.synced_at).label("day"),
                func.count(Engagement.id).label("sent"),
                func.sum(case((Engagement.replied.is_(True), 1), else_=0)).label("replied"),
            )
            .join(GeneratedContent, Engagement.content_id == GeneratedContent.id)
            .where(
                and_(
                    GeneratedContent.kind == "email",
                    Engagement.synced_at >= cutoff,
                )
            )
            .group_by(func.date(Engagement.synced_at))
            .order_by(func.date(Engagement.synced_at).asc())
        )
        rows = session.execute(stmt).all()
    df = pd.DataFrame(
        [{"date": r.day, "sent": int(r.sent or 0), "replied": int(r.replied or 0)} for r in rows]
    )
    if df.empty:
        return pd.DataFrame(columns=["date", "sent", "replied", "reply_rate"])
    df["reply_rate"] = df.apply(
        lambda r: (r["replied"] / r["sent"]) if r["sent"] else 0.0, axis=1
    )
    return df


# ---------------------------------------------------------------------------
# Phase 5e — review queue + rating trends for the Dashboard
# ---------------------------------------------------------------------------

_KIND_LABELS = {
    "email": "Email",
    "call_script": "Call Script",
    "linkedin_msg": "LinkedIn DM",
}


def list_unrated_content(
    content_type: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    """Review-queue DataFrame: head, unrated content rows joined with lead/tier.

    Wraps `src.feedback.ratings.get_unrated_content` and decorates with lead
    name and company in a single follow-up DB pass.
    """
    rows = get_unrated_content(content_type=content_type, limit=limit)
    if not rows:
        return pd.DataFrame(
            columns=["id", "lead_id", "Lead", "Company", "Type", "Tier", "Created"]
        )

    lead_ids = list({r["lead_id"] for r in rows})
    with session_scope() as session:
        lead_rows = session.execute(
            select(Lead.id, Lead.first_name, Lead.last_name, Lead.company)
            .where(Lead.id.in_(lead_ids))
        ).all()
    lead_lookup = {
        lid: {
            "name": f"{(fn or '').strip()} {(ln or '').strip()}".strip(),
            "company": comp or "",
        }
        for (lid, fn, ln, comp) in lead_rows
    }

    records: list[dict[str, Any]] = []
    for r in rows:
        info = lead_lookup.get(r["lead_id"], {"name": "", "company": ""})
        records.append(
            {
                "id": r["id"],
                "lead_id": r["lead_id"],
                "Lead": info["name"],
                "Company": info["company"],
                "Type": _KIND_LABELS.get(r["kind"], r["kind"]),
                "Tier": r["tier"] or "—",
                "Created": r["created_at"],
            }
        )
    return pd.DataFrame.from_records(
        records,
        columns=["id", "lead_id", "Lead", "Company", "Type", "Tier", "Created"],
    )


def rating_summary_per_content_type(days: int = 30) -> pd.DataFrame:
    """Pivoted up-rate frame for the trends chart.

    Returns a DataFrame indexed by date with one column per content_type
    holding the daily up-rate (replied:sent ratio for ratings, on a 0–1 scale).
    Empty if no ratings exist within the window.
    """
    trends = get_rating_trends(days=days)
    if not trends:
        return pd.DataFrame()

    long_rows: list[dict[str, Any]] = []
    for kind, entries in trends.items():
        for e in entries:
            long_rows.append(
                {
                    "date": e["date"],
                    "kind": _KIND_LABELS.get(kind, kind),
                    "up_rate": float(e["up_rate"]),
                }
            )
    long_df = pd.DataFrame(long_rows)
    if long_df.empty:
        return long_df

    pivot = long_df.pivot(index="date", columns="kind", values="up_rate").sort_index()
    return pivot
