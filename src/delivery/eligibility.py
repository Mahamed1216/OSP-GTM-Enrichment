"""Phase 9d — bulk push eligibility filter.

Pure function: given a list of lead ids and a DB session, returns
(eligible_ids, skipped_by_reason). Used by the Leads page bulk-push
UI and the matching test.

Skip reasons (stable string codes — UI maps to friendly labels):
- missing_email        : lead.email empty
- email_unverified     : verifier configured AND status != "valid"
                         AND not "risky-with-strict-off"
- below_tier           : no Score row OR tier below settings.send_min_tier
- no_content           : no email-kind GeneratedContent with a body
- already_sent         : latest content.delivery_status == "sent"
                         AND delivery_id present
- in_progress          : latest content.delivery_status == "in_progress"
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.models import GeneratedContent, Lead, Score


SKIP_LABELS: dict[str, str] = {
    "missing_email": "missing email",
    "email_unverified": "email unverified",
    "below_tier": "below tier gate",
    "no_content": "missing content",
    "unsafe_content": "needs review / regeneration",
    "already_sent": "already sent",
    "in_progress": "send in progress",
}


# Internal-only / placeholder markers that must NEVER reach a prospect or be
# pushed to Instantly. These are written into GeneratedContent.body by the
# generator as status records (e.g. the buyer-research "NEEDS REVIEW: ..."
# marker and the ICP-skip "SKIP: ..." marker). The send paths used to gate
# only on tier/dedupe/verify/has-content, so a sendable-tier lead with such a
# placeholder could have the marker delivered. This is the single source of
# truth for "this content is internal and unsafe to send".
_INTERNAL_CONTENT_MARKERS: tuple[str, ...] = (
    "needs review",             # buyer-research placeholder ("NEEDS REVIEW: ...")
    "no direct buyer account",
    "lookalike buyer account",
    "trigger based buyer",
)


def is_unsafe_internal_content(subject: str | None, body: str | None) -> bool:
    """Return True when an email's subject/body is empty or contains internal
    review / placeholder text that must never be sent to a prospect or pushed
    to Instantly.

    Shared by every pre-send path (deliver_email, overwrite_lead_copy, the
    bulk-push eligibility filter, and the debug push diagnostic) so the safety
    rule is enforced identically everywhere. Failure code: unsafe_content /
    unsafe_internal_review_content.
    """
    stripped = (body or "").strip()
    if not stripped:
        return True  # empty/whitespace body is never sendable
    combined = f"{subject or ''}\n{body or ''}".lower()
    if any(marker in combined for marker in _INTERNAL_CONTENT_MARKERS):
        return True
    # ICP-skip / explicit placeholder markers (defense in depth).
    if stripped.startswith("SKIP:"):
        return True
    if stripped.lower() in ("(needs review)", "(skip)"):
        return True
    return False


def _verifier_configured() -> bool:
    """Phase 9d treats verification as required only when a verifier is
    actively configured. The `email_verifier` setting defaults to
    'instantly' and is always non-empty, so we key off whether the
    user opted into strict verification semantics."""
    # Conservative: if `email_verifier` is set we honor whatever
    # status is on the lead; the default Instantly verifier writes the
    # status back during verify_email. Leads with no status yet are
    # treated as unverified — matches the deliver_email guard order
    # (which calls verify_email itself when running the full pipeline,
    # but bulk push assumes verification was already attempted).
    return bool(settings.email_verifier)


def _is_verified(lead: Lead) -> bool:
    """Accept verification from any source. Apollo writes "Verified",
    our internal verifier writes "valid", other vendors may use their
    own strings — if a provider name AND a verified_at timestamp are
    both present, we trust whoever verified it."""
    status = (lead.email_verification_status or "").strip().lower()
    if status in ("verified", "valid"):
        return True
    if (lead.email_verification_provider or "").strip() and lead.email_verified_at:
        return True
    return False


def filter_eligible(
    lead_ids: list[int],
    session: Session,
) -> tuple[list[int], dict[str, list[int]]]:
    """Categorize each lead into eligible or one skip bucket.

    Returns:
        (eligible_ids, skipped_by_reason) where each lead appears in
        exactly one of the two outputs (lead order in skipped buckets
        matches input order).
    """
    eligible: list[int] = []
    skipped: dict[str, list[int]] = {code: [] for code in SKIP_LABELS}

    if not lead_ids:
        return eligible, skipped

    verifier_on = _verifier_configured()

    leads = {
        l.id: l for l in session.execute(
            select(Lead).where(Lead.id.in_(lead_ids))
        ).scalars().all()
    }
    scores = {
        s.lead_id: s for s in session.execute(
            select(Score).where(Score.lead_id.in_(lead_ids))
        ).scalars().all()
    }
    # Latest email-kind content per lead.
    contents = session.execute(
        select(GeneratedContent)
        .where(
            GeneratedContent.lead_id.in_(lead_ids),
            GeneratedContent.kind == "email",
        )
        .order_by(GeneratedContent.lead_id, GeneratedContent.id.desc())
    ).scalars().all()
    latest_content: dict[int, GeneratedContent] = {}
    for c in contents:
        latest_content.setdefault(c.lead_id, c)

    for lid in lead_ids:
        lead = leads.get(lid)
        if not lead or not (lead.email or "").strip():
            skipped["missing_email"].append(lid)
            continue

        if verifier_on and not _is_verified(lead):
            skipped["email_unverified"].append(lid)
            continue

        score = scores.get(lid)
        if not score or not settings.should_send(score.tier):  # type: ignore[arg-type]
            skipped["below_tier"].append(lid)
            continue

        content = latest_content.get(lid)
        if not content or not (content.body or "").strip():
            skipped["no_content"].append(lid)
            continue

        # Hard content-safety gate: never bulk-send internal review/placeholder
        # text (e.g. the "NEEDS REVIEW: ..." buyer-research marker).
        if is_unsafe_internal_content(content.subject, content.body):
            skipped["unsafe_content"].append(lid)
            continue

        if content.delivery_status == "sent" and (content.delivery_id or "").strip():
            skipped["already_sent"].append(lid)
            continue

        if content.delivery_status == "in_progress":
            skipped["in_progress"].append(lid)
            continue

        eligible.append(lid)

    return eligible, skipped


def summarize_skips(skipped: dict[str, list[int]]) -> str:
    """Format the skip breakdown for the UI caption.
    Returns e.g. '25 missing content, 10 below tier gate, 3 already sent'."""
    parts = [
        f"{len(ids)} {SKIP_LABELS[code]}"
        for code, ids in skipped.items()
        if ids
    ]
    return ", ".join(parts) if parts else "none"
