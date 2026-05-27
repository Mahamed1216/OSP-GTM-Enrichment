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
        # Surface ALL four buyer-discovery fields the email prompt expects,
        # plus flagged competitors so the model can avoid naming them.
        # The email prompt branches on whether named accounts exist + the
        # confidence — keep the formatting machine-readable enough for
        # that branching to be unambiguous.
        accounts = ba.get("likely_buyer_accounts") or []
        segments = ba.get("likely_buyer_segments") or []
        confidence = ba.get("buyer_account_confidence") or "low"
        rationale = (ba.get("buyer_account_rationale") or "").strip()
        flagged = ba.get("flagged_competitors") or []
        parts.append("\n## Buyer accounts (research)")
        parts.append(
            f"- likely_buyer_accounts: "
            + (", ".join(accounts) if accounts else "(none)")
        )
        parts.append(
            f"- likely_buyer_segments: "
            + (", ".join(segments) if segments else "(none)")
        )
        parts.append(f"- buyer_account_confidence: {confidence}")
        if rationale:
            parts.append(f"- buyer_account_rationale: {rationale}")
        if flagged:
            parts.append(
                f"- DO NOT NAME (competitors): {', '.join(flagged)}"
            )

    return "\n".join(parts)
