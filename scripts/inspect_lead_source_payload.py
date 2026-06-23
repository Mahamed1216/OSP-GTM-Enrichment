"""Inspect imported OSP Lead Engine payloads for a workspace (read-only).

Prints, per lead: lead id, company, email, external contact id, whether a
raw_payload exists, whether signals exist, the signal keys, matched ICPs,
source tier, and source tier score. Does NOT modify any data.

Usage:
    python -m scripts.inspect_lead_source_payload --workspace-id 1 --limit 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from src.db import init_db, session_scope
from src.models import Lead


def _signal_keys(raw: dict) -> list[str]:
    """Best-effort: list the keys present on payload.signals (first item)."""
    signals = (raw or {}).get("signals")
    if isinstance(signals, list) and signals:
        first = signals[0]
        if isinstance(first, dict):
            return sorted(first.keys())
        return [f"<{type(first).__name__}>"]
    if isinstance(signals, dict):
        return sorted(signals.keys())
    return []


def inspect_workspace(workspace_id: int, limit: int = 10) -> list[dict]:
    """Return inspection rows (no mutation)."""
    out: list[dict] = []
    with session_scope() as session:
        rows = session.execute(
            select(Lead)
            .where(Lead.workspace_id == workspace_id)
            .order_by(Lead.id.desc())
            .limit(limit)
        ).scalars().all()
        for lead in rows:
            raw = lead.lead_source_raw or {}
            signals = raw.get("signals")
            has_signals = bool(signals)
            out.append({
                "lead_id": lead.id,
                "company": lead.company or "",
                "email": lead.email or "",
                "external_contact_id": lead.external_contact_id or "",
                "has_raw_payload": bool(raw),
                "has_signals": has_signals,
                "signal_keys": _signal_keys(raw),
                "matched_icps": raw.get("matched_icps") or raw.get("icp") or [],
                "source_tier": getattr(lead, "source_tier", None) or raw.get("tier"),
                "source_tier_score": (
                    getattr(lead, "source_tier_score", None)
                    if getattr(lead, "source_tier_score", None) is not None
                    else raw.get("tier_score")
                ),
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect imported OSP Lead Engine payloads (read-only)."
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    init_db()
    rows = inspect_workspace(args.workspace_id, limit=args.limit)

    if not rows:
        print(f"No leads found for workspace {args.workspace_id}.")
        return 0

    print(f"\n=== Lead source payload inspection (workspace {args.workspace_id}) ===")
    for r in rows:
        print(f"\nlead_id            : {r['lead_id']}")
        print(f"  company          : {r['company']}")
        print(f"  email            : {r['email']}")
        print(f"  external_id      : {r['external_contact_id']}")
        print(f"  has_raw_payload  : {r['has_raw_payload']}")
        print(f"  has_signals      : {r['has_signals']}")
        print(f"  signal_keys      : {r['signal_keys']}")
        print(f"  matched_icps     : {r['matched_icps']}")
        print(f"  source_tier      : {r['source_tier']}")
        print(f"  source_tier_score: {r['source_tier_score']}")

    with_signals = sum(1 for r in rows if r["has_signals"])
    print(f"\n{with_signals}/{len(rows)} inspected leads have source signals. "
          "(read-only — no data modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
