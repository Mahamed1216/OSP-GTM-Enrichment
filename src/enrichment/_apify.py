"""Shared helper for Apify actor calls."""
from typing import Any

from apify_client import ApifyClientAsync

from src.config import settings


class ApifyRunFailed(RuntimeError):
    """Apify actor run did not complete with status='SUCCEEDED'.

    Surfaced so the waterfall classifier marks the source as `error`
    rather than silently treating a TIMED-OUT/FAILED/ABORTED run as
    legitimate no-results. A diagnostic showed TIMED-OUT runs can
    still carry a partial dataset; we still raise because half-data
    masquerading as success is worse than an explicit error.
    """


def apify_client() -> ApifyClientAsync:
    return ApifyClientAsync(token=settings.apify_api_token)


async def run_actor(actor_id: str, run_input: dict, timeout_secs: int = 180) -> list[dict[str, Any]]:
    """Run an Apify actor and return its dataset items.

    Raises:
        ApifyRunFailed: client returned no run, run.status != 'SUCCEEDED',
            or succeeded run has no defaultDatasetId.
        ApifyApiError / network errors: propagated; caller's @retry_api
            retries only 5xx transients per src/retry.py.
    """
    client = apify_client()
    run = await client.actor(actor_id).call(run_input=run_input)
    if not run:
        raise ApifyRunFailed(f"{actor_id}: client returned no run object")

    status = run.status if hasattr(run, 'status') else run.get("status")
    if status != "SUCCEEDED":
        raise ApifyRunFailed(
            f"{actor_id}: status={status!r} "
            f"msg={getattr(run, 'status_message', None) if hasattr(run, 'status_message') else run.get('statusMessage')!r} run_id={getattr(run, 'id', None) if hasattr(run, 'id') else run.get('id')!r}"
        )

    dataset_id = run.default_dataset_id if hasattr(run, 'default_dataset_id') else run.get("defaultDatasetId")
    if not dataset_id:
        raise ApifyRunFailed(
            f"{actor_id}: SUCCEEDED but no defaultDatasetId (run_id={getattr(run, 'id', None) if hasattr(run, 'id') else run.get('id')!r})"
        )

    items_page = await client.dataset(dataset_id).list_items()
    return list(items_page.items or [])
