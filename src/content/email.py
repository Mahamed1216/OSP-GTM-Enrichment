"""Email generation with Sonnet 4.6 + few-shot from winning examples."""
import logging
import time

from src.secrets import get_secret

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config import settings
from src.context import format_lead_context
from src.content.winners import load_top_negatives, load_top_winners_for
from src.db import session_scope
from src.icp_config import load_workspace_icp_config
from src.llm import generate_json
from src.models import Enrichment, GeneratedContent, Lead, Score
from src.prompts.email import (
    PROMPT_VERSION,
    build_system as build_email_system,
    current_email_prompt_fingerprint,
)
from src.prompts.sanitize import (
    coerce_sdr_direct_pitch,
    coerce_structure_1_direct_plus_lookalike,
    coerce_structure_1_lookalike_accounts,
    coerce_structure_1_named_buyers,
    coerce_structure_1_single_named_buyer,
    coerce_structure_1_trigger_segment,
    detect_banned_direct_pitch_phrases,
    detect_broad_buyer_fallback,
    detect_competitor_as_buyer,
    detect_partner_channel_mismatch,
    detect_segment_vs_company_mismatch,
    prune_stale_named_signals,
    sanitize_generated_text,
    strip_structure_1_opening_hook,
)

log = logging.getLogger(__name__)


