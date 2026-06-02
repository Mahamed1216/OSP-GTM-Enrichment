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

from src.content.winners import (
    NEGATIVES_PATH,
    VALID_WINNER_REASONS,
    WINNER_REASON_UNCONFIRMED,
    WINNERS_PATH,
    _derive_winner_reason,
)
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
        # winner_reason = "positive_reply" is set here for new promotions.
        # Existing entries without this field get it assigned by cleanup_invalid_winners().
        "winner_reason": "positive_reply",
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
        "winner_reason": "manual_positive_rating",
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


# ---------------------------------------------------------------------------
# Winners cleanup — deactivate unconfirmed engagement_reply entries.
# ---------------------------------------------------------------------------

# Integer lead statuses in Instantly that signal positive engagement.
_POSITIVE_STATUS_INTS: frozenset[int] = frozenset({2, 3, 4, 5, 6})
# reply_sentiment string values that confirm a positive reply.
_POSITIVE_SENTIMENTS: frozenset[str] = frozenset({
    "positive", "interested", "opportunity", "booked", "meeting_booked",
})


def _has_positive_engagement_signal(content_id: int) -> bool:
    """Return True if the Engagement row for this content confirms a positive reply.

    Checks in order:
      1. Engagement.reply_sentiment is a known positive value.
      2. Instantly per-lead status in Engagement.raw is ≥ 2 (interested / booked / etc.).
    Returns False when no Engagement row exists or neither signal is present.
    """
    try:
        with session_scope() as session:
            eng = session.execute(
                select(Engagement).where(Engagement.content_id == content_id)
            ).scalar_one_or_none()
            if eng is None:
                return False
            sentiment = (eng.reply_sentiment or "").lower().strip()
            if sentiment in _POSITIVE_SENTIMENTS:
                return True
            raw = eng.raw or {}
            status = raw.get("status")
            if isinstance(status, int) and status in _POSITIVE_STATUS_INTS:
                return True
        return False
    except Exception as exc:
        log.warning(
            "positive_signal_check_failed",
            extra={"content_id": content_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return False


def cleanup_invalid_winners() -> dict:
    """Deactivate Winners Library entries that cannot be confirmed as positive.

    Rules (applied in order, non-destructive — entries are never deleted):
      - source="seed" or manually_flagged=True → keep active, ensure winner_reason="seed"
      - source="manual_rating" → keep active, ensure winner_reason="manual_positive_rating"
      - source="engagement_reply" with explicit valid winner_reason → keep active
      - source="engagement_reply" without explicit valid reason → check Engagement row:
          positive signal confirmed → keep active, winner_reason="positive_reply"
          no positive signal → set is_active=False, winner_reason=WINNER_REASON_UNCONFIRMED
      - any other source with no valid winner_reason → deactivate

    Returns a summary dict: {kept, deactivated, already_inactive, reason_set}.
    """
    library = _load_library()
    kept = 0
    deactivated = 0
    already_inactive = 0
    reason_set = 0

    for entry in library:
        source = entry.get("source") or ""
        manually_flagged = bool(entry.get("manually_flagged", False))
        currently_active = bool(entry.get("is_active", True))

        # --- Seed entries: always keep ---
        if source == "seed" or manually_flagged:
            if entry.get("winner_reason") != "seed":
                entry["winner_reason"] = "seed"
                reason_set += 1
            if not currently_active:
                entry["is_active"] = True
            kept += 1
            continue

        # --- Manual rating entries: always keep ---
        if source == "manual_rating":
            if entry.get("winner_reason") != "manual_positive_rating":
                entry["winner_reason"] = "manual_positive_rating"
                reason_set += 1
            kept += 1
            continue

        # --- Engagement reply entries: confirm positive signal ---
        if source == "engagement_reply":
            existing_reason = entry.get("winner_reason")
            # Already has an explicit valid reason (set by new code or a previous cleanup).
            if existing_reason in VALID_WINNER_REASONS:
                kept += 1
                continue

            # Try to extract content_id from the entry id (format: "auto_{content_id}").
            entry_id = entry.get("id", "")
            has_positive = False
            if entry_id.startswith("auto_"):
                try:
                    content_id = int(entry_id[5:])
                    has_positive = _has_positive_engagement_signal(content_id)
                except (ValueError, TypeError):
                    pass

            if has_positive:
                entry["winner_reason"] = "positive_reply"
                entry["is_active"] = True
                reason_set += 1
                kept += 1
            else:
                entry["winner_reason"] = WINNER_REASON_UNCONFIRMED
                if currently_active:
                    entry["is_active"] = False
                    deactivated += 1
                    reason_set += 1
                else:
                    already_inactive += 1
            continue

        # --- Unknown source: deactivate if no valid reason ---
        existing_reason = entry.get("winner_reason")
        if existing_reason in VALID_WINNER_REASONS:
            kept += 1
        else:
            entry["winner_reason"] = WINNER_REASON_UNCONFIRMED
            if currently_active:
                entry["is_active"] = False
                deactivated += 1
            else:
                already_inactive += 1

    _save_library(library)

    summary = {
        "kept_active": kept,
        "deactivated": deactivated,
        "already_inactive": already_inactive,
        "reason_set": reason_set,
        "library_size": len(library),
    }
    log.info("winners_cleanup_complete", extra=summary)
    return summary
