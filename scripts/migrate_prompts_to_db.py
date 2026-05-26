"""One-shot migration: copy data/prompts_config.json into the DB.

Why: previous storage was a local JSON file. Streamlit Cloud's local disk
is ephemeral, so every deploy wiped the user's edits. Storage moved to the
`prompt_configs` table; this script seeds that table from the JSON file
the user had locally before deploying.

Idempotent. Safe to re-run. Per channel:
  - If the JSON has a value AND the DB does not → insert.
  - If the JSON has a value AND the DB already has one → leave DB alone
    (the DB is authoritative once seeded; re-run won't clobber edits).
  - If the JSON has no value → skip.

Usage:
    python scripts/migrate_prompts_to_db.py
    python scripts/migrate_prompts_to_db.py --overwrite   # force re-import

Run once locally, commit + push. After deploy, the table is created by
`init_db()` on app startup (Streamlit Cloud) but the seed data needs this
script — you can also re-run it against the prod DB by exporting the same
DATABASE_URL the deployed app uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from src.db import init_db, session_scope
from src.models import PromptConfig

JSON_PATH = Path("data/prompts_config.json")
_VALID_CHANNELS = {"email", "linkedin_msg", "call_script"}


def _load_json() -> dict[str, str]:
    if not JSON_PATH.exists():
        print(f"No file at {JSON_PATH} — nothing to migrate.")
        return {}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗ Could not read {JSON_PATH}: {exc}")
        return {}
    if not isinstance(data, dict):
        print(f"✗ {JSON_PATH} is not a JSON object — got {type(data).__name__}")
        return {}
    return {k: v for k, v in data.items() if k in _VALID_CHANNELS and isinstance(v, str) and v.strip()}


def main(overwrite: bool) -> int:
    init_db()

    payload = _load_json()
    if not payload:
        return 0

    inserted: list[str] = []
    skipped: list[str] = []
    overwrote: list[str] = []

    with session_scope() as session:
        existing_rows = session.execute(select(PromptConfig)).scalars().all()
        existing = {row.channel: row for row in existing_rows}
        now = datetime.utcnow()

        for channel, content in payload.items():
            row = existing.get(channel)
            if row is None:
                session.add(PromptConfig(channel=channel, content=content, updated_at=now))
                inserted.append(channel)
            elif overwrite:
                row.content = content
                row.updated_at = now
                overwrote.append(channel)
            else:
                skipped.append(channel)

    print(f"Read {len(payload)} channels from {JSON_PATH}")
    if inserted:
        print(f"  inserted: {', '.join(inserted)}")
    if overwrote:
        print(f"  overwrote (--overwrite): {', '.join(overwrote)}")
    if skipped:
        print(
            f"  skipped (DB already has row, use --overwrite to force): "
            f"{', '.join(skipped)}"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace DB rows with JSON values even if the DB already has them.",
    )
    args = parser.parse_args()
    raise SystemExit(main(overwrite=args.overwrite))
