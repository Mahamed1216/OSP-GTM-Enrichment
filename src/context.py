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
    max_personal_posts: int = 5,
    max_company_posts: int = 3,
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

    posts = enrichment.linkedin_posts or []
    if posts:
        parts.append("\n## Recent personal posts")
        for p in posts[:max_personal_posts]:
            text = _truncate(p.get("text"), 300)
            if text:
                date = p.get("posted_at") or ""
                parts.append(f"- [{date}] {text}" if date else f"- {text}")

    if enrichment.company_details:
        cd = enrichment.company_details
        parts.append("\n## Company")
        desc = _truncate(cd.get("description"), 500)
        if desc:
            parts.append(f"- {desc}")
        if cd.get("employee_count"):
            parts.append(f"- Employees: {cd.get('employee_count')}")

    cposts = enrichment.company_posts or []
    if cposts:
        parts.append("\n## Company posts")
        for p in cposts[:max_company_posts]:
            text = _truncate(p.get("text"), 250)
            if text:
                parts.append(f"- {text}")

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

    return "\n".join(parts)
