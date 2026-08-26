"""Website analysis for settings autofill.

Fetches and analyzes a company website to generate suggested ICP settings.
Never auto-saves — returns an AnalysisResult for user review only.

Entry points:
  analyze_website(url, notes)            — synchronous wrapper
  analyze_website_async(url, notes)      — async version
  apply_suggestions_to_config(cfg, s, checked_fields) — apply reviewed fields

Internal helpers exposed for testing:
  _raw_llm_call(system, user, max_tokens)  — raw Anthropic call → str
  _parse_llm_json(raw)                     — strip fences + parse + clamp → _LLMOutput
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError

from src.config import settings

log = logging.getLogger(__name__)

_FETCH_TIMEOUT = 10.0
_MAX_CONTENT_CHARS = 8000
_USER_AGENT = "Mozilla/5.0 (compatible; research-bot/1.0)"


class WebsiteSuggestions(BaseModel):
    company_name: str = ""
    one_liner: str = ""
    value_props: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    target_company_sizes: list[str] = Field(default_factory=list)
    target_company_stages: list[str] = Field(default_factory=list)
    target_tech_stack_signals: list[str] = Field(default_factory=list)
    target_geographies: list[str] = Field(default_factory=list)
    target_titles: list[str] = Field(default_factory=list)
    seniority_levels: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    top_pain_points: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    news_search_terms: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    suggestions: WebsiteSuggestions = Field(default_factory=WebsiteSuggestions)
    sources_used: list[str] = Field(default_factory=list)
    confidence: str = "low"
    reasoning: dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None
    debug_info: Optional[str] = None  # technical details for debug expander


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "head", "noscript", "template", "svg", "path"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self.description = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            ad = dict(attrs)
            name = (ad.get("name") or "").lower()
            if name == "description" and not self.description:
                self.description = (ad.get("content") or "").strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
            return
        if self._skip_depth > 0:
            return
        self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_domain(url: str) -> str:
    try:
        return urlparse(_normalize_url(url)).netloc or url
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Fetch + search
# ---------------------------------------------------------------------------

async def _fetch_html(url: str) -> tuple[str, str]:
    """Return (page_title, body_text). Raises on HTTP/network error."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_FETCH_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    parser = _TextExtractor()
    parser.feed(resp.text)
    title = parser.title or parser.description
    body = re.sub(r"\s{3,}", "  ", parser.get_text())
    if len(body) > _MAX_CONTENT_CHARS:
        body = body[:_MAX_CONTENT_CHARS] + "…"
    return title, body


async def _search_tavily(domain: str, notes: str) -> tuple[list[str], str]:
    if not settings.tavily_api_key:
        return [], ""
    try:
        from tavily import TavilyClient
        query = f"{domain} company product customers"
        if notes:
            query += f" {notes}"
        raw = await asyncio.to_thread(
            TavilyClient(api_key=settings.tavily_api_key).search,
            query,
            max_results=5,
            search_depth="basic",
        )
        items = raw.get("results", [])
        sources = [r["url"] for r in items if r.get("url")]
        text = "\n\n".join(
            f"{r.get('title', '')}: {r.get('content', '')}" for r in items
        )
        return sources, text[:_MAX_CONTENT_CHARS]
    except Exception as exc:
        log.warning("tavily_search_failed", extra={"domain": domain, "error": str(exc)})
        return [], ""


# ---------------------------------------------------------------------------
# LLM analysis — with JSON repair fallback
# ---------------------------------------------------------------------------

