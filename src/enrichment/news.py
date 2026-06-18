"""Company + industry news via Tavily."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from tavily import TavilyClient

from src.config import settings
from src.enrichment.schemas import NewsItem
from src.retry import retry_api

if TYPE_CHECKING:
    from src.icp_config import ICPConfig

log = logging.getLogger(__name__)


def _client() -> TavilyClient:
    return TavilyClient(api_key=settings.tavily_api_key)


def _parse(item: dict) -> Optional[NewsItem]:
    url = item.get("url")
    title = item.get("title")
    if not url or not title:
        return None
    return NewsItem(
        title=title,
        url=url,
        snippet=item.get("content") or item.get("snippet"),
        published_at=item.get("published_date") or item.get("publishedAt"),
        score=item.get("score"),
    )


def _search_sync(
    query: str,
    max_results: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    # Date window (start_date/end_date) bounds company-news to the configured
    # buyer-research window so this feed stops returning only the last few days.
    # NEVER time_range=day/week. No window passed → unbounded (industry trends).
    kwargs: dict = {
        "query": query,
        "topic": "news",
        "max_results": max_results,
        "search_depth": "basic",
    }
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    return _client().search(**kwargs).get("results", [])


@retry_api
async def search_news(
    query: str,
    max_results: int = 5,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[NewsItem]:
    if not query:
        return []
    raw = await asyncio.to_thread(
        _search_sync, query, max_results, start_date=start_date, end_date=end_date,
    )
    return [n for n in (_parse(r) for r in raw) if n is not None]


def _company_news_query(company: str, icp: "ICPConfig | None") -> str:
    # ICP terms scope the search to topics that map to our value prop, so
    # results for a generically-named company stay anchored to our pitch
    # (e.g. "Parakeet outbound sales SDR automation" not just "Parakeet").
    if icp is not None and icp.news_search_terms:
        qualifier = " ".join(icp.news_search_terms[:2])
        return f'"{company}" {qualifier}'
    return f'"{company}" news OR funding OR launch OR hire'


def _industry_news_query(industry: str, icp: "ICPConfig | None") -> str:
    # The lead's raw industry tag pulls noise (Sales Technology -> Shopify,
    # automotive, electronics). ICP-defined search terms are the user's own
    # description of which industry conversation actually maps to their
    # pitch — so use those when configured.
    if icp is not None and icp.news_search_terms:
        terms = " ".join(icp.news_search_terms)
        return f"{terms} 2026"
    return f"{industry} industry trends 2026"


async def fetch_company_news(
    company: str,
    max_results: int = 5,
    *,
    icp: "ICPConfig | None" = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[NewsItem]:
    if not company:
        return []
    return await search_news(
        _company_news_query(company, icp),
        max_results=max_results,
        start_date=start_date,
        end_date=end_date,
    )


async def fetch_industry_news(
    industry: str,
    max_results: int = 5,
    *,
    icp: "ICPConfig | None" = None,
) -> list[NewsItem]:
    if not industry and (icp is None or not icp.news_search_terms):
        return []
    return await search_news(_industry_news_query(industry, icp), max_results=max_results)
