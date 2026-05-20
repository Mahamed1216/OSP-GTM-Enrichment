"""Idempotent backfill of ``is_active`` on winners + negatives entries.

Phase 7c introduces an ``is_active`` boolean on each entry in
``data/winning_examples.json`` and ``data/negative_examples.json`` so the
SDR can demote bad picks from the Engagement page without deleting them.
New entries (post-Phase-7c) get ``is_active: true`` stamped by the
``_make_*`` helpers in ``src/feedback/learning.py``. This script backfills
existing pre-Phase-7c entries.

Strictly additive: an entry that already has ``is_active`` (whatever its
value) is left untouched. Safe to re-run.

Run:  python scripts/migrate_is_active.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_setup import setup_logging

log = logging.getLogger(__name__)

WINNERS_PATH = Path("data/winning_examples.json")
NEGATIVES_PATH = Path("data/negative_examples.json")


def _migrate_one(path: Path) -> dict:
    """Return a per-file summary dict.

    Keys: total, migrated, untouched, wrote_file.
    """
    summary = {"path": str(path), "total": 0, "migrated": 0, "untouched": 0, "wrote_file": False}
    if not path.exists():
        log.info("is_active_migration_no_file", extra={"path": str(path)})
        return summary

    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("is_active_migration_invalid_json", extra={"path": str(path), "error": str(exc)})
        raise

    if not isinstance(items, list):
        raise ValueError(f"{path} did not contain a JSON array")

    changed_any = False
    out: list = []
    for entry in items:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        if "is_active" in entry:
            out.append(entry)
            summary["untouched"] += 1
            continue
        new = dict(entry)
        new["is_active"] = True
        out.append(new)
        summary["migrated"] += 1
        changed_any = True

    summary["total"] = len(items)
    if changed_any:
        # Same JSON shape and indentation as winners.py atomic-write path.
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary["wrote_file"] = True

    return summary


def main() -> int:
    setup_logging()
    overall = {"files": [], "total_migrated": 0}
    for path in (WINNERS_PATH, NEGATIVES_PATH):
        result = _migrate_one(path)
        overall["files"].append(result)
        overall["total_migrated"] += result["migrated"]
        print(
            f"{path}: total={result['total']} "
            f"migrated={result['migrated']} untouched={result['untouched']} "
            f"wrote_file={result['wrote_file']}"
        )
    print(f"done. {overall['total_migrated']} entries gained is_active=True.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