_SYSTEM = """You are a B2B sales intelligence analyst. Analyze website content and extract structured ICP (Ideal Customer Profile) information for an outbound sales team.

CRITICAL OUTPUT FORMAT:
- Return ONLY a valid JSON object
- No markdown, no code fences (```), no comments, no trailing commas
- No unescaped newlines inside string values — use a space instead
- Start your response with { and end with }

Generate SPECIFIC, SALES-USABLE values — never generic marketing copy.
BAD: "We help companies grow."
GOOD: "Helps B2B SaaS teams automate outbound prospecting with enriched lead data and personalized messaging."

Focus on:
- Who buys this product (decision-maker titles, seniority, departments)
- What problems this product solves (buyer pain points)
- What triggers indicate a need (positive signals)
- What makes a lead a bad fit (disqualifiers)
- What industry terms to monitor (news_search_terms)

Strict length limits — never exceed these to keep the JSON small:
- company_name: max 80 chars
- one_liner: max 250 chars
- value_props: max 6 items, each max 120 chars
- differentiators: max 6 items, each max 120 chars
- target_industries: max 8 items
- target_company_sizes: max 4 items
- target_company_stages: max 4 items
- target_tech_stack_signals: max 6 items
- target_geographies: max 6 items
- target_titles: max 8 items
- seniority_levels: max 5 items
- departments: max 6 items
- top_pain_points: max 8 items, each max 120 chars
- common_objections: max 6 items, each max 120 chars
- positive_signals: max 10 items, each max 100 chars
- disqualifiers: max 10 items, each max 100 chars
- news_search_terms: max 10 items, each max 80 chars
- reasoning: object, each value max 120 chars

Leave lists as [] and strings as "" for fields you cannot reasonably infer."""

_REPAIR_SYSTEM = "You are a JSON repair tool. Return ONLY the corrected JSON object — no markdown, no explanation, no code fences. Start with { and end with }."


class _LLMOutput(BaseModel):
    company_name: str = ""
    one_liner: str = ""
    value_props: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    target_company_sizes: list[str] = Field(default_factory=list)
    target_company_stages: list[str] = Field(default_factory=list)
    target_tech_stack_signals: list[str] = Field(default_factory=list)
    target_geographies: list[str] = Field(default_factory=list)
    target_titles: list[str] = Field(default_factory=list)
    seniority_levels: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    top_pain_points: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    news_search_terms: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    reasoning: dict[str, str] = Field(default_factory=dict)


async def _raw_llm_call(system: str, user: str, max_tokens: int = 4000) -> str:
    """Make a raw Anthropic API call and return the text response."""
    from src.llm import client as _llm_client
    response = await _llm_client().messages.create(
        model=settings.content_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    )


def _parse_llm_json(raw: str) -> _LLMOutput:
    """Strip code fences, extract the first JSON object, parse + validate + clamp."""
    text = raw.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    # Skip any non-JSON preamble and find the first { or [
    if text and text[0] not in "{[":
        for start_ch in ("{", "["):
            idx = text.find(start_ch)
            if idx >= 0:
                text = text[idx:]
                break

    parsed = json.loads(text)  # raises json.JSONDecodeError if malformed
    result = _LLMOutput.model_validate(parsed)  # raises ValidationError if schema mismatch
    return _clamp(result)


def _clamp(raw: _LLMOutput) -> _LLMOutput:
    """Enforce field-level length limits regardless of what the LLM returned."""
    def _t(s: str, n: int) -> str:
        return s[:n] if s else s

    def _tl(lst: list, n_items: int, n_chars: int) -> list:
        return [_t(str(x), n_chars) for x in lst[:n_items]]

    def _hl(lst: list, n: int) -> list:
        return lst[:n]

    return _LLMOutput(
        company_name=_t(raw.company_name, 80),
        one_liner=_t(raw.one_liner, 250),
        value_props=_tl(raw.value_props, 6, 120),
        differentiators=_tl(raw.differentiators, 6, 120),
        target_industries=_hl(raw.target_industries, 8),
        target_company_sizes=_hl(raw.target_company_sizes, 4),
        target_company_stages=_hl(raw.target_company_stages, 4),
        target_tech_stack_signals=_hl(raw.target_tech_stack_signals, 6),
        target_geographies=_hl(raw.target_geographies, 6),
        target_titles=_hl(raw.target_titles, 8),
        seniority_levels=_hl(raw.seniority_levels, 5),
        departments=_hl(raw.departments, 6),
        top_pain_points=_tl(raw.top_pain_points, 8, 120),
        common_objections=_tl(raw.common_objections, 6, 120),
        positive_signals=_tl(raw.positive_signals, 10, 100),
        disqualifiers=_tl(raw.disqualifiers, 10, 100),
        news_search_terms=_tl(raw.news_search_terms, 10, 80),
        confidence=raw.confidence or "medium",
        reasoning={k: _t(v, 120) for k, v in (raw.reasoning or {}).items()},
    )