def _load_signal(session, lead_id: int, signal_type: str) -> dict | None:
    """Read a LeadSignal of the given type for a lead as a plain dict (in-session).

    Returns None when there is no signal or the table doesn't exist yet
    (pre-migration DB) — content generation must never break on its absence.
    """
    try:
        from src.models import LeadSignal
        row = session.execute(
            select(LeadSignal).where(
                LeadSignal.lead_id == lead_id,
                LeadSignal.signal_type == signal_type,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "signal_found": bool(row.signal_found),
            "signal_strength": row.signal_strength,
            "relevant_roles": list(row.relevant_roles or []),
            "relevant_departments": list(row.relevant_departments or []),
            "summary": row.summary,
            "why_it_matters": row.why_it_matters,
            "recommended_email_angle": row.recommended_email_angle,
        }
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("signal_load_failed", extra={"lead_id": lead_id, "type": signal_type, "error": str(exc)})
        return None


def _load_hiring_signal(session, lead_id: int) -> dict | None:
    return _load_signal(session, lead_id, "hiring")


def _load_source_signal(session, lead_id: int) -> dict | None:
    return _load_signal(session, lead_id, "source_import")


class EmailResult(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    signals_cited: list[str] = Field(default_factory=list)


async def generate_email(
    lead_id: int,
    *,
    regeneration_feedback: str | None = None,
    workspace_id: int | None = None,
) -> EmailResult | None:
    # Cost gate (defense-in-depth). Email is ON by default, so this is a no-op
    # in normal operation; it only skips when a workspace explicitly disables
    # email generation. Returns None so callers treat it as "skipped_disabled".
    from src.icp_config import is_content_type_enabled
    if not is_content_type_enabled("email", workspace_id):
        log.info("email_skipped_disabled", extra={"lead_id": lead_id, "workspace_id": workspace_id})
        return None

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
        # Hiring signal (C-tier rescue layer) + imported source signal — read
        # inside the session so the email prompt can lead with either angle.
        hiring_signal = _load_hiring_signal(session, lead_id)
        source_signal = _load_source_signal(session, lead_id)
        user_msg = format_lead_context(
            lead, enrichment, score,
            hiring_signal=hiring_signal, source_signal=source_signal,
        )
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

        # v3 buyer fallback ladder fields.
        # `buyer_fallback_mode` is None for enrichment rows created before v3.
        # When None, fall back to the old explicit_buyer_accounts logic below.
        buyer_fallback_mode: str | None = ba.get("buyer_fallback_mode") or None
        v3_direct: list[str] = [
            a.strip() for a in (ba.get("direct_buyer_accounts") or []) if a and a.strip()
        ]
        v3_lookalike: list[str] = [
            a.strip() for a in (ba.get("lookalike_buyer_accounts") or []) if a and a.strip()
        ]
        v3_trigger: list[str] = [
            s.strip() for s in (ba.get("trigger_based_buyer_segments") or []) if s and s.strip()
        ]

        # Legacy coercer inputs (used when buyer_fallback_mode is None).
        import re as _re
        _brand_re = _re.compile(r"^[A-Z][\w\.\-'&]*(?:\s+[A-Z][\w\.\-'&]*){0,3}$")
        explicit_buyer_accounts: list[str] = list(ba.get("likely_buyer_accounts") or [])
        if not explicit_buyer_accounts:
            explicit_buyer_accounts = [
                n for n in direct_buyers if _brand_re.match((n or "").strip())
            ]
        buyer_confidence: str = (
            (ba.get("buyer_confidence") or ba.get("buyer_account_confidence") or "low")
        ).strip().lower()
        buyer_segments: list[str] = [
            s.strip() for s in list(ba.get("likely_buyer_segments") or []) if s and s.strip()
        ]
        if len(buyer_segments) < 2:
            backfill = [
                s.strip() for s in direct_buyers
                if s and s.strip() and not _brand_re.match(s.strip())
            ]
            for seg in backfill:
                if seg not in buyer_segments:
                    buyer_segments.append(seg)

        company_industry = lead.industry or None
        lead_first_name = (lead.first_name or "").strip()
        lead_company = (lead.company or "").strip()

        # Flag for needs_review short-circuit (resolved after session closes).
        is_needs_buyer_research = (buyer_fallback_mode == "needs_review")

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
                prompt_fingerprint=current_email_prompt_fingerprint(workspace_id),
                model="rule:icp_skip",
                skip_reason="tier_below_threshold",
                workspace_id=workspace_id,
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

    # Needs-buyer-research short-circuit: enrichment ran but could not find
    # any direct buyer accounts, lookalike accounts, or trigger-based segments.
    # Saving a normal email here would use broad team fallbacks that read as
    # AI-written and obvious. Save a NEEDS REVIEW marker instead and return.
    if is_needs_buyer_research:
        nr_subject = ""
        nr_body = (
            "NEEDS REVIEW: No direct buyer account, lookalike buyer account, "
            "or trigger based buyer segment found."
        )
        nr_signals = ["Needs buyer research"]
        with session_scope() as session:
            session.add(GeneratedContent(
                lead_id=lead_id,
                kind="email",
                subject=nr_subject,
                body=nr_body,
                signals_cited=nr_signals,
                prompt_version=PROMPT_VERSION,
                prompt_fingerprint=current_email_prompt_fingerprint(workspace_id),
                model="rule:needs_buyer_research",
                skip_reason="needs_buyer_research",
                workspace_id=workspace_id,
            ))
        log.info(
            "email_needs_buyer_research",
            extra={"lead_id": lead_id},
        )
        return EmailResult(
            subject=nr_subject or "(needs review)",
            body=nr_body,
            signals_cited=nr_signals,
        )

    winners = load_top_winners_for("email", k=3, workspace_id=workspace_id)
    negatives = load_top_negatives("email", k=2, workspace_id=workspace_id)
    icp = load_workspace_icp_config(workspace_id)
    sender_first_name = get_secret("SENDER_FIRST_NAME", "Mohammed")
    system = build_email_system(winners, negatives, icp, sender_first_name=sender_first_name, workspace_id=workspace_id)
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

    # Buyer coercer — route to the appropriate Structure 1 rewrite based on
    # buyer_fallback_mode. For v3 enrichment rows the mode is explicit; for
    # old rows (buyer_fallback_mode is None) fall through to the legacy
    # coercers so backward compat is preserved.
    named_buyer_warning: str | None = None
    single_named_buyer_warning: str | None = None

    if buyer_fallback_mode == "direct_accounts" and len(v3_direct) >= 2:
        # CASE 1: 2 confirmed direct buyer accounts.
        coerced, named_buyer_warning = coerce_structure_1_named_buyers(
            clean_body, lead_company=lead_company, buyer_accounts=v3_direct[:2],
        )
        clean_body = coerced

    elif buyer_fallback_mode == "direct_plus_lookalike" and v3_direct and v3_lookalike:
        # CASE 2: 1 confirmed direct buyer + 1 lookalike.
        coerced, named_buyer_warning = coerce_structure_1_direct_plus_lookalike(
            clean_body,
            lead_company=lead_company,
            direct_buyer=v3_direct[0],
            lookalike_buyer=v3_lookalike[0],
        )
        clean_body = coerced

    elif buyer_fallback_mode == "lookalike_accounts" and len(v3_lookalike) >= 2:
        # CASE 3: 2 lookalike accounts (no confirmed direct buyer).
        coerced, named_buyer_warning = coerce_structure_1_lookalike_accounts(
            clean_body, lead_company=lead_company, lookalike_accounts=v3_lookalike[:2],
        )
        clean_body = coerced

    elif buyer_fallback_mode == "trigger_segment" and v3_trigger:
        # CASE 4: trigger-based segment only.
        coerced, named_buyer_warning = coerce_structure_1_trigger_segment(
            clean_body, lead_company=lead_company, trigger_segment=v3_trigger[0],
        )
        clean_body = coerced

    else:
        # buyer_fallback_mode is None (old enrichment row) — use legacy coercers.
        # Named-buyer coercer: rewrite intro + CTA to use the 2 named buyers
        # verbatim. No-op when there aren't 2 named buyers OR when both names
        # already appear in the body OR when the body is Structure 2.
        coerced, named_buyer_warning = coerce_structure_1_named_buyers(
            clean_body, lead_company=lead_company, buyer_accounts=explicit_buyer_accounts,
        )
        clean_body = coerced

        # Hybrid coercer — fires when buyer discovery surfaced EXACTLY ONE
        # named buyer at medium-or-better confidence. Skip when the single
        # candidate is flagged as a competitor.
        if (
            len(explicit_buyer_accounts) == 1
            and buyer_confidence in ("medium", "high")
            and explicit_buyer_accounts[0].strip()
        ):
            candidate = explicit_buyer_accounts[0].strip()
            competitor_hit_set = {c.strip().lower() for c in flagged_competitors if c}
            if candidate.lower() not in competitor_hit_set:
                single_body, single_named_buyer_warning = coerce_structure_1_single_named_buyer(
                    clean_body,
                    lead_company=lead_company,
                    named_buyer=candidate,
                    segments=buyer_segments,
                )
                clean_body = single_body

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

    # Broad buyer fallback validator — flag when the intro uses banned phrases
    # like "sales teams" or "founders" instead of specific named accounts or
    # trigger-based segments.
    broad_fallback_hits = detect_broad_buyer_fallback(clean_body)
    if broad_fallback_hits:
        validation_warnings.append(
            "Broad buyer fallback phrases detected in intro — rewrite with "
            "named accounts or trigger segments: "
            + ", ".join(f'"{p}"' for p in broad_fallback_hits)
        )
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
            prompt_fingerprint=current_email_prompt_fingerprint(workspace_id),
            model=settings.content_model,
            workspace_id=workspace_id,
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
