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


def _to_dict(obj: Any) -> dict[str, Any]:
    """Normalize an Apify SDK return value into a plain dict.

    The SDK's response shape has shifted across versions:
      - Older clients returned a dict directly.
      - Newer clients return a Pydantic-style `Run` model.
      - Some versions return an in-between object with neither `.get`
        nor `model_dump` but with snake_case attributes.

    The previous code did `obj.status if hasattr(obj, 'status') else obj.get('status')`,
    which crashes with `AttributeError: 'Run' object has no attribute 'get'`
    when the SDK returns a Run that has SOME of those attributes but
    falls into the `.get` fallback. This helper collapses all those
    cases into a dict so the rest of the function can read keys
    uniformly without per-key hasattr guards.

    Returns {} for None / unrecognised inputs.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    # Pydantic v2 / dataclass / attrs models.
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                result = method()
            except TypeError:
                continue
            if isinstance(result, dict):
                return result
    # Last resort: shallow attribute scan. Skip dunders and callables so
    # bound methods don't leak into the dict.
    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        result[name] = value
    return result


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

    # Normalize once. Every read below is a plain dict.get(...) — no more
    # hasattr ladders, no more AttributeError on .get fallthrough.
    run_d = _to_dict(run)

    status = run_d.get("status")
    if status != "SUCCEEDED":
        msg = run_d.get("status_message") or run_d.get("statusMessage")
        run_id = run_d.get("id")
        raise ApifyRunFailed(
            f"{actor_id}: status={status!r} msg={msg!r} run_id={run_id!r}"
        )

    dataset_id = run_d.get("default_dataset_id") or run_d.get("defaultDatasetId")
    if not dataset_id:
        run_id = run_d.get("id")
        raise ApifyRunFailed(
            f"{actor_id}: SUCCEEDED but no defaultDatasetId (run_id={run_id!r})"
        )

    items_page = await client.dataset(dataset_id).list_items()
    return list(items_page.items or [])