async def _call_llm(content: str, url: str, notes: str) -> _LLMOutput:
    """Analyze content with the LLM. Retries once with a repair call on JSON failure."""
    notes_section = f"\n\nClient notes: {notes}" if notes else ""
    user_prompt = (
        f"Website: {url}{notes_section}\n\n"
        f"Content:\n{content}\n\n"
        "Return a JSON object with these exact keys: company_name, one_liner, value_props, "
        "differentiators, target_industries, target_company_sizes, target_company_stages, "
        "target_tech_stack_signals, target_geographies, target_titles, seniority_levels, "
        "departments, top_pain_points, common_objections, positive_signals, disqualifiers, "
        "news_search_terms, confidence (high/medium/low), reasoning (object: field→reason). "
        "Remember: ONLY valid JSON — no markdown, no code fences."
    )

    # First attempt
    raw = await _raw_llm_call(_SYSTEM, user_prompt, max_tokens=4000)
    try:
        return _parse_llm_json(raw)
    except (json.JSONDecodeError, ValidationError, Exception) as first_err:
        log.warning(
            "autofill_first_parse_failed_attempting_repair",
            extra={"error": str(first_err)[:200], "raw_len": len(raw)},
        )

    # Repair attempt: ask the LLM to fix its own output
    repair_user = (
        "The following JSON is malformed or truncated. "
        "Return ONLY the corrected, complete JSON object. "
        "No markdown. No explanation. No code fences:\n\n"
        f"{raw[:4000]}"
    )
    raw2 = await _raw_llm_call(_REPAIR_SYSTEM, repair_user, max_tokens=4000)
    return _parse_llm_json(raw2)  # raises if still broken — caller handles it


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def analyze_website_async(url: str, notes: str = "") -> AnalysisResult:
    """Fetch and analyze a website URL. Returns an AnalysisResult — does NOT save anything."""
    if not (url or "").strip():
        return AnalysisResult(error="No URL provided.")

    normalized = _normalize_url(url)
    domain = _extract_domain(url)
    sources: list[str] = []
    content_parts: list[str] = []

    # Step 1: direct fetch
    try:
        title, body = await _fetch_html(normalized)
        sources.append(normalized)
        if title:
            content_parts.append(f"Title: {title}")
        if body:
            content_parts.append(body)
    except Exception as fetch_err:
        log.info("direct_fetch_failed", extra={"url": url, "error": str(fetch_err)})
        # Step 2: Tavily fallback
        tavily_sources, tavily_text = await _search_tavily(domain, notes)
        if tavily_text:
            sources.extend(tavily_sources)
            content_parts.append(tavily_text)
        else:
            msg = f"Could not fetch website: {fetch_err}."
            if not settings.tavily_api_key:
                msg += " Tavily search is not configured — paste a company description manually."
            else:
                msg += " Tavily search also returned no results."
            return AnalysisResult(error=msg)

    if not content_parts:
        return AnalysisResult(error="Website returned no readable content.")

    # Step 3: LLM analysis (with built-in repair fallback)
    try:
        raw = await _call_llm("\n\n".join(content_parts), url, notes)
    except Exception as llm_err:
        log.error("llm_analysis_failed", extra={"url": url, "error": str(llm_err)})
        return AnalysisResult(
            sources_used=sources,
            error="Could not parse the analysis output. Please retry or add shorter notes.",
            debug_info=str(llm_err),
        )

    return AnalysisResult(
        suggestions=WebsiteSuggestions(
            company_name=raw.company_name,
            one_liner=raw.one_liner,
            value_props=raw.value_props,
            differentiators=raw.differentiators,
            target_industries=raw.target_industries,
            target_company_sizes=raw.target_company_sizes,
            target_company_stages=raw.target_company_stages,
            target_tech_stack_signals=raw.target_tech_stack_signals,
            target_geographies=raw.target_geographies,
            target_titles=raw.target_titles,
            seniority_levels=raw.seniority_levels,
            departments=raw.departments,
            top_pain_points=raw.top_pain_points,
            common_objections=raw.common_objections,
            positive_signals=raw.positive_signals,
            disqualifiers=raw.disqualifiers,
            news_search_terms=raw.news_search_terms,
        ),
        sources_used=sources,
        confidence=raw.confidence or "medium",
        reasoning=raw.reasoning or {},
    )


