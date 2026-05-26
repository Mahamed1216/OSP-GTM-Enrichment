"""Diagnose what each Apify actor actually returns.

Calls each actor once with hard-coded public URLs, prints the raw run
object's status fields and the dataset's item shape. Does NOT go
through src.enrichment._apify.run_actor — we want to see what the
client gives us before any wrapping happens.

Run:  python scripts/diagnose_apify.py
"""
import asyncio
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apify_client import ApifyClientAsync

from src.config import settings
from src.logging_setup import setup_logging


PROFILE_URL = "https://www.linkedin.com/in/williamhgates"
COMPANY_URL = "https://www.linkedin.com/company/microsoft"


def _line(char: str = "=", n: int = 70) -> str:
    return char * n


async def _probe(client: ApifyClientAsync, label: str, actor_id: str, run_input: dict) -> None:
    print(f"\n{_line('=')}")
    print(f"  {label}")
    print(f"  actor_id = {actor_id!r}")
    print(f"  run_input = {run_input!r}")
    print(_line('='))

    start = time.monotonic()
    try:
        run = await client.actor(actor_id).call(run_input=run_input)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        print(f"  EXCEPTION after {duration_ms} ms: {exc!r}")
        traceback.print_exc()
        return

    duration_ms = int((time.monotonic() - start) * 1000)
    print(f"  wall-clock duration: {duration_ms} ms")
    print(f"  type(run) = {type(run).__name__}")

    if not run:
        print(f"  run is falsy: {run!r}")
        return

    if isinstance(run, dict):
        print(f"  run keys: {sorted(run.keys())}")
    print(f"  run.status        = {run.get('status')!r}")
    print(f"  run.statusMessage = {run.get('statusMessage')!r}")
    print(f"  run.id            = {run.get('id')!r}")
    print(f"  run.startedAt     = {run.get('startedAt')!r}")
    print(f"  run.finishedAt    = {run.get('finishedAt')!r}")
    print(f"  run.defaultDatasetId = {run.get('defaultDatasetId')!r}")
    print(f"  run.exitCode      = {run.get('exitCode')!r}")

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        print("  -> no defaultDatasetId; cannot list items")
        return

    try:
        items_page = await client.dataset(dataset_id).list_items()
    except Exception as exc:
        print(f"  dataset list_items() raised: {exc!r}")
        traceback.print_exc()
        return

    items = list(items_page.items or [])
    print(f"  dataset items count: {len(items)}")
    if items:
        first = items[0]
        if isinstance(first, dict):
            print(f"  first item top-level keys: {sorted(first.keys())}")
        else:
            print(f"  first item type: {type(first).__name__}")


async def amain() -> int:
    setup_logging()
    if not settings.apify_api_token:
        print("ERROR: APIFY_API_TOKEN is empty in settings — check .env")
        return 1

    print(f"APIFY_API_TOKEN length: {len(settings.apify_api_token)}")
    client = ApifyClientAsync(token=settings.apify_api_token)

    probes = [
        (
            "1. linkedin_profile",
            settings.apify_actor_linkedin_profile,
            {"profileUrls": [PROFILE_URL]},
        ),
        (
            "2. linkedin_posts (using 'urls' per API error)",
            settings.apify_actor_linkedin_posts,
            {"urls": [PROFILE_URL], "limit": 10},
        ),
        (
            "3. company_details (using 'urls' per API error)",
            settings.apify_actor_company_details,
            {"urls": [COMPANY_URL]},
        ),
    ]

    failures: list[tuple[str, BaseException]] = []
    for label, actor_id, run_input in probes:
        try:
            await _probe(client, label, actor_id, run_input)
        except BaseException as exc:
            failures.append((label, exc))
            print(f"  outer EXCEPTION: {exc!r}")

    print(f"\n{_line('-')}")
    if failures:
        print(f"completed with {len(failures)} failure(s):")
        for label, exc in failures:
            print(f"  - {label}: {exc!r}")
        return 2
    print("completed all 4 probes without outer exceptions")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
