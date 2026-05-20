"""Ingest a CSV of leads into the DB. Idempotent on email (upserts).

Header handling: auto-detects common SDR-tool exports (Apollo, ZoomInfo,
Sales Navigator, Lusha) via src.ingest_aliases. Pass --mapping to override
auto-detect with an explicit JSON map of {canonical_field: source_column}.

Row handling: dedupes duplicate emails within the file and isolates each
upsert in a SAVEPOINT so a single bad row never aborts the batch.
"""
import argparse
import csv
import dataclasses
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db, session_scope
from src.ingest_aliases import (
    CANONICAL_ALIASES,
    auto_detect_mapping,
    validate_mapping,
)
from src.ingest_writer import ingest_rows
from src.logging_setup import setup_logging

log = logging.getLogger(__name__)


def _load_mapping_file(path: Path) -> dict[str, str | None]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"Mapping file must contain a JSON object, got {type(raw).__name__}.")
    return {field: (raw.get(field) or None) for field in CANONICAL_ALIASES}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest leads CSV into SQLite.")
    parser.add_argument("csv_path", type=Path, help="Path to leads CSV")
    parser.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="Optional path to a JSON file mapping {canonical_field: source_column}. "
        "If omitted, columns are auto-detected from the CSV header.",
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        print(f"CSV not found: {args.csv_path}", file=sys.stderr)
        return 1

    setup_logging()
    init_db()

    # utf-8-sig: tolerate the BOM Excel/Apollo exports sometimes prepend.
    with args.csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])

        if args.mapping is not None:
            if not args.mapping.exists():
                print(f"Mapping file not found: {args.mapping}", file=sys.stderr)
                return 1
            mapping = _load_mapping_file(args.mapping)
        else:
            mapping = auto_detect_mapping(fieldnames)

        ok, errors = validate_mapping(mapping)
        if not ok:
            print("Mapping validation failed:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            print(f"CSV columns detected: {fieldnames}", file=sys.stderr)
            return 2

        # canonical_field -> source_column for every mapped (non-None) field
        active = {field: src for field, src in mapping.items() if src}

        canonical_rows: list[dict] = []
        for row in reader:
            canonical = {}
            for field, src in active.items():
                raw_val = row.get(src)
                if isinstance(raw_val, str):
                    canonical[field] = raw_val.strip()
                else:
                    canonical[field] = raw_val
            canonical_rows.append(canonical)

    with session_scope() as session:
        stats = ingest_rows(canonical_rows, session)

    log.info("ingest_complete", extra=dataclasses.asdict(stats))

    # Human-readable line (CLI users, logs).
    print(
        f"Ingest complete: inserted={stats.inserted} updated={stats.updated} "
        f"deduped={stats.deduped_within_file} skipped={stats.skipped}"
    )
    if stats.skip_reasons:
        breakdown = " ".join(f"{k}={v}" for k, v in sorted(stats.skip_reasons.items()))
        print(f"Skip reasons: {breakdown}")

    # Machine-readable JSON line (UI runner parses this).
    print("Ingest result: " + json.dumps(dataclasses.asdict(stats), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