def analyze_website(url: str, notes: str = "") -> AnalysisResult:
    """Synchronous wrapper for sync callers. Never saves — for review only."""
    from src.async_runner import run_async
    return run_async(analyze_website_async(url=url, notes=notes))


def apply_suggestions_to_config(
    cfg: "ICPConfig",
    suggestions: WebsiteSuggestions,
    checked_fields: set[str],
) -> "ICPConfig":
    """Return a new ICPConfig with checked suggestion fields applied.

    Unchecked fields are copied unchanged from cfg. Does not touch the DB.
    """
    from src.icp_config import ICPConfig, CompanyProfile, ICPDefinition, BuyerPersona, IntentSignals

    def _str(field: str, current: str, suggested: str) -> str:
        return suggested if (field in checked_fields and suggested) else current

    def _lst(field: str, current: list, suggested: list) -> list:
        return suggested if (field in checked_fields and suggested) else current

    return ICPConfig(
        company=CompanyProfile(
            name=_str("company_name", cfg.company.name, suggestions.company_name),
            one_liner=_str("one_liner", cfg.company.one_liner, suggestions.one_liner),
            value_props=_lst("value_props", cfg.company.value_props, suggestions.value_props),
            differentiators=_lst("differentiators", cfg.company.differentiators, suggestions.differentiators),
        ),
        icp=ICPDefinition(
            target_industries=_lst("target_industries", cfg.icp.target_industries, suggestions.target_industries),
            target_company_sizes=_lst("target_company_sizes", cfg.icp.target_company_sizes, suggestions.target_company_sizes),
            target_company_stages=_lst("target_company_stages", cfg.icp.target_company_stages, suggestions.target_company_stages),
            target_tech_stack_signals=_lst("target_tech_stack_signals", cfg.icp.target_tech_stack_signals, suggestions.target_tech_stack_signals),
            target_geographies=_lst("target_geographies", cfg.icp.target_geographies, suggestions.target_geographies),
        ),
        persona=BuyerPersona(
            target_titles=_lst("target_titles", cfg.persona.target_titles, suggestions.target_titles),
            seniority_levels=_lst("seniority_levels", cfg.persona.seniority_levels, suggestions.seniority_levels),
            departments=_lst("departments", cfg.persona.departments, suggestions.departments),
            top_pain_points=_lst("top_pain_points", cfg.persona.top_pain_points, suggestions.top_pain_points),
            common_objections=_lst("common_objections", cfg.persona.common_objections, suggestions.common_objections),
        ),
        signals=IntentSignals(
            positive_signals=_lst("positive_signals", cfg.signals.positive_signals, suggestions.positive_signals),
            disqualifiers=_lst("disqualifiers", cfg.signals.disqualifiers, suggestions.disqualifiers),
        ),
        news_search_terms=_lst("news_search_terms", cfg.news_search_terms, suggestions.news_search_terms),
        generate_content_for_all_tiers=cfg.generate_content_for_all_tiers,
    )
