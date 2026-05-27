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
    """Structured buyer-account discovery result.

    Schema evolution: v1 only had `likely_buyer_accounts` + segments. v2
    adds `buyer_motion` plus split partner/referral/end-user channels so
    B2C companies (e.g. Dovly — sells to consumers, partners with banks)
    don't get their distribution channels mislabeled as direct buyers.

    Field map:
      - `buyer_motion` — what the company sells, who pays the invoice.
        Drives the email prompt's CTA wording (buyer vs partner framing).
      - `likely_direct_buyers` — companies/segments that buy the product.
      - `likely_partner_channels` — orgs that partner / co-sell / embed
        (NOT direct buyers).
      - `likely_referral_channels` — orgs that refer leads but don't
        buy or embed.
      - `likely_end_users` — B2C consumer segments when buyer_motion=B2C.
      - `likely_buyer_accounts` / `likely_buyer_segments` — legacy fields
        kept so old code paths keep working. New code reads the split
        fields above.
    """
    # v2 fields (preferred)
    buyer_motion: Literal[
        "B2B", "B2C", "B2B2C", "marketplace", "partner_led", "unknown"
    ] = "unknown"
    likely_direct_buyers: list[str] = Field(default_factory=list)
    likely_partner_channels: list[str] = Field(default_factory=list)
    likely_referral_channels: list[str] = Field(default_factory=list)
    likely_end_users: list[str] = Field(default_factory=list)
    buyer_confidence: Literal["low", "medium", "high"] = "low"
    partner_confidence: Literal["low", "medium", "high"] = "low"
    reasoning: str = ""

    # v1 fields (legacy — kept for backward-compat with rows persisted
    # before the schema split).
    likely_buyer_accounts: list[str] = Field(default_factory=list)
    likely_buyer_segments: list[str] = Field(default_factory=list)
    buyer_account_confidence: Literal["low", "medium", "high"] = "low"
    buyer_account_rationale: str = ""
    flagged_competitors: list[str] = Field(default_factory=list)


_SYSTEM_PROMPT = """\
You classify a target company's commercial relationships so downstream
email copy uses the right framing (buyers vs partners vs end users).
Output JSON ONLY, conforming to the schema below.

STEP 1 — Decide buyer_motion. Who actually writes the check?
  - "B2B"        — sells to businesses; businesses pay.
  - "B2C"        — sells to consumers; consumers pay. (Dovly = B2C: a
                   consumer credit app billed to individuals.)
  - "B2B2C"      — sells through businesses to their consumers (e.g.
                   employer benefit platforms, white-label fintech).
  - "marketplace" — two-sided; both sides matter.
  - "partner_led" — primary revenue routes through partners/resellers.
  - "unknown"     — research is too thin to call.

STEP 2 — Populate the four channel buckets. Each item is either a
NAMED company or a short SEGMENT label. Never list a competitor.

  - likely_direct_buyers
      Orgs/segments that BUY and PAY for the product. For B2B SaaS this
      is the customer list. For B2C this is usually EMPTY (consumers
      aren't worth naming individually).

  - likely_partner_channels
      Orgs that EMBED, WHITE-LABEL, CO-SELL, or DISTRIBUTE the product.
      Not buyers. Critical for B2C and B2B2C: banks/fintechs that embed
      a consumer credit app are partner channels, NOT direct buyers.

  - likely_referral_channels
      Orgs that send leads but don't buy/embed. E.g., mortgage lenders
      referring users to a credit-repair app.

  - likely_end_users
      For B2C / B2B2C only. Segment labels for the consumers who use
      the product. Empty for pure B2B.

STEP 3 — Confidence calls.
  - buyer_confidence: "high" if you found ≥2 confirmed direct buyers in
    research; "medium" if 1 or evidence is indirect; "low" otherwise.
  - partner_confidence: same rubric for likely_partner_channels.

STEP 4 — Competitors.
  - `flagged_competitors`: any org in the same core product category
    as the target. Never list these elsewhere.

STEP 5 — Reasoning.
  - `reasoning` (1-2 sentences): WHY this motion + WHY these channels
    were chosen. Name the evidence ("customer page lists Acme, Beta",
    "no enterprise pricing — consumer billing").

Critical rules:
  1. NEVER conflate partners with direct buyers. If the product is
     sold to consumers but BANKS or LENDERS appear in research, that's
     almost always a partner channel — not a buyer. List them under
     likely_partner_channels.
  2. For B2C products, likely_direct_buyers should usually be empty.
     The email layer will switch CTA to "channels like that" when it
     sees that pattern.
  3. For B2B infra / API / platform: buyers are the orgs EMBEDDING the
     product; other infra vendors are competitors.
  4. Backward-compat: also populate the legacy fields
     `likely_buyer_accounts`, `likely_buyer_segments`,
     `buyer_account_confidence`, `buyer_account_rationale` so older code
     paths keep working. For B2C, set those to the partner-channel
     equivalents (segments → segments, confidence = partner_confidence).

Output schema (JSON, no prose):
{
  "buyer_motion": "B2B | B2C | B2B2C | marketplace | partner_led | unknown",
  "likely_direct_buyers": ["..."],
  "likely_partner_channels": ["..."],
  "likely_referral_channels": ["..."],
  "likely_end_users": ["..."],
  "buyer_confidence": "low | medium | high",
  "partner_confidence": "low | medium | high",
  "reasoning": "<1-2 sentences naming the evidence>",
  "likely_buyer_accounts": ["..."],
  "likely_buyer_segments": ["..."],
  "buyer_account_confidence": "low | medium | high",
  "buyer_account_rationale": "<1-2 sentences>",
  "flagged_competitors": ["..."]
}

Worked examples:

A) Voice AI infrastructure (e.g. Ultravox.ai)
{
  "buyer_motion": "B2B",
  "likely_direct_buyers": ["support orgs deploying voice agents", "healthcare call centers", "fintech ops teams"],
  "likely_partner_channels": [],
  "likely_referral_channels": [],
  "likely_end_users": [],
  "buyer_confidence": "medium",
  "partner_confidence": "low",
  "reasoning": "Voice AI infra is embedded by enterprise support and contact-center teams; no consumer-facing motion.",
  "flagged_competitors": ["Bland AI", "Retell AI", "ElevenLabs"]
}

B) Consumer credit app (e.g. Dovly)
{
  "buyer_motion": "B2C",
  "likely_direct_buyers": [],
  "likely_partner_channels": ["financial wellness platforms", "fintechs offering credit tools", "employers offering financial benefits"],
  "likely_referral_channels": ["mortgage lenders", "credit unions"],
  "likely_end_users": ["consumers repairing or building credit"],
  "buyer_confidence": "low",
  "partner_confidence": "medium",
  "reasoning": "Dovly's app is sold to individuals; banks and lenders are partner/referral channels, not direct buyers."
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
