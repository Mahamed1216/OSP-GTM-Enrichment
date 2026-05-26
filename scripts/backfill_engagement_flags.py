"""One-shot backfill: re-derive Engagement boolean flags from raw payloads.

Why this exists: the old `_parse_metrics` looked for `emails_sent_count` and
`delivered` keys, neither of which appears in Instantly v2's per-lead
response. Every Engagement row ended up with sent=False / delivered=False
even when Instantly had actually sent the email. The fix in
src/feedback/engagement.py uses `timestamp_last_contact` instead, but every
existing row still has the wrong flag values. This script re-runs the
corrected parser over each row's stored `raw` JSON — no new API calls.

Idempotent. Safe to re-run. Prints before/after counts.

Run: python scripts/backfill_engagement_flags.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import case, func, select

from src.db import init_db, session_scope
from src.feedback.engagement import _parse_metrics
from src.models import Engagement


def _counts(session) -> dict[str, int]:
    row = session.execute(
        select(
            func.count(Engagement.id).label("total"),
            func.sum(case((Engagement.sent.is_(True), 1), else_=0)).label("sent"),
            func.sum(case((Engagement.delivered.is_(True), 1), else_=0)).label("delivered"),
            func.sum(case((Engagement.opened.is_(True), 1), else_=0)).label("opened"),
            func.sum(case((Engagement.clicked.is_(True), 1), else_=0)).label("clicked"),
            func.sum(case((Engagement.replied.is_(True), 1), else_=0)).label("replied"),
            func.sum(case((Engagement.bounced.is_(True), 1), else_=0)).label("bounced"),
        )
    ).one()
    return {
        "total": int(row.total or 0),
        "sent": int(row.sent or 0),
        "delivered": int(row.delivered or 0),
        "opened": int(row.opened or 0),
        "clicked": int(row.clicked or 0),
        "replied": int(row.replied or 0),
        "bounced": int(row.bounced or 0),
    }


def main() -> int:
    init_db()

    with session_scope() as session:
        before = _counts(session)
    print("Before:", before)

    updated = 0
    skipped_no_raw = 0
    with session_scope() as session:
        rows = session.execute(select(Engagement)).scalars().all()
        for eng in rows:
            raw = eng.raw
            if not isinstance(raw, dict):
                skipped_no_raw += 1
                continue
            metrics = _parse_metrics(raw)
            changed = False
            for key in ("sent", "delivered", "opened", "clicked", "replied", "bounced"):
                new_val = bool(metrics[key])
                if bool(getattr(eng, key)) != new_val:
                    setattr(eng, key, new_val)
                    changed = True
            if changed:
                eng.synced_at = datetime.utcnow()
                updated += 1

    with session_scope() as session:
        after = _counts(session)
    print("After: ", after)
    print(f"Rows updated: {updated}, skipped (no raw payload): {skipped_no_raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
