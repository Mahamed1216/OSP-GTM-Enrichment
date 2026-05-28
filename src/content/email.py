"""Email generation with Sonnet 4.6 + few-shot from winning examples."""
import logging
import time

from app.lib.config import get_secret

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.content.winners import load_top_negatives, load_top_winners_for
from src.db import session_scope
from src.icp_config import load_icp_config
from src.llm import generate_json
from src.models import Enrichment, GeneratedContent, Lead, Score
from src.prompts.email import (
    PROMPT_VERSION,
    build_system as build_email_system,
    current_email_prompt_fingerprint,
)
from src.prompts.sanitize import (
    coerce_sdr_direct_pitch,
    coerce_structure_1_named_buyers,
    coerce_structure_1_single_named_buyer,
    detect_banned_direct_pitch_phrases,
    detect_competitor_as_buyer,
    detect_partner_channel_mismatch,
    detect_segment_vs_company_mismatch,
    prune_stale_named_signals,
    sanitize_generated_text,
    strip_structure_1_opening_hook,
)

log = logging.getLogger(__name__)


class EmailResult(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    signals_cited: list[str] = Field(default_factory=list)


async def generate_email(
    lead_id: int,
    *,
    regeneration_feedback: str | None = None,
) -> EmailResult:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        enrichment = session.execute(
            select(Enrichment).where(Enrichment.lead_id == lead_id)
        ).scalar_one_or_none()
        score = session.execute(
            select(Score).where(Score.lead_id == lead_id)
        ).scalar_one_or_none()
        # ICP-skip path: tier "D" is reserved for confirmed B2C / non-fit
        # leads. Snapshot the flag here so we can short-circuit AFTER the
        # session closes (no detached-attribute access on the score row).
        is_icp_skip = bool(score and getattr(score, "tier", None) == "D")
        user_msg = format_lead_context(lead, enrichment, score)
        user_msg += "\n\nWrite the email now. Output JSON only."
        # Snapshot buyer-account fields for the post-generation validators.
        # Read INSIDE the session so we never touch detached attributes.
        # Prefer the v2 fields (likely_direct_buyers, buyer_motion, partner
        # channels) for the new validators; fall back to v1 fields for the
        # legacy segment-vs-company validator.
        ba = (enrichment.buyer_accounts or {}) if enrichment else {}
        direct_buyers: list[str] = list(ba.get("likely_direct_buyers") or [])
        partner_channels: list[str] = list(ba.get("likely_partner_channels") or [])
        buyer_motion: str | None = ba.get("buyer_motion") or None
        # Combine v2 direct + v1 accounts for the segment-mismatch validator's
        # "named buyer accounts" lookup. v1 rows survive a schema upgrade
        # this way.
        named_buyers: list[str] = direct_buyers + list(
            ba.get("likely_buyer_accounts") or []
        )
        flagged_competitors: list[str] = list(ba.get("flagged_competitors") or [])
        # Explicit `likely_buyer_accounts` field from buyer discovery v2
        # — the canonical "2 named buyers" pair for Structure 1 CASE A.
        # Fall back to v2 `likely_direct_buyers` (filtered to titlecase
        # brand-shaped entries) when the dedicated field is empty, so old
        # enrichment rows still drive the coercer.
        explicit_buyer_accounts: list[str] = list(
            ba.get("likely_buyer_accounts") or []
        )
        if not explicit_buyer_accounts:
            # Heuristic backfill: from direct_buyers, prefer items that
            # look like brand names (Titlecase, no plural noun-phrase shape).
            import re as _re
            brand_re = _re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+[A-Z][\w\.\-'&]*){0,3}$")
            explicit_buyer_accounts = [
                n for n in direct_buyers if brand_re.match((n or "").strip())
            ]
        # Confidence rating governs whether we anchor on the single
        # named buyer. v2 uses `buyer_confidence`; v1 used
        # `buyer_account_confidence`. Either being "high" is enough.
        buyer_confidence: str = (
            (ba.get("buyer_confidence") or ba.get("buyer_account_confidence") or "low")
        ).strip().lower()
        # Buyer SEGMENT pool for the hybrid "1 named buyer + 2 segments"
        # rewrite. Prefer v1 `likely_buyer_segments` (segment-only
        # strings); fall back to segment-shaped entries in
        # `likely_direct_buyers` (lowercased / multi-word non-brand
        # items) so v2-only rows still get hybrid coverage.
        import re as _re_seg
        _brand_only_re = _re_seg.compile(
            r"^[A-Z][\w\.\-'&]*(?:\s+[A-Z][\w\.\-'&]*){0,3}$"
        )
        buyer_segments: list[str] = [
            s.strip()
            for s in list(ba.get("likely_buyer_segments") or [])
            if s and s.strip()
        ]
        if len(buyer_segments) < 2:
            backfill = [
                s.strip()
                for s in direct_buyers
                if s and s.strip() and not _brand_only_re.match(s.strip())
            ]
            for seg in backfill:
                if seg not in buyer_segments:
                    buyer_segments.append(seg)
        company_industry = lead.industry or None
        lead_first_name = (lead.first_name or "").strip()
        lead_company = (lead.company or "").strip()

    # ICP-skip short-circuit: tier "D" means scoring already decided
    # this is a poor OSP fit (typically B2C without B2B motion). Skip
    # the LLM call entirely, persist a canned skip record, and return.
    if is_icp_skip:
        skip_subject = ""
        skip_body = "SKIP: Primary motion appears B2C. No clear B2B outbound motion found."
        skip_signals = ["B2C motion, poor OSP fit"]
        with session_scope() as session:
            session.add(GeneratedContent(
                lead_id=lead_id,
                kind="email",
                subject=skip_subject,
                body=skip_body,
                signals_cited=skip_signals,
                prompt_version=PROMPT_VERSION,
                prompt_fingerprint=current_email_prompt_fingerprint(),
                model="rule:icp_skip",
                skip_reason="tier_below_threshold",
            ))
        log.info(
            "email_icp_skip",
            extra={"lead_id": lead_id, "reason": "tier_D_b2c_no_b2b_motion"},
        )
        return EmailResult(
            subject=skip_subject or "(skip)",
            body=skip_body,
            signals_cited=skip_signals,
        )

    winners = load_top_winners_for("email", k=3)
    negatives = load_top_negatives("email", k=2)
    icp = load_icp_config()
    sender_first_name = get_secret("SENDER_FIRST_NAME", "Mohammed")
    system = build_email_system(winners, negatives, icp, sender_first_name=sender_first_name)
    if regeneration_feedback:
        system = (
            "Previous version of this content was rated negatively. "
            f"Reason: {regeneration_feedback.strip()}. Address this in your output.\n\n"
            + system
        )

    start = time.monotonic()
    result = await generate_json(
        model=settings.content_model,
        system=system,
        user=user_msg,
        schema=EmailResult,
        max_tokens=1000,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    clean_subject = sanitize_generated_text(result.subject, sender_first_name)
    clean_body = sanitize_generated_text(result.body, sender_first_name)

    # SDR-pitch coercer — deterministic rewrite when the body talks
    # about SDR/BDR hiring AND the model leaked legacy phrasing ("I
    # could fill", "0 onboarding time", "fraction of the cost",
    # "meetings and pipeline next week") instead of using the canonical
    # two-paragraph template ending on "Want to meet one of them?".
    # The coercer is a no-op when the body either doesn't mention
    # SDR/BDR hiring (different signal) or already follows the canonical
    # pattern (correct output). Runs BEFORE the validators so they see
    # the final body the operator will actually read.
    coerced_body, coerce_warning = coerce_sdr_direct_pitch(
        clean_body, first_name=lead_first_name,
    )
    clean_body = coerced_body

    # Structure 1 opening-hook stripper. Removes preamble sentences like
    # "Mindsmith looks built for L&D teams..." that land between the
    # greeting and the "Not sure if you're already working with..."
    # starter. No-op when the body doesn't have a Structure 1 starter,
    # or when the body is a Structure 2 direct pitch (the SDR coercer
    # above already produced the canonical pattern in that case).
    stripped_body, strip_warning = strip_structure_1_opening_hook(clean_body)
    clean_body = stripped_body

    # Named-buyer coercer: when buyer discovery surfaced ≥2 high-conf
    # buyer companies but the model wrote a segment-only Structure 1
    # intro (e.g. "community banks, commercial lenders, broker
    # networks"), rewrite the intro + CTA to use the 2 named buyers
    # verbatim. No-op when there aren't 2 named buyers OR when both
    # names already appear in the body OR when the body is Structure 2.
    named_buyer_body, named_buyer_warning = coerce_structure_1_named_buyers(
        clean_body,
        lead_company=lead_company,
        buyer_accounts=explicit_buyer_accounts,
    )
    clean_body = named_buyer_body

    # Hybrid Structure 1 coercer — fires when buyer discovery surfaced
    # EXACTLY ONE high-confidence named buyer (the 2-buyer coercer above
    # is a no-op in that case because it needs a pair). Don't force a
    # fake second name; don't ignore the real one. Skip when the single
    # candidate is flagged as a competitor — those are never buyers.
    single_named_buyer_warning: str | None = None
    if (
        len(explicit_buyer_accounts) == 1
        and buyer_confidence == "high"
        and explicit_buyer_accounts[0].strip()
    ):
        candidate = explicit_buyer_accounts[0].strip()
        competitor_hit_set = {c.strip().lower() for c in flagged_competitors if c}
        if candidate.lower() not in competitor_hit_set:
            single_body, single_warning = coerce_structure_1_single_named_buyer(
                clean_body,
                lead_company=lead_company,
                named_buyer=candidate,
                segments=buyer_segments,
            )
            clean_body = single_body
            single_named_buyer_warning = single_warning

    # Post-generation validators. These are HEURISTIC flags, not hard
    # blocks — the email still saves, but warnings ride along on
    # `signals_cited` so the operator can review on the Lead detail page
    # and choose to regenerate.
    validation_warnings: list[str] = []
    if coerce_warning:
        validation_warnings.append(coerce_warning)
    if strip_warning:
        validation_warnings.append(strip_warning)
    if named_buyer_warning:
        validation_warnings.append(named_buyer_warning)
    if single_named_buyer_warning:
        validation_warnings.append(single_named_buyer_warning)
    seg_warnings = detect_segment_vs_company_mismatch(
        clean_body, named_buyer_accounts=named_buyers,
    )
    validation_warnings.extend(seg_warnings)
    competitor_hits = detect_competitor_as_buyer(
        clean_body,
        company_industry=company_industry,
        competitor_seed=flagged_competitors,
    )
    if competitor_hits:
        validation_warnings.append(
            "Possible competitor named as buyer: " + ", ".join(competitor_hits)
        )
    # B2C / partner-channel framing validator. Surfaces both the
    # "B2C motion but body uses buyer language" mistake and the
    # "partner framing but CTA says 'companies like them'" mistake.
    partner_warnings = detect_partner_channel_mismatch(
        clean_body,
        buyer_motion=buyer_motion,
        likely_direct_buyers=direct_buyers,
        likely_partner_channels=partner_channels,
    )
    validation_warnings.extend(partner_warnings)
    # Banned over-explanation phrases on direct SDR/hiring pitches.
    # These were the "real tension" / "no ramp" / "weeks not months"
    # leaks called out in operator feedback.
    banned_hits = detect_banned_direct_pitch_phrases(clean_body)
    if banned_hits:
        validation_warnings.append(
            "Banned over-explanation phrases in body: "
            + ", ".join(banned_hits)
        )
    # Drop stale named-customer signals — bare brand entries in
    # `signals_cited` ("Stack AI", "Zendesk") that aren't present in
    # the final body. The named-buyer coercer above may have rewritten
    # the intro; LLM-cited customer names that didn't make it into the
    # rewritten body shouldn't ride along in signals_cited either.
    pruned_signals, dropped_signals = prune_stale_named_signals(
        clean_body, list(result.signals_cited or []),
    )
    if dropped_signals:
        validation_warnings.append(
            "Pruned stale named-customer signals not present in body: "
            + ", ".join(dropped_signals)
        )

    # Attach as prefixed entries to signals_cited so existing UIs that
    # render the field show them without a schema change. Prefix makes
    # them filterable downstream.
    signals_with_warnings = pruned_signals + [
        f"validator:{w}" for w in validation_warnings
    ]

    with session_scope() as session:
        session.add(GeneratedContent(
            lead_id=lead_id,
            kind="email",
            subject=clean_subject,
            body=clean_body,
            signals_cited=signals_with_warnings,
            prompt_version=PROMPT_VERSION,
            prompt_fingerprint=current_email_prompt_fingerprint(),
            model=settings.content_model,
        ))

    if validation_warnings:
        log.warning(
            "email_validation_warnings",
            extra={"lead_id": lead_id, "warnings": validation_warnings},
        )

    log.info("email_generated", extra={
        "lead_id": lead_id,
        "duration_ms": duration_ms,
        "signals_count": len(result.signals_cited),
        "subject_chars": len(result.subject),
        "body_chars": len(result.body),
    })
    return result
