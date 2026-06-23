"""READ-ONLY report: find generated content containing internal review /
placeholder text ("NEEDS REVIEW: ...", "SKIP: ...", empty, etc.) and flag any
that were already pushed to Instantly.

This script NEVER modifies the database and NEVER calls Instantly. It only
reports. Remediation (regenerate / delete / re-push corrected copy) is left to
the operator after reviewing the output.

Usage:
    python scripts/report_unsafe_content.py            # human-readable table
    python scripts/report_unsafe_content.py --json      # machine-readable JSON

Run it against the same DATABASE_URL the app/webhook use.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import or_, select

from src.db import session_scope
from src.delivery.eligibility import is_unsafe_internal_content
from src.models import GeneratedContent, Lead


def _was_pushed(row: GeneratedContent) -> bool:
    """True if this content shows any sign of having been pushed to Instantly."""
    return bool(
        (row.delivery_status or "") == "sent"
        or row.delivered_at is not None
        or (row.delivery_id or "").strip()
    )


def collect_unsafe_content() -> list[dict]:
    """Return a list of unsafe-content records (read-only)."""
    records: list[dict] = []
    # Resolve campaign per workspace, cached to avoid repeat lookups.
    from src.workspace import get_campaign_id_for_workspace
    campaign_cache: dict[int | None, str | None] = {}

    with session_scope() as session:
        # Pre-filter cheaply in SQL, then confirm with the shared predicate so
        # this report uses the exact same definition the send paths enforce.
        candidates = session.execute(
            select(GeneratedContent, Lead)
            .join(Lead, Lead.id == GeneratedContent.lead_id, isouter=True)
            .where(
                or_(
                    GeneratedContent.body.is_(None),
                    GeneratedContent.body == "",
                    GeneratedContent.body.ilike("%needs review%"),
                    GeneratedContent.body.ilike("SKIP:%"),
                    GeneratedContent.body.ilike("%buyer account%"),
                )
            )
            .order_by(GeneratedContent.id.asc())
        ).all()

        for content, lead in candidates:
            if not is_unsafe_internal_content(content.subject, content.body):
                continue
            ws_id = content.workspace_id
            if ws_id not in campaign_cache:
                try:
                    campaign_cache[ws_id] = get_campaign_id_for_workspace(ws_id)
                except Exception:
                    campaign_cache[ws_id] = None
            records.append({
                "content_id": content.id,
                "lead_id": content.lead_id,
                "email": (lead.email if lead else None),
                "workspace_id": ws_id,
                "campaign_id": campaign_cache.get(ws_id),
                "kind": content.kind,
                "delivery_status": content.delivery_status,
                "delivered_at": content.delivered_at.isoformat() if content.delivered_at else None,
                "delivery_id": content.delivery_id,
                "skip_reason": content.skip_reason,
                "already_pushed": _was_pushed(content),
                "body_preview": (content.body or "")[:120],
            })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Report unsafe internal-review content (read-only).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    records = collect_unsafe_content()
    pushed = [r for r in records if r["already_pushed"]]
    not_pushed = [r for r in records if not r["already_pushed"]]

    if args.json:
        print(json.dumps({
            "total": len(records),
            "already_pushed": len(pushed),
            "not_pushed": len(not_pushed),
            "records": records,
        }, indent=2))
        return

    print(f"Unsafe-content rows found: {len(records)}")
    print(f"  ALREADY PUSHED to Instantly (needs remediation): {len(pushed)}")
    print(f"  Not sent (blocked / pending):                    {len(not_pushed)}")
    if not records:
        print("\nNo unsafe internal-review content found. Nothing to do.")
        return

    print("\n{:<10} {:<8} {:<30} {:<6} {:<18} {:<8} {}".format(
        "content", "lead", "email", "ws", "campaign", "kind", "status"))
    print("-" * 110)
    for r in records:
        flag = "PUSHED" if r["already_pushed"] else (r["delivery_status"] or "not_sent")
        print("{:<10} {:<8} {:<30} {:<6} {:<18} {:<8} {}".format(
            r["content_id"], r["lead_id"], str(r["email"])[:30],
            str(r["workspace_id"]), str(r["campaign_id"])[:18], r["kind"], flag))

    if pushed:
        print("\nWARNING: The leads marked PUSHED above were already delivered with internal")
        print("    review text. Review and remediate (regenerate + re-push corrected copy,")
        print("    or follow up manually). This script does NOT modify anything.")


if __name__ == "__main__":
    main()
