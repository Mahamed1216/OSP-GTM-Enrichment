"""Pre-flight diagnostic for Instantly: verifies the API key works,
the configured campaign exists, sender(s) are attached, and prints
the current lead count.

Run:  python scripts/instantly_ping.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.config import settings
from src.delivery.instantly import count_campaign_leads, get_campaign
from src.logging_setup import setup_logging


# Instantly v2 campaign.status is an integer; map the values they document.
_STATUS_LABELS = {
    0: "Draft",
    1: "Active",
    2: "Paused",
    3: "Completed",
    4: "Running Subsequences",
    -99: "Account Suspended",
    -1: "Accounts Unhealthy",
    -2: "Bounce Protect",
}


def _status_label(raw: object) -> str:
    if isinstance(raw, int) and raw in _STATUS_LABELS:
        return f"{_STATUS_LABELS[raw]} ({raw})"
    if isinstance(raw, str) and raw.strip():
        return raw
    return f"unknown ({raw!r})"


def _extract_senders(campaign: dict) -> list[str]:
    """Pull sender emails from whichever shape the campaign object uses."""
    for key in ("email_list", "sending_accounts", "accounts", "from_email"):
        val = campaign.get(key)
        if isinstance(val, list):
            return [str(v) for v in val if v]
        if isinstance(val, str) and val:
            return [val]
    return []


async def amain() -> int:
    setup_logging()

    if not settings.instantly_api_key:
        print("✗ INSTANTLY_API_KEY is not set in .env")
        return 2
    if not settings.instantly_campaign_id:
        print("✗ INSTANTLY_CAMPAIGN_ID is not set in .env")
        return 2

    campaign_id = settings.instantly_campaign_id

    # ---- Fetch campaign (covers key validity + campaign existence) ----
    try:
        campaign = await get_campaign(campaign_id)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            print(f"✗ API key rejected (HTTP {code}) — check INSTANTLY_API_KEY in .env")
            return 3
        if code == 404:
            print(f'✗ Campaign "{campaign_id}" not found (HTTP 404) — check INSTANTLY_CAMPAIGN_ID')
            return 4
        print(f"✗ Unexpected HTTP {code} from Instantly: {exc.response.text[:200]}")
        return 6
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
        print(f"✗ Could not reach Instantly: {type(exc).__name__}: {exc}")
        return 6
    except Exception as exc:
        print(f"✗ Unexpected error fetching campaign: {type(exc).__name__}: {exc}")
        return 6

    print("✓ API key valid")

    name = campaign.get("name") or campaign.get("campaign_name") or "(no name)"
    print(f'✓ Campaign "{name}" found')

    status = campaign.get("status")
    print(f"  Status: {_status_label(status)}")

    senders = _extract_senders(campaign)
    if not senders:
        print("  Sender: (none attached)")
        print("✗ Campaign found, but no sender accounts linked — "
              "open the campaign in Instantly and attach a warmed sender.")
        return 5
    label = "Sender" if len(senders) == 1 else "Senders"
    print(f"  {label}: {', '.join(senders)}")

    # ---- Lead count (best effort) ----
    try:
        leads = await count_campaign_leads(campaign_id)
    except Exception as exc:
        print(f"  Current leads: unknown ({type(exc).__name__})")
    else:
        print(f"  Current leads: {leads if leads is not None else 'unknown'}")

    print("Ready for live send.")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
