"""Critical path: waterfall must never raise, even when every source fails."""
from unittest.mock import patch

import pytest

from src.enrichment.schemas import LinkedInProfile
from src.enrichment.waterfall import enrich_lead


async def _boom(*args, **kwargs):
    raise RuntimeError("simulated failure")


@pytest.mark.asyncio
async def test_waterfall_does_not_raise_when_all_sources_fail(sample_lead_id):
    with patch("src.enrichment.waterfall.fetch_linkedin_profile", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_company_details", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_company_news", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_industry_news", side_effect=_boom):
        status = await enrich_lead(sample_lead_id)

    assert len(status) == 4
    assert all(s["success"] is False for s in status.values())
    assert all(s["error"] for s in status.values())
    assert all("duration_ms" in s for s in status.values())


@pytest.mark.asyncio
async def test_waterfall_records_partial_success(sample_lead_id):
    async def ok_profile(_url):
        return LinkedInProfile(full_name="Test User", headline="VP of Sales", raw={"x": 1})

    with patch("src.enrichment.waterfall.fetch_linkedin_profile", side_effect=ok_profile), \
         patch("src.enrichment.waterfall.fetch_company_details", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_company_news", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_industry_news", side_effect=_boom):
        status = await enrich_lead(sample_lead_id)

    assert status["linkedin_profile"]["success"] is True
    assert status["company_details"]["success"] is False


@pytest.mark.asyncio
async def test_waterfall_persists_payload(sample_lead_id):
    """Verify enrichment row is written even when sources fail."""
    from sqlalchemy import select

    from src.db import session_scope
    from src.models import Enrichment

    with patch("src.enrichment.waterfall.fetch_linkedin_profile", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_company_details", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_company_news", side_effect=_boom), \
         patch("src.enrichment.waterfall.fetch_industry_news", side_effect=_boom):
        await enrich_lead(sample_lead_id)

    with session_scope() as session:
        row = session.execute(
            select(Enrichment).where(Enrichment.lead_id == sample_lead_id)
        ).scalar_one_or_none()
        assert row is not None
        assert row.source_status
        assert len(row.source_status) == 4


@pytest.mark.asyncio
async def test_empty_payload_does_not_render_as_success(sample_lead_id):
    """Regression: a source returning [] (no exception) must NOT show green ✓.

    Previously Apify returning zero items would log success=True with a
    ~0ms duration and an empty payload, hiding real failures. The
    classifier should mark these as "no_results" (yellow ⚠) or
    "skipped" — never "ok".
    """
    async def empty_list(*_args, **_kwargs):
        return []

    async def empty_none(*_args, **_kwargs):
        return None

    with patch("src.enrichment.waterfall.fetch_linkedin_profile", side_effect=empty_none), \
         patch("src.enrichment.waterfall.fetch_company_details", side_effect=empty_none), \
         patch("src.enrichment.waterfall.fetch_company_news", side_effect=empty_list), \
         patch("src.enrichment.waterfall.fetch_industry_news", side_effect=empty_list):
        status = await enrich_lead(sample_lead_id)

    for name, info in status.items():
        # The bug we're guarding against: empty payload reporting as success.
        assert info["success"] is False, (
            f"{name} returned empty payload but reported success=True"
        )
        assert info["status"] != "ok", (
            f"{name} returned empty payload but classified as 'ok'"
        )
        assert info["status"] in ("no_results", "skipped"), (
            f"{name} should be no_results/skipped, got {info['status']!r}"
        )
        # Reason must be set so the UI can show *why* it wasn't green.
        assert info["reason"], f"{name} missing reason for non-ok status"


@pytest.mark.asyncio
async def test_nonempty_payload_classified_as_ok(sample_lead_id):
    """Inverse: a real returned payload must be 'ok' even with fast mocked duration."""
    async def ok_profile(_url):
        return LinkedInProfile(full_name="Test User", headline="VP", raw={"x": 1})

    async def empty_list(*_args, **_kwargs):
        return []

    with patch("src.enrichment.waterfall.fetch_linkedin_profile", side_effect=ok_profile), \
         patch("src.enrichment.waterfall.fetch_company_details", side_effect=empty_list), \
         patch("src.enrichment.waterfall.fetch_company_news", side_effect=empty_list), \
         patch("src.enrichment.waterfall.fetch_industry_news", side_effect=empty_list):
        status = await enrich_lead(sample_lead_id)

    assert status["linkedin_profile"]["status"] == "ok"
    assert status["linkedin_profile"]["success"] is True
    for other in ("company_details", "company_news", "industry_news"):
        assert status[other]["status"] in ("no_results", "skipped")
        assert status[other]["success"] is False
