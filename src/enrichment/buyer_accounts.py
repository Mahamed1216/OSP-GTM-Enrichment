"""Buyer-account discovery — who would plausibly BUY the lead's product.

The pipeline:
  1. Tavily search: "{company} customers", "{company} case studies",
     "what does {company} sell". Three short queries; results are
     concatenated into one snippet block for the LLM.
  2. Claude classifies the result into structured buyers vs competitors,
     using rules baked into the system prompt (no competitors as buyers,
     buyer SEGMENTS when accounts aren't obvious, etc).

Returned `BuyerAccountResult` ships into the Enrichment row and is
surfaced in the email-generation prompt so the model can pick named
companies or buyer segments based on confidence.

Failure modes:
  - Tavily empty / errors → empty result with confidence="low" and
    rationale explaining why.
  - LLM errors → empty result, rationale stamps the exception.

The function NEVER raises into the waterfall — enrich_lead() classifies
the source as "no_results" or "error" via duration/payload heuristics.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, Field
from tavily import TavilyClient

from src.config import settings
from src.llm import generate_json
from src.retry import retry_api

log = logging.getLogger(__name__)


class BuyerAccountResult(BaseModel):
    likely_buyer_accounts: list[str] = Field(default_factory=list)
    likely_buyer_segments: list[str] = Field(default_factory=list)
    buyer_account_confidence: Literal["low", "medium", "high"] = "low"
    buyer_account_rationale: str = ""
    flagged_competitors: list[str] = Field(default_factory=list)


_SYSTEM_PROMPT = """\
You classify companies into BUYERS (plausible customers) vs COMPETITORS
for a target company. Output JSON ONLY, conforming to the schema below.

Rules:
1. A BUYER is a company that would plausibly purchase or embed the target
   company's product. Examples: for a voice-AI infrastructure company,
   buyers are large support / healthcare / fintech orgs that deploy voice
   agents — NOT other voice-AI platforms.
2. A COMPETITOR is any company offering the same core product category.
   Never list a competitor as a buyer.
3. For infrastructure / API / platform companies, buyers are the
   companies that would EMBED the infrastructure, not other infra vendors.
4. For vertical SaaS, buyers are companies operating IN that vertical
   (e.g., healthcare SaaS → hospital systems, payer networks).
5. Prefer 2-4 named buyer accounts when you can find them in the
   research snippet. If you cannot, return an empty `likely_buyer_accounts`
   and populate `likely_buyer_segments` (2-4 short segment labels) instead.
6. Confidence:
   - "high" — 2+ named buyers AND they are clearly NOT competitors
     (case study, customer list, partnership reference).
   - "medium" — 2+ named buyers but uncertain whether they are buyers or
     competitors, OR 1 named buyer.
   - "low" — no named buyers; fall back to segments.
7. Any company you suspect is a COMPETITOR of the target, list under
   `flagged_competitors` so downstream code can warn if it leaks into
   email copy.
8. `buyer_account_rationale` (1-2 sentences): WHY these are buyers, or
   why you fell back to segments.

Output schema:
{
  "likely_buyer_accounts": ["Company A", "Company B"],
  "likely_buyer_segments": ["segment 1", "segment 2"],
  "buyer_account_confidence": "low" | "medium" | "high",
  "buyer_account_rationale": "<1-2 sentences>",
  "flagged_competitors": ["Competitor X"]
}
"""


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


def _format_snippets(label: str, results: list[dict]) -> str:
    """Compact Tavily results into a few labeled bullets the LLM can read."""
    if not results:
        return f"## {label}\n(no results)"
    bullets: list[str] = [f"## {label}"]
    for r in results[:4]:
        title = (r.get("title") or "").strip()
        snippet = (r.get("content") or r.get("snippet") or "").strip()
        if snippet:
            snippet = snippet[:300]
        bullets.append(f"- {title}: {snippet}")
    return "\n".join(bullets)


async def discover_buyer_accounts(
    company_name: str,
    *,
    company_description: str | None = None,
    industry: str | None = None,
) -> BuyerAccountResult:
    """Identify likely buyer accounts for `company_name`.

    Returns a BuyerAccountResult; never raises. On any failure (no
    Tavily key, no LLM key, empty results, schema mismatch) returns a
    low-confidence result with a rationale explaining why so the
    enrichment status surfaces "no_results" rather than masking the
    issue as success.
    """
    if not company_name or not company_name.strip():
        return BuyerAccountResult(
            buyer_account_rationale="No company name provided to research."
        )
    if not settings.tavily_api_key:
        return BuyerAccountResult(
            buyer_account_rationale="Tavily not configured — buyer discovery skipped."
        )
    if not settings.anthropic_api_key:
        return BuyerAccountResult(
            buyer_account_rationale="Anthropic not configured — buyer discovery skipped."
        )

    # Three short queries. Customers + case studies are the two highest
    # signal-to-noise searches; the "what does X sell" query gives the
    # LLM context to tell buyers from competitors.
    queries = [
        f'"{company_name}" customers',
        f'"{company_name}" case studies',
        f'what does "{company_name}" sell',
    ]
    try:
        result_lists = await asyncio.gather(
            *[_tavily_search(q, max_results=4) for q in queries],
            return_exceptions=True,
        )
    except Exception as exc:
        log.warning(
            "buyer_discovery_tavily_failed",
            extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"},
        )
        return BuyerAccountResult(
            buyer_account_rationale=f"Tavily search failed: {type(exc).__name__}"
        )

    snippet_blocks: list[str] = []
    any_results = False
    for label, lst in zip(("Customers", "Case studies", "What they sell"), result_lists):
        if isinstance(lst, Exception):
            continue
        if lst:
            any_results = True
        snippet_blocks.append(_format_snippets(label, lst))

    if not any_results:
        return BuyerAccountResult(
            buyer_account_rationale=(
                f"Tavily returned no results for '{company_name}' — "
                "falling back to segments only."
            )
        )

    user_msg = (
        f"# Target company\n- Name: {company_name}\n"
        + (f"- Description: {company_description}\n" if company_description else "")
        + (f"- Industry: {industry}\n" if industry else "")
        + "\n# Research snippets\n"
        + "\n\n".join(snippet_blocks)
        + "\n\nClassify per the rules. Output JSON only."
    )

    try:
        result = await generate_json(
            model=settings.content_model,
            system=_SYSTEM_PROMPT,
            user=user_msg,
            schema=BuyerAccountResult,
            max_tokens=800,
        )
    except Exception as exc:
        log.warning(
            "buyer_discovery_llm_failed",
            extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"},
        )
        return BuyerAccountResult(
            buyer_account_rationale=(
                f"LLM classification failed: {type(exc).__name__}"
            )
        )

    log.info(
        "buyer_discovery_complete",
        extra={
            "company": company_name,
            "accounts": result.likely_buyer_accounts,
            "segments": result.likely_buyer_segments,
            "confidence": result.buyer_account_confidence,
            "flagged_competitors_count": len(result.flagged_competitors),
        },
    )
    return result
