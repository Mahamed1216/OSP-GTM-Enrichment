"""run_actor must raise on every non-SUCCEEDED outcome.

Regression: previously it silently returned [] when the run had no
defaultDatasetId, which let TIMED-OUT/FAILED runs masquerade as
legitimate no-results in the waterfall.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.enrichment._apify import ApifyRunFailed, run_actor


def _mock_client(call_return, list_items_return=None):
    """Build a mock that mirrors ApifyClientAsync's chained API surface."""
    actor_client = MagicMock()
    actor_client.call = AsyncMock(return_value=call_return)

    dataset_client = MagicMock()
    dataset_client.list_items = AsyncMock(
        return_value=SimpleNamespace(items=list_items_return or [])
    )

    client = MagicMock()
    client.actor = MagicMock(return_value=actor_client)
    client.dataset = MagicMock(return_value=dataset_client)
    return client


@pytest.mark.asyncio
async def test_raises_on_failed_status():
    client = _mock_client(
        call_return={
            "status": "FAILED",
            "statusMessage": "boom",
            "id": "run_1",
            "defaultDatasetId": "ds_1",
        }
    )
    with patch("src.enrichment._apify.apify_client", return_value=client):
        with pytest.raises(ApifyRunFailed) as exc_info:
            await run_actor("some/actor", {"urls": ["x"]})
    assert "FAILED" in str(exc_info.value)
    assert "boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_raises_on_timed_out_status_even_with_dataset():
    """A TIMED-OUT run with a partial dataset is still an error.

    Diagnostic showed the supreme_coder/linkedin-post actor times out
    while still producing 1000+ items. Treating that as success is the
    exact bug we're fixing — half-data masquerading as green ✓.
    """
    client = _mock_client(
        call_return={
            "status": "TIMED-OUT",
            "id": "run_2",
            "defaultDatasetId": "ds_2",
        },
        list_items_return=[{"hello": "world"}],
    )
    with patch("src.enrichment._apify.apify_client", return_value=client):
        with pytest.raises(ApifyRunFailed) as exc_info:
            await run_actor("some/actor", {"urls": ["x"]})
    assert "TIMED-OUT" in str(exc_info.value)


@pytest.mark.asyncio
async def test_raises_on_missing_dataset_id():
    client = _mock_client(
        call_return={
            "status": "SUCCEEDED",
            "id": "run_3",
            "defaultDatasetId": None,
        }
    )
    with patch("src.enrichment._apify.apify_client", return_value=client):
        with pytest.raises(ApifyRunFailed) as exc_info:
            await run_actor("some/actor", {"urls": ["x"]})
    assert "no defaultDatasetId" in str(exc_info.value)


@pytest.mark.asyncio
async def test_raises_on_falsy_run():
    client = _mock_client(call_return=None)
    with patch("src.enrichment._apify.apify_client", return_value=client):
        with pytest.raises(ApifyRunFailed) as exc_info:
            await run_actor("some/actor", {"urls": ["x"]})
    assert "no run object" in str(exc_info.value)


@pytest.mark.asyncio
async def test_succeeded_with_empty_dataset_returns_empty_list():
    """Legitimate no-results path must still return [] cleanly.

    The waterfall classifier turns this into status='no_results'
    (yellow ⚠), not green ✓ — that part is tested in test_enrichment.
    """
    client = _mock_client(
        call_return={
            "status": "SUCCEEDED",
            "id": "run_4",
            "defaultDatasetId": "ds_4",
        },
        list_items_return=[],
    )
    with patch("src.enrichment._apify.apify_client", return_value=client):
        items = await run_actor("some/actor", {"urls": ["x"]})
    assert items == []


@pytest.mark.asyncio
async def test_succeeded_with_items_returns_them():
    payload = [{"id": 1}, {"id": 2}]
    client = _mock_client(
        call_return={
            "status": "SUCCEEDED",
            "id": "run_5",
            "defaultDatasetId": "ds_5",
        },
        list_items_return=payload,
    )
    with patch("src.enrichment._apify.apify_client", return_value=client):
        items = await run_actor("some/actor", {"urls": ["x"]})
    assert items == payload
