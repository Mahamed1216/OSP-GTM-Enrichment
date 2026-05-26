"""LinkedIn profile enrichment via dev_fusion/Linkedin-Profile-Scraper."""
from typing import Optional

from src.config import settings
from src.enrichment._apify import run_actor
from src.enrichment.schemas import LinkedInProfile
from src.retry import retry_api


def _first(items: list, key: str) -> Optional[str]:
    if items and isinstance(items[0], dict):
        return items[0].get(key)
    return None


def _parse(raw: dict) -> LinkedInProfile:
    experiences = raw.get("experiences") or raw.get("experience") or []
    return LinkedInProfile(
        full_name=raw.get("fullName") or raw.get("name"),
        headline=raw.get("headline"),
        about=raw.get("about") or raw.get("summary"),
        location=raw.get("location") or raw.get("addressWithCountry"),
        current_company=raw.get("companyName") or _first(experiences, "company") or _first(experiences, "companyName"),
        current_title=raw.get("jobTitle") or _first(experiences, "title"),
        raw=raw,
    )


@retry_api
async def fetch_linkedin_profile(profile_url: str) -> Optional[LinkedInProfile]:
    if not profile_url:
        return None
    if profile_url.startswith("http://"):
        profile_url = "https://" + profile_url[7:]
    if "://www." not in profile_url:
        profile_url = profile_url.replace("://", "://www.", 1)
    items = await run_actor(
        settings.apify_actor_linkedin_profile,
        run_input={"profileUrls": [profile_url]},
    )
    return _parse(items[0]) if items else None
