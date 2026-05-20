"""Promote high-performing emails to winning_examples.json + process SDR ratings.

Two promotion paths:
- `promote_winners()` — engagement-based (replied=True). Tags entries with
  `source: "engagement_reply"`.
- `process_ratings()` — manual SDR ratings. Up → winners, down with feedback →
  negatives. Tags entries with `source: "manual_rating"` and embeds `rating_id`
  for idempotency.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from src.content.winners import NEGATIVES_PATH, WINNERS_PATH
from src.db import session_scope
from src.models import ContentRating, Engagement, GeneratedContent, Lead, Score

log = logging.getLogger(__name__)

DEFAULT_REPLY_THRESHOLD = 0.20  # at the per-lead grain a reply is binary,
                                # so this acts as a floor for any future
                                # cohort-based reply-rate calculation
MAX_LIBRARY_SIZE = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json_array(path) -> list[dict]:
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("library_load_failed", extra={"path": str(path), "error": str(exc)})
        return []
    return items if isinstance(items, list) else []


def _save_json_array(path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


# Backward-compat aliases for the old single-purpose helpers.
def _load_library() -> list[dict]:
    return _load_json_array(WINNERS_PATH)


def _save_library(items: list[dict]) -> None:
    _save_json_array(WINNERS_PATH, items)


# ---------------------------------------------------------------------------
# Engagement-based promotion (Phase 4 behavior, enriched with new keys)
# ---------------------------------------------------------------------------

def _make_winner(content: GeneratedContent, lead: Lead, score: Score | None, reply_rate: float) -> dict:
    signal = score.signals_used[0] if score and score.signals_used else "n/a"
    now_iso = _now_iso()
    return {
        "id": f"auto_{content.id}",
        "content_type": content.kind,
        "source": "engagement_reply",
        "score": float(reply_rate),
        "added_at": now_iso,
        "promoted_at": now_iso,
        "lead_context": {
            "title": lead.title,
            "industry": lead.industry,
            "signal": signal,
        },
        "lead_context_summary": (
            f"{lead.title or 'unknown role'} in {lead.industry or 'unknown industry'} "
            f"— signal: {signal}"
        ),
        "subject": content.subject,
        "body": content.body,
        "content": {"subject": content.subject, "body": content.body},
        "reply_rate": reply_rate,
        "manually_flagged": False,
        "is_active": True,
    }


def promote_winners(threshold: float = DEFAULT_REPLY_THRESHOLD) -> dict:
    """Find replied emails not yet in the library and add them.

    Idempotent on content id. Library is sorted by manually_flagged desc, then
    score (or legacy reply_rate) desc, then trimmed to MAX_LIBRARY_SIZE.
    """
    library = _load_library()
    existing_ids = {w.get("id") for w in library if w.get("id")}

    promoted = 0
    with session_scope() as session:
        rows = session.execute(
            select(GeneratedContent, Lead, Score)
            .join(Engagement, Engagement.content_id == GeneratedContent.id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .outerjoin(Score, Score.lead_id == GeneratedContent.lead_id)
            .where(Engagement.replied.is_(True), GeneratedContent.kind == "email")
        ).all()

        for content, lead, score in rows:
            auto_id = f"auto_{content.id}"
            if auto_id in existing_ids:
                continue
            reply_rate = 1.0  # binary at lead grain; future: cohort rate
            if reply_rate < threshold:
                continue
            library.append(_make_winner(content, lead, score, reply_rate))
            existing_ids.add(auto_id)
            promoted += 1

    library.sort(
        key=lambda w: (
            w.get("manually_flagged", False),
            float(w.get("score", w.get("reply_rate", 0.0)) or 0.0),
        ),
        reverse=True,
    )
    library = library[:MAX_LIBRARY_SIZE]
    _save_library(library)

    log.info("winners_promoted", extra={"promoted": promoted, "library_size": len(library)})
    return {"promoted": promoted, "library_size": len(library)}


# ---------------------------------------------------------------------------
# Manual rating-based promotion (Phase 5)
# ---------------------------------------------------------------------------

def _make_rating_winner(content: GeneratedContent, lead: Lead, score: Score | None, rating_id: int) -> dict:
    signal = score.signals_used[0] if score and score.signals_used else "n/a"
    return {
        "id": f"rating_{rating_id}",
        "rating_id": rating_id,
        "content_type": content.kind,
        "source": "manual_rating",
        "score": 1.0,  # SDR thumbs-up is treated as max signal
        "added_at": _now_iso(),
        "lead_context": {
            "title": lead.title,
            "industry": lead.industry,
            "signal": signal,
        },
        "lead_context_summary": (
            f"{lead.title or 'unknown role'} in {lead.industry or 'unknown industry'} "
            f"— signal: {signal}"
        ),
        "subject": content.subject,
        "body": content.body,
        "content": {"subject": content.subject, "body": content.body},
        "manually_flagged": False,
        "is_active": True,
    }


def _make_rating_negative(content: GeneratedContent, lead: Lead, rating: ContentRating) -> dict:
    return {
        "id": f"rating_{rating.id}",
        "rating_id": rating.id,
        "content_type": content.kind,
        "source": "manual_rating",
        "added_at": _now_iso(),
        "feedback_reason": rating.feedback_text or "",
        "lead_context_summary": (
            f"{lead.title or 'unknown role'} in {lead.industry or 'unknown industry'}"
        ),
        "subject": content.subject,
        "body": content.body,
        "content": {"subject": content.subject, "body": content.body},
        "is_active": True,
    }


def process_ratings() -> dict:
    """Walk all ContentRating rows and append new winners/negatives.

    Idempotent: each library entry stores the originating `rating_id`; on
    re-run, ratings whose id is already in either library are skipped.

    Thumbs-down without feedback is ignored — we cannot teach an anti-pattern
    without a reason.
    """
    winners = _load_json_array(WINNERS_PATH)
    negatives = _load_json_array(NEGATIVES_PATH)

    seen_winner_ids = {int(w["rating_id"]) for w in winners if "rating_id" in w}
    seen_negative_ids = {int(n["rating_id"]) for n in negatives if "rating_id" in n}

    new_winners = 0
    new_negatives = 0
    skipped_no_feedback = 0
    processed = 0

    with session_scope() as session:
        rows = session.execute(
            select(ContentRating, GeneratedContent, Lead, Score)
            .join(GeneratedContent, GeneratedContent.id == ContentRating.generated_content_id)
            .join(Lead, Lead.id == GeneratedContent.lead_id)
            .outerjoin(Score, Score.lead_id == GeneratedContent.lead_id)
            .order_by(ContentRating.id.asc())
        ).all()

        for rating, content, lead, score in rows:
            processed += 1
            if rating.rating == "up":
                if rating.id in seen_winner_ids:
                    continue
                winners.append(_make_rating_winner(content, lead, score, rating.id))
                seen_winner_ids.add(rating.id)
                new_winners += 1
            elif rating.rating == "down":
                if not (rating.feedback_text or "").strip():
                    skipped_no_feedback += 1
                    continue
                if rating.id in seen_negative_ids:
                    continue
                negatives.append(_make_rating_negative(content, lead, rating))
                seen_negative_ids.add(rating.id)
                new_negatives += 1

    winners.sort(
        key=lambda w: (
            w.get("manually_flagged", False),
            float(w.get("score", w.get("reply_rate", 0.0)) or 0.0),
        ),
        reverse=True,
    )
    winners = winners[:MAX_LIBRARY_SIZE]

    negatives.sort(key=lambda n: n.get("added_at", ""), reverse=True)
    negatives = negatives[:MAX_LIBRARY_SIZE]

    _save_json_array(WINNERS_PATH, winners)
    _save_json_array(NEGATIVES_PATH, negatives)

    summary = {
        "processed": processed,
        "new_winners": new_winners,
        "new_negatives": new_negatives,
        "skipped_no_feedback": skipped_no_feedback,
        "winners_total": len(winners),
        "negatives_total": len(negatives),
    }
    log.info("ratings_processed", extra=summary)
    return summary
