"""Hiring-signal research via Tavily + LLM classification.

Pipeline (mirrors src.enrichment.buyer_accounts):
  1. Tavily: a few hiring-focused queries built from company name/domain.
  2. Claude classifies the snippets into a HiringSignalResult.
  3. Result is persisted to the lead_signals table and a deterministic tier
     uplift is applied to the lead's Score row.

NEVER raises into the pipeline and NEVER sends/pushes anything. On any
failure (no Tavily key, no LLM key, empty results) it returns a not-found
result with a rationale so the caller can log it.
"""
from __future__ import annotations

import asyncio
import logging

from tavily import TavilyClient

from src.config import settings
from src.icp_config import load_workspace_icp_config
from src.llm import generate_json
from src.retry import retry_api
from src.signals.schemas import HiringSignalResult
from src.signals.store import (
    apply_hiring_uplift,
    has_hiring_signal,
    record_hiring_failure,
    upsert_hiring_signal,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query building (pure — unit-tested)
# ---------------------------------------------------------------------------

def build_hiring_queries(
    company_name: str | None,
    company_domain: str | None = None,
    company_website: str | None = None,
) -> list[str]:
    """Build hiring-focused Tavily queries from company name/domain.

    Uses a `site:` scope when a domain (or website) is available so we read
    the company's own careers pages first, then broader name-based queries.
    Returns an empty list when there is nothing to search on.
    """
    domain = _normalize_domain(company_domain or company_website)
    name = (company_name or "").strip()
    queries: list[str] = []

    if domain:
        queries.append(
            f'site:{domain} careers jobs hiring RevOps OR "Sales Operations" '
            f'OR SDR OR "Business Development" OR GTM OR "Growth" '
            f'OR "Revenue Operations" OR automation OR AI OR data'
        )
    if name:
        queries.append(
            f'{name} jobs RevOps "Sales Operations" SDR GTM Growth automation AI data'
        )
        queries.append(
            f'{name} careers "sales" "operations" "revenue" "growth" "automation"'
        )
        queries.append(
            f'{name} hiring "RevOps" OR "sales operations" OR "go-to-market"'
        )
    return queries


def _normalize_domain(value: str | None) -> str | None:
    """Strip scheme / path / www so a `site:` filter is clean."""
    if not value:
        return None
    v = value.strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    if v.startswith("www."):
        v = v[4:]
    v = v.split("/")[0].strip()
    return v or None


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------

def _client() -> TavilyClient:
    return TavilyClient(api_key=settings.tavily_api_key)


def _tavily_search_sync(query: str, max_results: int) -> list[dict]:
    return _client().search(
        query=query,
        max_results=max_results,
        search_depth="basic",
    ).get("results", [])


@retry_api
async def _tavily_search(query: str, max_results: int = 4) -> list[dict]:
    if not query.strip():
        return []
    return await asyncio.to_thread(_tavily_search_sync, query, max_results)


def _format_snippets(results: list[dict]) -> tuple[str, list[str]]:
    """Compact Tavily results into bullets; also return the source URLs found."""
    if not results:
        return "(no results)", []
    bullets: list[str] = []
    urls: list[str] = []
    for r in results[:6]:
        title = (r.get("title") or "").strip()
        snippet = (r.get("content") or r.get("snippet") or "").strip()[:300]
        url = (r.get("url") or "").strip()
        if url:
            urls.append(url)
        bullets.append(f"- {title}: {snippet} ({url})")
    return "\n".join(bullets), urls


# ---------------------------------------------------------------------------
# Classifier prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You analyze web-search snippets about a company's open job postings and
classify whether the company shows a strong HIRING signal that indicates
active GTM / revenue investment, timing, and likely pain. Output JSON ONLY,
conforming to the schema below.

WHY HIRING MATTERS: open roles in sales, revenue, RevOps, GTM, growth,
marketing ops, data, or automation are the single strongest "active
investment" signal — the company is spending money to build pipeline right
now, which means timing and budget.

STRENGTH RUBRIC:
  - "high"   — multiple relevant open roles, OR a clearly relevant leadership
               role (e.g. "Head of Revenue Operations", "SDR Manager"), OR
               a named active careers page listing GTM/RevOps/SDR roles.
  - "medium" — one relevant open role, or relevant hiring implied but thin.
  - "low"    — only tangential hiring (e.g. unrelated engineering/support)
               or a single weak mention.
  - "none"   — no hiring evidence in the snippets.

HIGH-WEIGHT roles (treat these as the most relevant — name them in
relevant_roles_found verbatim when present):
  RevOps, Revenue Operations, Sales Operations, SDR Manager, BDR Manager,
  Sales Development, Business Development, GTM Operations, Growth,
  Demand Generation, Marketing Operations, AI Automation, Sales Automation,
  Data Operations, Customer Success Operations, Implementation Operations.

RULES:
  1. relevant_roles_found: ONLY list role titles actually visible in the
     snippets. Never invent a role. Empty list if none.
  2. relevant_departments: from {sales, revenue, GTM, RevOps, operations,
     marketing, data, AI, support, customer success, engineering, other}.
  3. recency_estimate: last_30_days | last_90_days | last_180_days | unknown.
     Use "unknown" unless a date is clearly visible — search data is stale.
  4. source_urls: ONLY URLs present in the snippets. Never fabricate.
  5. why_it_matters: 1 sentence on what the hiring implies for outreach.
  6. recommended_email_angle: a short, plain-English angle the SDR could open
     with — referencing ONLY roles actually found. No fake certainty. Empty
     string if no real signal. Example: "Saw you're hiring for RevOps and an
     SDR lead — usually a sign the team is investing in outbound."
  7. tier_uplift_recommendation: your suggestion (none | C_to_B | C_to_A |
     B_to_A). This is only a hint; downstream rules make the final call.

Output schema (JSON, no prose):
{
  "hiring_signal_found": true | false,
  "hiring_signal_strength": "none | low | medium | high",
  "relevant_roles_found": ["..."],
  "relevant_departments": ["..."],
  "recency_estimate": "last_30_days | last_90_days | last_180_days | unknown",
  "why_it_matters": "<1 sentence>",
  "tier_uplift_recommendation": "none | C_to_B | C_to_A | B_to_A",
  "recommended_email_angle": "<short angle, real roles only>",
  "source_urls": ["..."]
}
"""


def _empty_result(reason: str) -> HiringSignalResult:
    return HiringSignalResult(
        hiring_signal_found=False,
        hiring_signal_strength="none",
        why_it_matters=reason,
    )


async def research_hiring_signal(
    company_name: str | None,
    *,
    company_domain: str | None = None,
    company_website: str | None = None,
    industry: str | None = None,
    icp_context: str | None = None,
) -> HiringSignalResult:
    """Run Tavily + LLM and return a HiringSignalResult. Never raises.

    Does NOT persist anything — that is enrich_hiring_signal's job.
    """
    queries = build_hiring_queries(company_name, company_domain, company_website)
    if not queries:
        return _empty_result("No company name or domain to research.")
    if not settings.tavily_api_key:
        return _empty_result("Tavily not configured — hiring research skipped.")
    if not settings.anthropic_api_key:
        return _empty_result("Anthropic not configured — hiring research skipped.")

    try:
        result_lists = await asyncio.gather(
            *[_tavily_search(q, max_results=4) for q in queries],
            return_exceptions=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("hiring_tavily_failed", extra={"company": company_name, "error": str(exc)})
        return _empty_result(f"Tavily search failed: {type(exc).__name__}")

    all_results: list[dict] = []
    for lst in result_lists:
        if isinstance(lst, Exception):
            continue
        all_results.extend(lst)

    if not all_results:
        return _empty_result(f"No hiring results found for '{company_name}'.")

    snippet_block, urls = _format_snippets(all_results)
    user_msg = (
        f"# Target company\n- Name: {company_name}\n"
        + (f"- Domain: {company_domain}\n" if company_domain else "")
        + (f"- Industry: {industry}\n" if industry else "")
        + (f"\n# Our ICP / service offering\n{icp_context}\n" if icp_context else "")
        + "\n# Hiring search snippets\n"
        + snippet_block
        + "\n\nClassify the hiring signal per the rules. Output JSON only."
    )

    try:
        result = await generate_json(
            model=settings.content_model,
            system=_SYSTEM_PROMPT,
            user=user_msg,
            schema=HiringSignalResult,
            max_tokens=700,
        )
    except Exception as exc:
        log.warning("hiring_llm_failed", extra={"company": company_name, "error": str(exc)})
        return _empty_result(f"LLM classification failed: {type(exc).__name__}")

    # Backfill source_urls from Tavily when the model didn't echo them.
    if not result.source_urls:
        result.source_urls = urls[:6]
    return result


# ---------------------------------------------------------------------------
# Top-level orchestration (persist + apply uplift)
# ---------------------------------------------------------------------------

async def enrich_hiring_signal(
    lead_id: int,
    *,
    workspace_id: int | None = None,
    force: bool = False,
) -> dict:
    """Research, store, and apply the hiring signal for one lead.

    Credit-saving gate: if the lead already has a hiring signal and `force`
    is False, this is a no-op (returns {"skipped": True}). Callers that want
    to limit research to C-tier leads filter the lead set themselves; this
    function only enforces the "don't re-research what we already have" rule.

    Returns a small status dict. Never raises.
    """
    from src.db import session_scope
    from src.models import Lead, Score

    if not force and has_hiring_signal(lead_id):
        log.info("hiring_skip_existing", extra={"lead_id": lead_id})
        return {"lead_id": lead_id, "skipped": True, "status": "skipped", "reason": "signal_exists"}

    # Snapshot the lead inside a session.
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return {"lead_id": lead_id, "error": "lead_not_found", "status": "failed"}
        company = lead.company
        domain = lead.company_domain
        industry = lead.industry
        ws_id = workspace_id if workspace_id is not None else lead.workspace_id

    # No company name or domain to search on → record a skipped run so the UI
    # shows the lead was considered (rather than silently doing nothing).
    if not build_hiring_queries(company, domain):
        upsert_hiring_signal(
            lead_id,
            _empty_result("No company name or domain to research."),
            workspace_id=ws_id,
            status="skipped",
        )
        log.info("hiring_skip_no_input", extra={"lead_id": lead_id})
        return {"lead_id": lead_id, "skipped": True, "status": "skipped",
                "reason": "no_company_or_domain"}

    icp_context = None
    try:
        icp = load_workspace_icp_config(ws_id)
        icp_context = (icp.company.one_liner or None)
    except Exception:  # pragma: no cover - icp is best-effort context
        icp_context = None

    # Research is wrapped so a genuine failure (exception) is recorded with
    # status="failed" + error, distinct from "ran fine, found nothing".
    try:
        result = await research_hiring_signal(
            company,
            company_domain=domain,
            industry=industry,
            icp_context=icp_context,
        )
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        record_hiring_failure(lead_id, msg, workspace_id=ws_id)
        log.warning("hiring_enrich_failed", extra={"lead_id": lead_id, "error": msg})
        return {"lead_id": lead_id, "skipped": False, "status": "failed", "error": msg}

    upsert_hiring_signal(lead_id, result, workspace_id=ws_id, status="completed")
    uplift = apply_hiring_uplift(lead_id, workspace_id=ws_id)

    log.info(
        "hiring_enrich_complete",
        extra={
            "lead_id": lead_id,
            "found": result.hiring_signal_found,
            "strength": result.hiring_signal_strength,
            "roles": len(result.relevant_roles_found),
            "uplift": (uplift or {}).get("recommendation") if uplift else None,
        },
    )
    return {
        "lead_id": lead_id,
        "skipped": False,
        "status": "completed",
        "found": result.hiring_signal_found,
        "strength": result.hiring_signal_strength,
        "roles": result.relevant_roles_found,
        "uplift": uplift,
    }
