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
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from tavily import TavilyClient

from src.config import settings
from src.llm import generate_json
from src.retry import retry_api

log = logging.getLogger(__name__)


class CandidateSignal(BaseModel):
    """One classified company signal surfaced by layered Tavily research.

    The LLM classifies each meaningful result; `select_strongest_signal`
    (deterministic, not the LLM) then picks the one used for scoring/email so
    the choice always favors RELEVANCE over recency.
    """
    signal_type: str = ""                 # hiring | funding | expansion | partnership | product | leadership | customer_win | other
    signal_summary: str = ""
    signal_relevance: Literal["none", "low", "medium", "high"] = "none"
    signal_recency_days: int | None = None  # est. age in days; None = unknown
    usable_for_scoring: bool = False
    usable_for_email: bool = False
    reason: str = ""
    source_url: str | None = None


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

    # Concrete evidence the company has a B2B sales motion (enterprise
    # pricing page, partner program, white-label product, employer
    # benefits, embedded-finance deals, B2B case studies, etc.). Empty
    # when no such evidence was found in research. Used by the scoring
    # module to disqualify B2C-only companies from OSP outreach.
    explicit_b2b_motion_evidence: list[str] = Field(default_factory=list)

    # v1 fields (legacy — kept for backward-compat with rows persisted
    # before the schema split).
    likely_buyer_accounts: list[str] = Field(default_factory=list)
    likely_buyer_segments: list[str] = Field(default_factory=list)
    buyer_account_confidence: Literal["low", "medium", "high"] = "low"
    buyer_account_rationale: str = ""
    flagged_competitors: list[str] = Field(default_factory=list)

    # v3 fields — five-rung buyer fallback ladder.
    # `direct_buyer_accounts`: named companies CONFIRMED in research as buyers
    #   (case studies, customer pages, integration docs). Max 2. Medium+ conf only.
    # `lookalike_buyer_accounts`: companies NOT confirmed but matching the buyer
    #   profile (same vertical, motion, scale). Max 2. Used when direct < 2.
    # `trigger_based_buyer_segments`: "<vertical> companies <doing X>" segments.
    #   Combine vertical + specific trigger event. Never broad team labels.
    # `buyer_fallback_mode`: computed deterministically after classification.
    direct_buyer_accounts: list[str] = Field(default_factory=list)
    lookalike_buyer_accounts: list[str] = Field(default_factory=list)
    trigger_based_buyer_segments: list[str] = Field(default_factory=list)
    buyer_fallback_mode: Literal[
        "direct_accounts", "direct_plus_lookalike", "lookalike_accounts",
        "trigger_segment", "needs_review",
    ] = "needs_review"
    buyer_research_status: Literal[
        "found_direct", "found_lookalike", "trigger_only", "needs_review",
    ] = "needs_review"
    buyer_research_confidence: Literal["low", "medium", "high"] = "low"
    buyer_research_rationale: str = ""

    # Research metadata (set by code, not the LLM) so the operator can verify
    # the Tavily date window that was actually used. See discover_buyer_accounts.
    news_window_days: int | None = None
    news_start_date: str | None = None   # YYYY-MM-DD
    news_end_date: str | None = None     # YYYY-MM-DD
    tavily_topic_used: str | None = None  # "news" | "general" | "news+general"
    result_count: int = 0
    source_urls: list[str] = Field(default_factory=list)

    # Layered research (LLM-classified candidate signals + the code-selected
    # strongest one) and Tavily call debug metadata (code-set).
    candidate_signals: list["CandidateSignal"] = Field(default_factory=list)
    selected_signal: "CandidateSignal | None" = None
    company_context_summary: str = ""
    recommended_outreach_angle: str = ""
    tavily_modes_used: list[str] = Field(default_factory=list)   # research|news|general|crawl|extract
    tavily_calls: list[dict] = Field(default_factory=list)        # per-call debug records

    # Tavily Research agent (Company Researcher) output + bookkeeping.
    # research_useful_signal_found / research_no_signal_reason are LLM-set
    # (it reads the research summary); the rest are code-set.
    research_used: bool = False
    research_status: str = "not_started"   # not_started | completed | skipped | failed
    research_summary: str = ""
    research_findings: list[str] = Field(default_factory=list)
    research_sources: list[str] = Field(default_factory=list)
    research_useful_signal_found: bool = False
    research_no_signal_reason: str = ""
    research_raw_payload: dict | None = None
    research_error: str | None = None
    research_last_run_at: str | None = None
    # Whether the Tavily research findings actually shaped the buyer-segmentation
    # fields below (LLM-set), and a one-line note on HOW. Distinct from
    # research_useful_signal_found (which is research-level): research can be
    # useful for context yet not change the buyer segments.
    company_research_signal_used: bool = False
    company_research_effect: str = ""


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

  CRITICAL for B2B / B2B2C: also populate `likely_buyer_accounts` with
  EXACTLY 2 named company picks (your best two from
  `likely_direct_buyers`). The downstream email layer plugs those two
  names verbatim into "Not sure if you're already working with A or B?"
  — so the pair must be plausible, non-competing buyer companies.
  If you cannot identify 2 strong candidates with confidence, leave
  `likely_buyer_accounts` EMPTY and the email layer will fall back to
  segments. Never fill with one strong + one weak; an empty list is
  better than a guess.

  - likely_direct_buyers
      Orgs/segments that BUY and PAY for the product. For B2B SaaS this
      is the customer list. For B2C this is usually EMPTY (consumers
      aren't worth naming individually).

  BUYER SEGMENT QUALITY — applies to every segment string you put in
  `likely_buyer_segments`, `likely_direct_buyers`, or
  `likely_partner_channels`. The email layer uses these strings
  verbatim in "Not sure if you're already working with ...?", so they
  must read like REAL MARKET CATEGORIES, not abstract AI phrases.

  BANNED segment phrasings — never use these:
    * "enterprise data teams"
    * "LLM pipeline builders"
    * "teams building AI workflows"
    * "companies using AI"
    * "AI products"
    * "AI agent platforms" (too broad — name the SPECIFIC vertical, like
      "AI search companies" or "AI agent builders for sales/recruiting")
    * Any "<adjective> teams" / "<adjective> builders" construction
      that doesn't name a concrete product category.

  REQUIRED — pick segments that name a concrete market category. Use
  the named-product-category style of these (these are reference
  examples — pick whatever fits the target's actual market):
    * "AI search companies"
    * "sales intelligence platforms"
    * "market research tools"
    * "data enrichment platforms"
    * "recruiting intelligence platforms"
    * "customer support platforms"
    * "vertical SaaS companies in <vertical>"
    * "AI agent builders for <function>"
    * "contact-center platforms"
    * "underwriting / lending platforms"
  Each segment should be the kind of label that would appear in a G2
  category page or a "competitors" SimilarWeb panel.

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

STEP 6 — Explicit B2B motion evidence (CRITICAL for B2C companies).
  - `explicit_b2b_motion_evidence`: list every concrete artifact in the
    research that proves the company SELLS to businesses, not just
    partners with them. Each entry is a short quote or fact:
      * "enterprise pricing page"
      * "partner program for banks"
      * "white-label product for credit unions"
      * "employer benefits offering on workplace.example.com"
      * "embedded-finance API documented at api.example.com"
      * "case study with [Business Customer Name]"
      * "channel sales team listed on careers page"
      * "B2B pricing tiers"
  - DO NOT list market-adjacent companies (mortgage lenders, banks,
    neobanks, credit unions) as B2B evidence just because they exist
    in the same market. Evidence MUST show the target sells TO them.
  - Empty list when no such evidence exists. For a pure-B2C product
    like a consumer credit-repair app with only individual billing,
    this list MUST be empty even if banks appear in the research.

STEP 7 — Buyer fallback ladder (CRITICAL for email quality).
Populate these fields so the email layer can pick the highest-quality case.

  DIRECT BUYER ACCOUNTS (for CASE 1 and CASE 2):
  `direct_buyer_accounts` — named companies CONFIRMED in research as actual
  buyers/users (appeared in case studies, customer pages, integration docs).
  MAX 2. Only populate when buyer_confidence is "medium" or "high".
  If fewer than 2 strong confirmed names exist, leave EMPTY — a lookalike or
  trigger segment is better than a weak guess.

  LOOKALIKE BUYER ACCOUNTS (for CASE 2 and CASE 3):
  `lookalike_buyer_accounts` — 2 named companies NOT confirmed in research
  but that MATCH the buyer profile: same vertical, same scale, same buy motion.
  These are "obvious next buyers" you'd bet on even without direct evidence.
  Rules for valid lookalikes:
    - Same buyer motion (e.g., both embed infra into enterprise support)
    - Same vertical or adjacent (e.g., both contact-center platforms)
    - Similar scale/stage
    - NOT a competitor of the target company
  Examples:
    - If Zendesk is the confirmed buyer: lookalikes = ServiceNow, Freshdesk
    - If Five9 is the confirmed buyer: lookalikes = NICE, Genesys
    - If Stripe is the confirmed buyer: lookalikes = Adyen, Braintree
  MAX 2. Omit if no plausible lookalikes exist — needs_review is better.

  TRIGGER-BASED SEGMENTS (for CASE 4):
  `trigger_based_buyer_segments` — 1-3 segments combining VERTICAL + TRIGGER.
  Format: "<vertical> companies <doing specific thing>"
  GOOD examples (use this style — named vertical + named trigger):
    * "Series A SaaS companies hiring SDRs"
    * "fintechs launching partner programs"
    * "cybersecurity companies expanding enterprise GTM"
    * "DevTools companies hiring enterprise sales leaders"
    * "AI companies launching customer support products"
    * "healthcare SaaS companies expanding implementation teams"
  BANNED — never use these in trigger segments or anywhere else:
    * "sales teams" / "founders" / "revenue teams" / "product teams"
    * "engineering teams" / "HR teams" / "marketing teams" / "business leaders"
    * "companies that need growth" / "teams that need pipeline"
    * "teams that need automation" / "teams in the industry"
    * "buyers in this space"
  Each segment MUST name a specific vertical AND a specific trigger event.
  "SaaS companies" alone is not valid. "companies hiring" alone is not valid.
  Always give both: "<vertical> companies <trigger>".

  FALLBACK MODE (set by the system — you must suggest, system will verify):
  Set `buyer_fallback_mode` to match the best case the data supports:
    "direct_accounts"      — 2 confirmed direct buyers, medium+ confidence
    "direct_plus_lookalike"— 1 confirmed + 1 lookalike, medium+ confidence
    "lookalike_accounts"   — 2 lookalikes (no direct), medium+ confidence
    "trigger_segment"      — trigger segments only (no named accounts)
    "needs_review"         — no usable buyer data found

  Set `buyer_research_rationale` to 1 sentence explaining what evidence was
  found and why you chose the fallback mode you did.

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
  5. NEVER use broad team labels as buyer segments. "Sales teams" or
     "founders" sound obvious and AI-written. Always pair a vertical with
     a trigger event.
  6. RANK BY RELEVANCE FIRST, THEN RECENCY. The company-signals snippets
     cover the last 90 days (or the configured window), not just the last
     few days. A relevant funding / hiring / expansion / partnership /
     product-launch signal from weeks ago BEATS an irrelevant article from
     the last few days. Do not prefer an article just because it is the most
     recent, and do not ignore a relevant signal just because it is not from
     this week.

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
  "flagged_competitors": ["..."],
  "explicit_b2b_motion_evidence": ["..."],
  "direct_buyer_accounts": ["named confirmed buyer 1", "named confirmed buyer 2"],
  "lookalike_buyer_accounts": ["lookalike 1", "lookalike 2"],
  "trigger_based_buyer_segments": ["<vertical> companies <doing X>"],
  "buyer_fallback_mode": "direct_accounts | direct_plus_lookalike | lookalike_accounts | trigger_segment | needs_review",
  "buyer_research_status": "found_direct | found_lookalike | trigger_only | needs_review",
  "buyer_research_confidence": "low | medium | high",
  "buyer_research_rationale": "<1 sentence>"
}

Worked examples:

A) Voice AI infrastructure (e.g. Ultravox.ai) — B2B with 2 named buyers
{
  "buyer_motion": "B2B",
  "likely_direct_buyers": ["Zendesk", "Five9", "support orgs deploying voice agents", "healthcare call centers"],
  "likely_partner_channels": [],
  "likely_referral_channels": [],
  "likely_end_users": [],
  "buyer_confidence": "medium",
  "partner_confidence": "low",
  "reasoning": "Voice AI infra is embedded by enterprise support / contact-center platforms; case studies surfaced Zendesk and Five9 as deployment partners.",
  "flagged_competitors": ["Bland AI", "Retell AI", "ElevenLabs"],
  "likely_buyer_accounts": ["Zendesk", "Five9"],
  "likely_buyer_segments": ["support orgs deploying voice agents", "healthcare call centers"],
  "buyer_account_confidence": "medium",
  "buyer_account_rationale": "Zendesk and Five9 surfaced as live integration partners in research; they fit Ultravox's embed motion.",
  "explicit_b2b_motion_evidence": []
}

