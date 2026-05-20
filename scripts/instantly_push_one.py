"""End-to-end smoke for a single lead push.

Sends one lead to Instantly via the real `deliver_email()` prod path,
immediately re-fetches the lead from Instantly's API, and diffs the
custom_variables against what's in the local DB.

Run:  python scripts/instantly_push_one.py <lead_id>

Exit 0 = personalized_subject + personalized_body landed correctly.
Exit non-zero = the placeholder substitution didn't work — campaign
template likely references the wrong variable names, OR _build_payload
is still using the old keys.
"""
import asyncio
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select

from src.config import settings
from src.db import session_scope
from src.delivery.instantly import (
    DeliveryResult,
    _build_payload,
    deliver_email,
    get_lead,
)
from src.logging_setup import setup_logging
from src.models import GeneratedContent, Lead


def _preview(s: str, n: int = 200) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + "…"


def _print_local_payload(lead_id: int) -> tuple[str, str, str]:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise SystemExit(f"Lead {lead_id} not found in DB.")
        content = session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        if not content:
            raise SystemExit(f"Lead {lead_id} has no email-kind content. Generate first.")
        payload = _build_payload(lead, content)
        local_subject = content.subject or ""
        local_body = content.body
        email = lead.email

    print("─" * 70)
    print(f"Local payload for lead {lead_id} → {email}")
    print(f"  campaign:              {payload['campaign']}")
    print(f"  personalized_subject:  {_preview(local_subject, 120)}")
    print(f"  personalized_body:     {_preview(local_body, 200)}")
    print("─" * 70)
    return email, local_subject, local_body


async def amain() -> int:
    setup_logging()

    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Usage: python scripts/instantly_push_one.py <lead_id>")
        return 2
    lead_id = int(sys.argv[1])

    if not settings.instantly_api_key or not settings.instantly_campaign_id:
        print("✗ INSTANTLY_API_KEY or INSTANTLY_CAMPAIGN_ID not set in .env")
        return 2

    email, local_subject, local_body = _print_local_payload(lead_id)

    print(f"\nSending lead {lead_id} via deliver_email(...)…")
    try:
        result: DeliveryResult = await deliver_email(lead_id, dry_run=False)
    except Exception as exc:
        print(f"✗ deliver_email raised: {type(exc).__name__}: {exc}")
        return 3

    if result.dry_run:
        print("✗ deliver_email returned dry_run — unexpected.")
        return 3
    if not result.delivered:
        print(f"✗ deliver_email returned skip_reason={result.skip_reason!r}")
        print("  (Check tier gate, dedupe, email verification, or content guard.)")
        return 4

    remote_id = result.delivery_id
    print(f"✓ Sent. Instantly remote id: {remote_id}")

    print(f"\nFetching back from GET /api/v2/leads/{remote_id} …")
    try:
        remote = await get_lead(remote_id)
    except Exception as exc:
        print(f"✗ Read-back failed: {type(exc).__name__}: {exc}")
        return 5

    custom = remote.get("custom_variables") or remote.get("variables") or {}
    print("─" * 70)
    print("Remote custom_variables:")
    for k, v in custom.items():
        print(f"  {k}: {_preview(str(v), 200)}")
    print("─" * 70)

    remote_subject = custom.get("personalized_subject", "")
    remote_body = custom.get("personalized_body", "")

    subject_ok = remote_subject == local_subject
    body_ok = remote_body == local_body

    if subject_ok and body_ok:
        print("\n✓ Custom variables match — campaign template will substitute correctly.")
        print("  Wait a few minutes; verify the email lands personalized in the burn inbox.")
        return 0

    print("\n✗ Mismatch detected.")
    if not subject_ok:
        print("  personalized_subject:")
        print(f"    LOCAL:  {_preview(local_subject, 200)}")
        print(f"    REMOTE: {_preview(remote_subject, 200)}")
    if not body_ok:
        print("  personalized_body:")
        print(textwrap.indent(f"LOCAL:\n{_preview(local_body, 500)}", "    "))
        print(textwrap.indent(f"REMOTE:\n{_preview(remote_body, 500)}", "    "))
    print("\nFix: confirm _build_payload writes 'personalized_subject' / 'personalized_body'.")
    return 6


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
