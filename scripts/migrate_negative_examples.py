"""Idempotent: ensure data/negative_examples.json exists as a JSON array."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_PATH = Path("data/negative_examples.json")


def ensure(path: Path = DEFAULT_PATH) -> bool:
    """Create the file with [] if missing. Return True if it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\n", encoding="utf-8")
    return True


def main() -> int:
    created = ensure()
    if created:
        print(f"Created {DEFAULT_PATH} (empty array).")
    else:
        print(f"{DEFAULT_PATH} already exists — nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