B) Consumer credit app (e.g. Dovly) — pure B2C, NO B2B motion evidence found
{
  "buyer_motion": "B2C",
  "likely_direct_buyers": [],
  "likely_partner_channels": ["financial wellness platforms", "fintechs offering credit tools", "employers offering financial benefits"],
  "likely_referral_channels": ["mortgage lenders", "credit unions"],
  "likely_end_users": ["consumers repairing or building credit"],
  "buyer_confidence": "low",
  "partner_confidence": "medium",
  "reasoning": "Dovly's app is sold to individuals; banks and lenders are partner/referral channels, not direct buyers.",
  "explicit_b2b_motion_evidence": []
}

B-fallback) Web-scraping API for AI agents (e.g. Firecrawl) — only 1 strong named buyer surfaced, segments must be concrete market categories
{
  "buyer_motion": "B2B",
  "likely_direct_buyers": ["Stack AI", "sales intelligence platforms", "market research tools", "data enrichment platforms", "AI search companies", "AI agent builders for sales/recruiting"],
  "likely_partner_channels": [],
  "likely_referral_channels": [],
  "likely_end_users": [],
  "buyer_confidence": "low",
  "partner_confidence": "low",
  "reasoning": "Customer case studies surfaced Stack AI; couldn't confirm a second named account at high confidence, so fell back to concrete market categories that match Firecrawl's embedding ICP.",
  "flagged_competitors": ["Apify", "Bright Data", "ScrapingBee"],
  "likely_buyer_accounts": [],
  "likely_buyer_segments": ["AI search companies", "sales intelligence platforms", "market research tools", "data enrichment platforms", "AI agent builders for sales/recruiting"],
  "buyer_account_confidence": "low",
  "buyer_account_rationale": "Only 1 high-confidence named buyer (Stack AI); falling back to 5 concrete market categories so the email layer can use the segment template + 'teams like that' CTA.",
  "explicit_b2b_motion_evidence": []
}

