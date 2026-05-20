"""Company post history via harvestapi/linkedin-company-posts."""
from src.config import settings
from src.enrichment._apify import run_actor
from src.enrichment.schemas import CompanyPost
from src.retry import retry_api


def _parse(raw: dict) -> CompanyPost:
    return CompanyPost(
        text=raw.get("text") or raw.get("content") or raw.get("postText"),
        posted_at=raw.get("postedAt") or raw.get("date") or raw.get("publishedAt"),
        url=raw.get("url") or raw.get("postUrl"),
        raw=raw,
    )


@retry_api
async def fetch_company_posts(company_url: str, limit: int = 10) -> list[CompanyPost]:
    if not company_url:
        return []
    items = await run_actor(
        settings.apify_actor_company_posts,
        run_input={"companies": [company_url], "maxPosts": limit},
    )
    return [_parse(it) for it in items[:limit]]
