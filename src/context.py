"""Format Lead + Enrichment + Score into a markdown block for LLM prompts.

Centralized so scoring and content modules see consistent context shape.
"""
from typing import Optional

from src.models import Enrichment, Lead, Score


def _truncate(text: Optional[str], n: int) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    return text[:n] + ("..." if len(text) > n else "")


def format_lead_context(
    lead: Lead,
    enrichment: Optional[Enrichment] = None,
    score: Optional[Score] = None,
    *,
    include_score: bool = True,
    max_news: int = 5,
) -> str:
    parts: list[str] = [
        "# Lead",
        f"- Name: {lead.first_name} {lead.last_name}",
        f"- Title: {lead.title}",
        f"- Company: {lead.company}",
        f"- Industry: {lead.industry or 'unknown'}",
    ]
    if lead.company_domain:
        parts.append(f"- Domain: {lead.company_domain}")

    if include_score and score:
        parts.append(f"\n# Score: {score.score} ({score.tier})")
        parts.append(f"Rationale: {score.rationale}")
        if score.signals_used:
            parts.append(f"Signals: {', '.join(score.signals_used)}")

    if not enrichment:
        return "\n".join(parts)

    if enrichment.linkedin_profile:
        prof = enrichment.linkedin_profile
        parts.append("\n## LinkedIn profile")
        if prof.get("headline"):
            parts.append(f"- Headline: {prof.get('headline')}")
        about = _truncate(prof.get("about"), 600)
        if about:
            parts.append(f"- About: {about}")

    if enrichment.company_details:
        cd = enrichment.company_details
        parts.append("\n## Company")
        desc = _truncate(cd.get("description"), 500)
        if desc:
            parts.append(f"- {desc}")
        if cd.get("employee_count"):
            parts.append(f"- Employees: {cd.get('employee_count')}")

    cnews = enrichment.company_news or []
    if cnews:
        parts.append("\n## Company news")
        for n in cnews[:max_news]:
            snippet = _truncate(n.get("snippet"), 180) or ""
            parts.append(f"- {n.get('title')}: {snippet}")

    inews = enrichment.industry_news or []
    if inews:
        parts.append("\n## Industry news")
        for n in inews[:max_news]:
            parts.append(f"- {n.get('title')}")

    ba = enrichment.buyer_accounts or {}
    if ba:
        # v2 fields drive the email prompt's branching. v1 fields are
        # also surfaced for backward compat with rows persisted under
        # the original schema.
        motion = ba.get("buyer_motion") or "unknown"
        direct = ba.get("likely_direct_buyers") or []
        partners = ba.get("likely_partner_channels") or []
        referrals = ba.get("likely_referral_channels") or []
        end_users = ba.get("likely_end_users") or []
        buyer_conf = ba.get("buyer_confidence") or "low"
        partner_conf = ba.get("partner_confidence") or "low"
        reasoning = (ba.get("reasoning") or "").strip()
        flagged = ba.get("flagged_competitors") or []
        # v1 fields (legacy fallback for old enrichment rows).
        legacy_accounts = ba.get("likely_buyer_accounts") or []
        legacy_segments = ba.get("likely_buyer_segments") or []
        legacy_conf = ba.get("buyer_account_confidence") or "low"
        legacy_rationale = (ba.get("buyer_account_rationale") or "").strip()

        # v3 fallback ladder fields.
        fallback_mode = ba.get("buyer_fallback_mode") or ""
        v3_direct = ba.get("direct_buyer_accounts") or []
        v3_lookalike = ba.get("lookalike_buyer_accounts") or []
        v3_trigger = ba.get("trigger_based_buyer_segments") or []
        research_rationale = (ba.get("buyer_research_rationale") or "").strip()

        parts.append("\n## Buyer accounts (research)")
        parts.append(f"- buyer_motion: {motion}")

        # Surface v3 fallback ladder when fields are populated (new rows).
        if fallback_mode:
            parts.append(f"- buyer_fallback_mode: {fallback_mode}")
            parts.append(
                "- direct_buyer_accounts: "
                + (", ".join(v3_direct) if v3_direct else "(none)")
            )
            parts.append(
                "- lookalike_buyer_accounts: "
                + (", ".join(v3_lookalike) if v3_lookalike else "(none)")
            )
            parts.append(
                "- trigger_based_buyer_segments: "
                + (", ".join(v3_trigger) if v3_trigger else "(none)")
            )
            if research_rationale:
                parts.append(f"- buyer_research_rationale: {research_rationale}")

        # Always surface v2 fields for the partner-channel framing case.
        parts.append(
            "- likely_direct_buyers: "
            + (", ".join(direct) if direct else "(none)")
        )
        parts.append(
            "- likely_partner_channels: "
            + (", ".join(partners) if partners else "(none)")
        )
        parts.append(
            "- likely_referral_channels: "
            + (", ".join(referrals) if referrals else "(none)")
        )
        if end_users:
            parts.append("- likely_end_users: " + ", ".join(end_users))
        parts.append(f"- buyer_confidence: {buyer_conf}")
        parts.append(f"- partner_confidence: {partner_conf}")
        if reasoning:
            parts.append(f"- reasoning: {reasoning}")
        if flagged:
            parts.append(
                f"- DO NOT NAME (competitors): {', '.join(flagged)}"
            )
        # Legacy block only when both v2 and v3 fields are empty (old rows).
        v2_empty = (
            motion == "unknown" and not direct and not partners
            and not referrals and not end_users
        )
        if v2_empty and not fallback_mode and (legacy_accounts or legacy_segments):
            parts.append(
                "- legacy.likely_buyer_accounts: "
                + (", ".join(legacy_accounts) if legacy_accounts else "(none)")
            )
            parts.append(
                "- legacy.likely_buyer_segments: "
                + (", ".join(legacy_segments) if legacy_segments else "(none)")
            )
            parts.append(f"- legacy.buyer_account_confidence: {legacy_conf}")
            if legacy_rationale:
                parts.append(f"- legacy.buyer_account_rationale: {legacy_rationale}")

    return "\n".join(parts)