C) Same company but research surfaced a real B2B motion
{
  "buyer_motion": "B2B2C",
  "likely_direct_buyers": ["employers offering financial wellness benefits"],
  "likely_partner_channels": ["financial wellness platforms"],
  "likely_referral_channels": [],
  "likely_end_users": ["consumers repairing or building credit"],
  "buyer_confidence": "medium",
  "partner_confidence": "medium",
  "reasoning": "Workplace.dovly.com lists 12 employer customers; clear B2B benefits motion alongside the consumer app.",
  "explicit_b2b_motion_evidence": ["employer benefits page lists Aon, Mercer, 10 others", "B2B pricing tier visible on benefits.dovly.com"]
}
"""


def _compute_buyer_fallback_mode(result: BuyerAccountResult) -> None:
    """Deterministically set buyer_fallback_mode and status from classified fields.

    Overrides whatever the LLM suggested so the mode is always consistent with
    the actual content of the other fields. Operates in-place on `result`.

    Ladder (highest quality first):
      1. direct_accounts    — ≥2 direct buyer accounts, medium+ confidence
      2. direct_plus_lookalike — 1 direct + ≥1 lookalike, medium+ confidence
      3. lookalike_accounts — ≥2 lookalikes, medium+ confidence
      4. trigger_segment    — ≥1 trigger segment (no named accounts met threshold)
      5. needs_review       — nothing usable
    """
    direct = [a.strip() for a in (result.direct_buyer_accounts or []) if a and a.strip()]
    lookalike = [a.strip() for a in (result.lookalike_buyer_accounts or []) if a and a.strip()]
    trigger = [s.strip() for s in (result.trigger_based_buyer_segments or []) if s and s.strip()]
    conf = (result.buyer_confidence or "low").strip().lower()
    eligible = conf in ("medium", "high")

    if len(direct) >= 2 and eligible:
        result.buyer_fallback_mode = "direct_accounts"
        result.buyer_research_status = "found_direct"
    elif len(direct) >= 1 and len(lookalike) >= 1 and eligible:
        result.buyer_fallback_mode = "direct_plus_lookalike"
        result.buyer_research_status = "found_direct"
    elif len(lookalike) >= 2 and eligible:
        result.buyer_fallback_mode = "lookalike_accounts"
        result.buyer_research_status = "found_lookalike"
    elif trigger:
        result.buyer_fallback_mode = "trigger_segment"
        result.buyer_research_status = "trigger_only"
    else:
        result.buyer_fallback_mode = "needs_review"
        result.buyer_research_status = "needs_review"

    result.buyer_research_confidence = conf  # type: ignore[assignment]


def _client() -> TavilyClient:
    return TavilyClient(api_key=settings.tavily_api_key)


def compute_news_window(news_window_days: int) -> tuple[str, str]:
    """Return (start_date, end_date) as YYYY-MM-DD for an N-day UTC window.

    end_date is today (UTC); start_date is today minus `news_window_days`.
    Pure + UTC-based so buyer research reads relevant signals from the last 90
    days by default rather than only the last few days.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, int(news_window_days)))
    return start.isoformat(), end.isoformat()


_RELEVANCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def select_strongest_signal(
    candidates: list["CandidateSignal"],
) -> "CandidateSignal | None":
    """Pick the strongest signal: RELEVANCE first, then recency.

    Pure + deterministic (not the LLM) so a relevant 60-day-old signal always
    beats an irrelevant 4-day-old one. Among equally-relevant signals, the more
    recent wins (smaller signal_recency_days; unknown age sorts last). Only
    signals usable for scoring with non-"none" relevance are eligible.
    """
    eligible = [
        c for c in (candidates or [])
        if _RELEVANCE_RANK.get((c.signal_relevance or "none"), 0) > 0
    ]
    if not eligible:
        return None

    def _key(c: "CandidateSignal"):
        relevance = _RELEVANCE_RANK.get((c.signal_relevance or "none"), 0)
        # recency tiebreaker: smaller days = fresher = better; unknown last.
        recency = c.signal_recency_days if c.signal_recency_days is not None else 10**9
        return (relevance, -recency)

    return max(eligible, key=_key)


def _crawl_sync(url: str, *, max_depth: int = 1, limit: int = 15) -> list[dict]:
    """Tavily Crawl (Company Researcher / Crawl2RAG style). Returns result list.

    Never raises here — callers wrap in try/except; an SDK without crawl or an
    API error degrades to no crawl results.
    """
    instructions = (
        "Find pages that explain what this company does, who they sell to, "
        "customer types, case studies, industries served, services/products, "
        "careers/jobs, press/news, leadership, partnerships, expansion, and "
        "proof of B2B buying motion."
    )
    resp = _client().crawl(
        url=url,
        max_depth=max_depth,
        limit=limit,
        extract_depth="basic",
        format="markdown",
        instructions=instructions,
    )
    return resp.get("results", []) if isinstance(resp, dict) else []


def _extract_sync(urls: list[str]) -> list[dict]:
    """Tavily Extract on the strongest URLs. Returns result list. Never raises here."""
    if not urls:
        return []
    resp = _client().extract(urls=urls, extract_depth="basic", format="markdown")
    return resp.get("results", []) if isinstance(resp, dict) else []


