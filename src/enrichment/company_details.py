"""Company LinkedIn page details via rigelbytes/linkedin-company-details."""
from typing import Optional

from src.config import settings
from src.enrichment._apify import run_actor
from src.enrichment.schemas import CompanyDetails
from src.retry import retry_api


def _to_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse(raw: dict) -> CompanyDetails:
    return CompanyDetails(
        name=raw.get("name") or raw.get("companyName"),
        tagline=raw.get("tagline") or raw.get("slogan"),
        description=raw.get("description") or raw.get("about"),
        industry=raw.get("industry") or raw.get("industries"),
        employee_count=_to_int(raw.get("employeeCount") or raw.get("employees")),
        headquarters=raw.get("headquarters") or raw.get("location") or raw.get("hq"),
        founded=_to_int(raw.get("founded") or raw.get("foundedYear")),
        website=raw.get("website") or raw.get("websiteUrl"),
        raw=raw,
    )


@retry_api
async def fetch_company_details(company_url: str) -> Optional[CompanyDetails]:
    if not company_url:
        return None
    items = await run_actor(
        settings.apify_actor_company_details,
        run_input={"urls": [company_url]},
    )
    return _parse(items[0]) if items else None
