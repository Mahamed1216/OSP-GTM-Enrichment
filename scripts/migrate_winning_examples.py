"""Idempotent migration of data/winning_examples.json to the Phase 5 schema.

Adds these keys to each entry (without removing any existing ones):
  - content_type:   "email"
  - source:         "seed" if manually_flagged else "engagement_reply"
  - score:          existing reply_rate (or 0.0 if absent)
  - added_at:       existing promoted_at (or now, ISO-8601 UTC)
  - content:        {"subject": ..., "body": ...}

Entries that already carry `content_type` are left untouched. Safe to re-run.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_setup import setup_logging

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/winning_examples.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_entry(entry: dict, now_iso: str) -> tuple[dict, bool]:
    """Return (entry, changed) — additive-only fill of the new keys."""
    if "content_type" in entry:
        return entry, False

    new = dict(entry)
    new["content_type"] = "email"
    new["source"] = "seed" if entry.get("manually_flagged") else "engagement_reply"
    new["score"] = float(entry.get("reply_rate", 0.0) or 0.0)
    new["added_at"] = entry.get("promoted_at") or now_iso
    new["content"] = {
        "subject": entry.get("subject"),
        "body": entry.get("body", ""),
    }
    return new, True


def migrate(path: Path = DEFAULT_PATH) -> dict:
    summary = {"total": 0, "migrated": 0, "untouched": 0, "wrote_file": False}
    if not path.exists():
        log.info("winners_migration_no_file", extra={"path": str(path)})
        return summary

    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("winners_migration_invalid_json", extra={"error": str(exc)})
        raise

    if not isinstance(items, list):
        raise ValueError(f"{path} did not contain a JSON array")

    now_iso = _now_iso()
    changed_any = False
    out: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        new, changed = _migrate_entry(entry, now_iso)
        out.append(new)
        if changed:
            summary["migrated"] += 1
            changed_any = True
        else:
            summary["untouched"] += 1

    summary["total"] = len(items)
    if changed_any:
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        summary["wrote_file"] = True

    return summary


def main() -> int:
    setup_logging()
    summary = migrate()
    if summary["wrote_file"]:
        print(
            f"winning_examples.json migrated — total={summary['total']}, "
            f"migrated={summary['migrated']}, untouched={summary['untouched']}"
        )
    else:
        print("winning_examples.json already in new schema — nothing to do.")
    log.info("winners_migration_complete", extra=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