@retry_api
async def _tavily_crawl(url: str, *, max_depth: int = 1, limit: int = 15) -> list[dict]:
    if not (url or "").strip():
        return []
    return await asyncio.to_thread(_crawl_sync, url, max_depth=max_depth, limit=limit)


@retry_api
async def _tavily_extract(urls: list[str]) -> list[dict]:
    if not urls:
        return []
    return await asyncio.to_thread(_extract_sync, urls)


# ----- Tavily Research agent (Company Researcher) --------------------------

_RESEARCH_DONE = {"completed", "done", "success", "finished"}
_RESEARCH_FAIL = {"failed", "error", "cancelled", "canceled"}


def _research_sync(input_text: str, *, model: str = "mini", timeout: float = 60.0) -> dict:
    resp = _client().research(input=input_text, model=model, timeout=timeout)
    return resp if isinstance(resp, dict) else {}


def _get_research_sync(request_id: str) -> dict:
    resp = _client().get_research(request_id)
    return resp if isinstance(resp, dict) else {}


def _research_has_results(resp: dict) -> bool:
    return bool(
        resp.get("answer") or resp.get("summary") or resp.get("output")
        or resp.get("result") or resp.get("content")
    )


@retry_api
async def _tavily_research(
    input_text: str,
    *,
    model: str = "mini",
    max_polls: int = 6,
    poll_interval: float = 5.0,
) -> dict:
    """Create a Tavily Research task and poll get_research until done/failed.

    Bounded polling (default ≤30s) so the enrichment flow never hangs. Returns
    the latest response dict; the caller classifies status. Never raises.
    """
    resp = await asyncio.to_thread(_research_sync, input_text, model=model)
    status = (resp.get("status") or "").lower()
    if status in _RESEARCH_DONE or status in _RESEARCH_FAIL or _research_has_results(resp):
        return resp
    request_id = resp.get("request_id") or resp.get("id")
    if not request_id:
        return resp
    for _ in range(max_polls):
        await asyncio.sleep(poll_interval)
        polled = await asyncio.to_thread(_get_research_sync, request_id)
        pstatus = (polled.get("status") or "").lower()
        if pstatus in _RESEARCH_DONE or pstatus in _RESEARCH_FAIL or _research_has_results(polled):
            return polled
    return resp  # timed out — caller marks accordingly


def parse_research_response(resp: dict) -> tuple[str, list[str], list[str]]:
    """Return (summary, findings, source_urls) from a Tavily research response.

    Defensive across response shapes — probes common keys; never raises.
    """
    resp = resp or {}
    summary = (
        resp.get("answer") or resp.get("summary") or resp.get("output")
        or resp.get("result") or resp.get("content") or ""
    )
    if isinstance(summary, dict):
        summary = summary.get("text") or summary.get("answer") or str(summary)
    summary = str(summary).strip()

    findings: list[str] = []
    raw_findings = resp.get("findings") or resp.get("key_findings") or resp.get("highlights")
    if isinstance(raw_findings, list):
        findings = [str(f).strip() for f in raw_findings if str(f).strip()]

    sources: list[str] = []
    for key in ("sources", "results", "citations", "references"):
        items = resp.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    u = (it.get("url") or it.get("link") or "").strip()
                elif isinstance(it, str):
                    u = it.strip()
                else:
                    u = ""
                if u and u not in sources:
                    sources.append(u)
    return summary, findings, sources


def _result_dates(results: list[dict]) -> tuple[str | None, str | None]:
    """Return (oldest, newest) published_date strings present in results, if any."""
    dates = sorted(
        d for d in (
            (r.get("published_date") or r.get("publishedAt") or "").strip()
            for r in results
        ) if d
    )
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _tavily_search_sync(
    query: str,
    max_results: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    topic: str | None = None,
    search_depth: str = "basic",
) -> list[dict]:
    # Build kwargs so date-windowed news searches pass start_date/end_date
    # (NEVER time_range=day/week — those over-restrict to the last few days).
    kwargs: dict = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
    }
    if topic:
        kwargs["topic"] = topic
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    return _client().search(**kwargs).get("results", [])


@retry_api
async def _tavily_search(
    query: str,
    max_results: int = 4,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    topic: str | None = None,
    search_depth: str = "basic",
) -> list[dict]:
    if not query.strip():
        return []
    return await asyncio.to_thread(
        _tavily_search_sync, query, max_results,
        start_date=start_date, end_date=end_date, topic=topic, search_depth=search_depth,
    )


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


