"""LinkedIn personal post history via supreme_coder/linkedin-post."""
from src.config import settings
from src.enrichment._apify import run_actor
from src.enrichment.schemas import LinkedInPost
from src.retry import retry_api


def _parse(raw: dict) -> LinkedInPost:
    return LinkedInPost(
        text=raw.get("text") or raw.get("content") or raw.get("postText"),
        posted_at=raw.get("postedAt") or raw.get("timeSincePosted") or raw.get("date"),
        url=raw.get("url") or raw.get("postUrl"),
        likes=raw.get("likes") or raw.get("numLikes") or raw.get("reactions"),
        comments=raw.get("comments") or raw.get("numComments"),
        raw=raw,
    )


@retry_api
async def fetch_linkedin_posts(profile_url: str, limit: int = 3) -> list[LinkedInPost]:
    if not profile_url:
        return []
    # supreme_coder/linkedin-post ignored `limit` in practice (returned 90+ on
    # a 10-cap). Pass several common Apify cap keys so whichever the actor
    # honors takes effect, and slice in Python as the hard guarantee.
    items = await run_actor(
        settings.apify_actor_linkedin_posts,
        run_input={
            "urls": [profile_url],
            "limit": limit,
            "maxPosts": limit,
            "maxItems": limit,
        },
    )
    return [_parse(it) for it in items[:limit]]