def _crawl_url(company_website: str | None, company_domain: str | None) -> str | None:
    """Best https URL to crawl from a website or bare domain."""
    raw = (company_website or company_domain or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    return f"https://{raw}"


def _format_doc_snippets(label: str, results: list[dict], *, limit: int = 6) -> str:
    """Format crawl/extract results (url + raw_content/content) into bullets."""
    if not results:
        return f"## {label}\n(no results)"
    bullets = [f"## {label}"]
    for r in results[:limit]:
        url = (r.get("url") or "").strip()
        content = (r.get("raw_content") or r.get("content") or r.get("snippet") or "").strip()
        if content:
            content = content[:400]
        bullets.append(f"- {url}: {content}")
    return "\n".join(bullets)


async def discover_buyer_accounts(
    company_name: str,
    *,
    company_description: str | None = None,
    industry: str | None = None,
    company_website: str | None = None,
    company_domain: str | None = None,
    icp_context: str | None = None,
    news_window_days: int = 90,
    use_crawl: bool = True,
    use_extract: bool = True,
    use_research: bool = True,
) -> BuyerAccountResult:
    """Identify likely buyer accounts + company signals for `company_name`.

    Layered Tavily research (Company Researcher / Crawl2RAG style):
      A. Search topic="news" (windowed) — press/announcements in last N days.
      B. Search topic="general" (windowed signal query) — broader signal pages.
         Plus un-windowed buyer-evidence queries (customers/case studies/sells).
      C. Crawl the company website (when a domain exists + use_crawl).
      D. Extract the strongest URLs (when use_extract).

    Every Tavily call is recorded in `result.tavily_calls` (query/topic/dates/
    counts/urls) so the operator can verify the date window. Never raises.

    `news_window_days` (default 90) bounds the date-sensitive signal searches
    via start_date/end_date — NEVER time_range=day/week — so relevant
    funding/hiring/expansion signals from weeks ago are found, not just the
    last few days. Buyer-evidence queries stay un-windowed (an old case study
    is still valid evidence).
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

    start_date, end_date = compute_news_window(news_window_days)

    tavily_calls: list[dict] = []
    modes_used: list[str] = []

    def _record(mode, *, query=None, topic=None, max_results=None,
                sd=None, ed=None, results=None):
        results = results or []
        oldest, newest = _result_dates(results)
        urls = [(r.get("url") or "").strip() for r in results if (r.get("url") or "").strip()]
        tavily_calls.append({
            "mode": mode,
            "query": query,
            "topic": topic,
            "search_depth": "basic",
            "max_results": max_results,
            "start_date": sd,
            "end_date": ed,
            "time_range": None,
            "result_count": len(results),
            "oldest_result_date": oldest,
            "newest_result_date": newest,
            "source_urls": urls,
        })
        if mode not in modes_used:
            modes_used.append(mode)
        return results

    # ----- Buyer-evidence queries (general topic, NOT date-bounded) -----
    evidence_queries = [
        f'"{company_name}" customers',
        f'"{company_name}" case studies',
        f'what does "{company_name}" sell',
    ]
    try:
        evidence_lists = await asyncio.gather(
            *[_tavily_search(q, max_results=4, topic="general") for q in evidence_queries],
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
    evidence_lists = [[] if isinstance(x, Exception) else x for x in evidence_lists]
    for q, lst in zip(evidence_queries, evidence_lists):
        _record("general", query=q, topic="general", max_results=4, results=lst)

    # ----- A. News search (windowed) + B. general signal search (windowed) -----
    signal_query = (
        f'"{company_name}" most relevant company signals from the last '
        f'{int(news_window_days)} days including hiring, expansion, funding, '
        f'partnerships, product launches, leadership changes, customer wins, '
        f'new case studies, new services, new locations, and operational changes'
    )
    news_results: list[dict] = []
    general_results: list[dict] = []
    try:
        news_results = await _tavily_search(
            signal_query, max_results=10,
            start_date=start_date, end_date=end_date, topic="news",
        )
        _record("news", query=signal_query, topic="news", max_results=10,
                sd=start_date, ed=end_date, results=news_results)
    except Exception as exc:
        log.warning("buyer_discovery_news_search_failed",
                    extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"})
    try:
        # General windowed signal search ALWAYS runs (it catches press/blog/
        # careers pages that topic="news" misses) — also the fallback path.
        general_results = await _tavily_search(
            signal_query, max_results=10,
            start_date=start_date, end_date=end_date, topic="general",
        )
        _record("general", query=signal_query, topic="general", max_results=10,
                sd=start_date, ed=end_date, results=general_results)
    except Exception as exc:
        log.warning("buyer_discovery_general_search_failed",
                    extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"})

    if news_results and general_results:
        topic_used = "news+general"
    elif news_results:
        topic_used = "news"
    elif general_results:
        topic_used = "general"
    else:
        topic_used = None

    # ----- C. Crawl the company website (Company Researcher / Crawl2RAG) -----
    crawl_results: list[dict] = []
    crawl_target = _crawl_url(company_website, company_domain)
    if use_crawl and crawl_target:
        try:
            crawl_results = await _tavily_crawl(crawl_target, max_depth=1, limit=15)
        except Exception as exc:
            log.warning("buyer_discovery_crawl_failed",
                        extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"})
        _record("crawl", query=crawl_target, topic="crawl",
                max_results=15, results=crawl_results)

    # ----- D. Extract strongest URLs (relevance-ranked already by Tavily) -----
    extract_results: list[dict] = []
    if use_extract:
        ranked_urls: list[str] = []
        for lst in (news_results, general_results, crawl_results, *evidence_lists):
            for r in lst:
                u = (r.get("url") or "").strip()
                if u and u not in ranked_urls:
                    ranked_urls.append(u)
        top_urls = ranked_urls[:5]
        if top_urls:
            try:
                extract_results = await _tavily_extract(top_urls)
            except Exception as exc:
                log.warning("buyer_discovery_extract_failed",
                            extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"})
            _record("extract", query=", ".join(top_urls), topic="extract",
                    max_results=len(top_urls), results=extract_results)

    # ----- Tavily Research agent (Company Researcher) -----
    research_used = False
    research_status = "skipped"
    research_summary = ""
    research_findings: list[str] = []
    research_sources: list[str] = []
    research_raw: dict | None = None
    research_error: str | None = None
    research_last_run_at: str | None = None
    if use_research and (company_name or company_domain):
        research_used = True
        research_last_run_at = datetime.now(timezone.utc).isoformat()
        research_input = (
            f'Research the company "{company_name}" for B2B outbound sales '
            f"qualification. "
            + (f"Company domain: {company_domain}. " if company_domain else "")
            + (f"Company website: {company_website}. " if company_website else "")
            + (f"Industry: {industry}. " if industry else "")
            + (f"Our offering / ICP context: {icp_context}. " if icp_context else "")
            + "Identify what the company does, who they sell to, customer "
            "segments, relevant recent business signals from the last "
            f"{int(news_window_days)} days ({start_date} to {end_date}), including "
            "hiring, expansion, funding, partnership, product, and customer "
            "signals; ICP fit, buyer relevance, and the strongest outreach "
            "angle. Prefer relevance over recency. If no useful signal is "
            "found, say so clearly."
        )
        try:
            research_raw = await _tavily_research(research_input, model="mini")
            rstatus = (research_raw.get("status") or "").lower()
            research_summary, research_findings, research_sources = parse_research_response(research_raw)
            if research_summary or research_findings:
                research_status = "completed"
            elif rstatus in _RESEARCH_FAIL:
                research_status = "failed"
                research_error = research_raw.get("error") or f"research status={rstatus or 'unknown'}"
            else:
                research_status = "completed"  # ran; usefulness decided by LLM/code below
        except Exception as exc:
            research_status = "failed"
            research_error = f"{type(exc).__name__}: {exc}"
        _record("research", query=research_input, topic="research",
                sd=start_date, ed=end_date,
                results=[{"url": u} for u in research_sources])
        tavily_calls[-1]["status"] = research_status
        tavily_calls[-1]["error"] = research_error
        tavily_calls[-1]["window_days"] = int(news_window_days)
        tavily_calls[-1]["source_count"] = len(research_sources)

    # ----- Assemble snippet blocks + aggregate metadata -----
    source_urls: list[str] = []
    result_count = 0
    snippet_blocks: list[str] = []
    if research_summary:
        snippet_blocks.append(
            "## Tavily company research (agent summary)\n" + research_summary[:1500]
        )
        source_urls.extend(research_sources)
    for label, lst in (
        ("Customers", evidence_lists[0]),
        ("Case studies", evidence_lists[1]),
        ("What they sell", evidence_lists[2]),
        (f"Company signals — news (last {int(news_window_days)}d)", news_results),
        (f"Company signals — general (last {int(news_window_days)}d)", general_results),
    ):
        if lst:
            result_count += len(lst)
            source_urls.extend((r.get("url") or "").strip() for r in lst if (r.get("url") or "").strip())
        snippet_blocks.append(_format_snippets(label, lst))
    if crawl_results:
        result_count += len(crawl_results)
        source_urls.extend((r.get("url") or "").strip() for r in crawl_results if (r.get("url") or "").strip())
        snippet_blocks.append(_format_doc_snippets("Company website (crawl)", crawl_results))
    if extract_results:
        result_count += len(extract_results)
        snippet_blocks.append(_format_doc_snippets("Extracted top sources", extract_results))

    any_results = result_count > 0 or bool(research_summary)

    def _apply_research_meta(res: BuyerAccountResult) -> None:
        """Stamp Tavily Research bookkeeping onto a result (code-set fields).

        research_useful_signal_found / research_no_signal_reason are LLM-set
        when a summary was available; otherwise forced False here so scoring /
        content never invent a signal.
        """
        res.research_used = research_used
        res.research_status = research_status if research_used else "skipped"
        res.research_summary = research_summary
        res.research_findings = research_findings
        res.research_sources = research_sources
        res.research_raw_payload = research_raw
        res.research_error = research_error
        res.research_last_run_at = research_last_run_at
        if not research_used:
            res.research_useful_signal_found = False
            res.research_no_signal_reason = "Tavily Research disabled for this workspace."
        elif research_status == "failed":
            res.research_useful_signal_found = False
            res.research_no_signal_reason = res.research_error or "Tavily Research failed."
        elif not research_summary:
            res.research_useful_signal_found = False
            res.research_no_signal_reason = (
                res.research_no_signal_reason
                or "Tavily Research ran but returned no usable company signal."
            )
        # else: keep the LLM's research_useful_signal_found / no_signal_reason.

        # company_research_signal_used can only be true if research was actually
        # useful — keep the two consistent so the UI never claims research shaped
        # the segments when it didn't.
        if not res.research_useful_signal_found:
            res.company_research_signal_used = False
            if not res.company_research_effect:
                res.company_research_effect = (
                    "Tavily research did not produce a useful buyer-segmentation signal."
                )

    if not any_results:
        empty = BuyerAccountResult(
            buyer_account_rationale=(
                f"Tavily returned no results for '{company_name}' — "
                "falling back to segments only."
            )
        )
        empty.news_window_days = int(news_window_days)
        empty.news_start_date = start_date
        empty.news_end_date = end_date
        empty.tavily_topic_used = topic_used
        empty.result_count = 0
        empty.tavily_calls = tavily_calls
        empty.tavily_modes_used = modes_used
        _apply_research_meta(empty)
        return empty

    user_msg = (
        f"# Target company\n- Name: {company_name}\n"
        + (f"- Description: {company_description}\n" if company_description else "")
        + (f"- Industry: {industry}\n" if industry else "")
        + f"\n# Company-signals search window: {start_date} to {end_date} "
        + f"(last {int(news_window_days)} days)\n"
        + "\n# Research snippets\n"
        + "\n\n".join(snippet_blocks)
        + "\n\n# Classify the company signals.\n"
        + "For each meaningful signal, add a candidate_signals entry with: "
        + "signal_type, signal_summary, signal_relevance (none/low/medium/high), "
        + "signal_recency_days (estimated age in days, null if unknown), "
        + "usable_for_scoring, usable_for_email, reason, source_url.\n"
        + "RANK BY RELEVANCE FIRST, THEN RECENCY: a relevant hiring/funding/"
        + "expansion/partnership signal from 45-90 days ago BEATS an irrelevant "
        + "article from the last few days. Do not prefer an article just because "
        + "it is newer; do not penalize the company for no news in the last few "
        + "days. Also set company_context_summary and recommended_outreach_angle.\n"
        + (
            "A '## Tavily company research (agent summary)' block may be present. "
            "If it contains a genuinely useful, relevant company signal, set "
            "research_useful_signal_found=true and leave research_no_signal_reason "
            "empty. If it is empty, generic, or has no useful signal, set "
            "research_useful_signal_found=false and put a one-line "
            "research_no_signal_reason. Never invent a signal that is not in the "
            "research text.\n"
            "USE the research to IMPROVE buyer segmentation, not just scoring: "
            "ground likely_direct_buyers, likely_buyer_segments, "
            "likely_partner_channels, trigger_based_buyer_segments, "
            "lookalike_buyer_accounts and explicit_b2b_motion_evidence in what "
            "the research actually says. Make segments SPECIFIC and grounded "
            "(e.g. 'CPG brands sourcing private-label manufacturing', 'regional "
            "grocery retailers expanding store-brand portfolios') — never vague "
            "('businesses looking to grow'). When the research shaped these "
            "fields, set company_research_signal_used=true and put a one-line "
            "company_research_effect describing HOW it changed the segments; "
            "otherwise set company_research_signal_used=false. Mention in "
            "buyer_research_rationale whether company research was used and how. "
            "Keep buyer_research_rationale consistent with the research signal. "
            "Do NOT add a company to direct_buyer_accounts unless the research "
            "CONFIRMS it as a real customer; never list a competitor as a buyer.\n"
            if research_summary else
            "No Tavily research summary is present; set "
            "research_useful_signal_found=false and company_research_signal_used=false.\n"
        )
        + "Then classify buyers per the rules. Output JSON only."
    )

    try:
        result = await generate_json(
            model=settings.content_model,
            system=_SYSTEM_PROMPT,
            user=user_msg,
            schema=BuyerAccountResult,
            max_tokens=1200,
        )
    except Exception as exc:
        log.warning(
            "buyer_discovery_llm_failed",
            extra={"company": company_name, "error": f"{type(exc).__name__}: {exc}"},
        )
        fail = BuyerAccountResult(
            buyer_account_rationale=f"LLM classification failed: {type(exc).__name__}"
        )
        fail.news_window_days = int(news_window_days)
        fail.news_start_date = start_date
        fail.news_end_date = end_date
        fail.tavily_calls = tavily_calls
        fail.tavily_modes_used = modes_used
        _apply_research_meta(fail)
        return fail

    # Override buyer_fallback_mode deterministically — don't trust LLM self-assessment.
    _compute_buyer_fallback_mode(result)

    # Pick the strongest signal deterministically (relevance first, then recency).
    result.selected_signal = select_strongest_signal(result.candidate_signals)

    # Stamp research metadata (code-set, not LLM) so the operator can verify
    # the date window + Tavily calls actually used.
    result.news_window_days = int(news_window_days)
    result.news_start_date = start_date
    result.news_end_date = end_date
    result.tavily_topic_used = topic_used
    result.result_count = result_count
    result.source_urls = list(dict.fromkeys(source_urls))
    result.tavily_calls = tavily_calls
    result.tavily_modes_used = modes_used
    _apply_research_meta(result)

    log.info(
        "buyer_discovery_complete",
        extra={
            "company": company_name,
            "fallback_mode": result.buyer_fallback_mode,
            "modes_used": modes_used,
            "result_count": result_count,
            "selected_signal": (result.selected_signal.signal_type if result.selected_signal else None),
            "news_window_days": int(news_window_days),
        },
    )
    return result
