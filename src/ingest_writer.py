"""Dedup + per-row resilient upsert for CSV ingest.

Why this exists separately from the script: tests need to exercise the
dedup, validation, and per-row error isolation without shelling out to
the subprocess.

Per-row SAVEPOINTs (session.begin_nested) so one bad row never aborts
the batch — fixes the "0 inserted on first UNIQUE conflict" bug where
session_scope rolled back the entire transaction.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from src.ingest_aliases import CANONICAL_ALIASES, REQUIRED_FIELDS
from src.leads import reset_lead_sequence
from src.models import Lead

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Reason codes surfaced in IngestStats.skip_reasons
SKIP_MISSING_EMAIL = "missing_email"
SKIP_INVALID_EMAIL = "invalid_email_format"
SKIP_INTEGRITY = "integrity_error"
SKIP_OTHER = "other"


@dataclass
class IngestStats:
    inserted: int = 0
    updated: int = 0
    deduped_within_file: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def _bump_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


def is_valid_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(_EMAIL_RE.match(value.strip()))


def _populated_count(row: dict) -> int:
    """Count fields with a non-empty stripped value."""
    n = 0
    for v in row.values():
        if isinstance(v, str):
            if v.strip():
                n += 1
        elif v not in (None, ""):
            n += 1
    return n


def _merge_rows(winner: dict, loser: dict) -> dict:
    """Winner is the base; for any field empty in winner, fill from loser."""
    merged = dict(winner)
    for key, lval in loser.items():
        wval = merged.get(key)
        w_empty = (wval is None) or (isinstance(wval, str) and not wval.strip())
        l_has = lval not in (None, "") and not (isinstance(lval, str) and not lval.strip())
        if w_empty and l_has:
            merged[key] = lval
    return merged


def dedupe_within_file(rows: Iterable[dict]) -> tuple[list[dict], int]:
    """Group by lowercased email; pick row with most populated fields
    (last-seen wins on ties); fill empty fields from the others.

    Rows with missing/blank email are passed through untouched — they
    will be skipped by ingest_rows with reason `missing_email`.

    Returns (deduped_rows, deduped_count).
    """
    buckets: OrderedDict[str, dict] = OrderedDict()
    passthrough: list[dict] = []
    deduped_count = 0

    for row in rows:
        email_raw = row.get("email")
        email = email_raw.strip().lower() if isinstance(email_raw, str) else ""
        if not email:
            passthrough.append(row)
            continue

        normalized = dict(row)
        normalized["email"] = email

        if email not in buckets:
            buckets[email] = normalized
            continue

        # Collision — pick the winner, fill in the gaps from the loser.
        deduped_count += 1
        existing = buckets[email]
        existing_score = _populated_count(existing)
        new_score = _populated_count(normalized)
        # ties: last-seen wins (new replaces existing)
        if new_score >= existing_score:
            buckets[email] = _merge_rows(normalized, existing)
        else:
            buckets[email] = _merge_rows(existing, normalized)

    return list(buckets.values()) + passthrough, deduped_count


def _build_lead_fields(canonical_row: dict) -> dict[str, object]:
    """Pull just the canonical fields out of the row, stripping strings.
    Optional fields with empty values become None (so we don't write '' to
    columns the caller meant to leave blank)."""
    fields: dict[str, object] = {}
    for field_name in CANONICAL_ALIASES:
        value = canonical_row.get(field_name)
        if isinstance(value, str):
            value = value.strip()
        if field_name in REQUIRED_FIELDS:
            fields[field_name] = value
        else:
            fields[field_name] = value if value else None
    return fields


def _upsert_one(session: Session, row: dict) -> str:
    """Apply one row inside a SAVEPOINT. Returns 'inserted' or 'updated'.
    Raises on any DB error so caller can categorize."""
    fields = _build_lead_fields(row)
    email = fields["email"]  # already lowercased + stripped by dedup

    existing = session.query(Lead).filter_by(email=email).one_or_none()
    if existing is not None:
        # Preserve already-populated fields: only overwrite when the new
        # value is truthy. (COALESCE-style behavior, locked by spec.)
        for key, value in fields.items():
            if value:
                setattr(existing, key, value)
        session.flush()
        return "updated"
    session.add(Lead(**fields))
    session.flush()
    return "inserted"


def ingest_rows(
    canonical_rows: Iterable[dict],
    session: Session,
    *,
    pre_deduped: bool = False,
) -> IngestStats:
    """Insert/update one row at a time, isolated by SAVEPOINT.

    If `pre_deduped` is False, runs `dedupe_within_file` first and rolls
    its count into stats.deduped_within_file. Pass True if the caller
    already deduped (e.g. for unit tests that want to feed pre-shaped
    input).
    """
    stats = IngestStats()

    # When the leads table was just emptied (e.g. via bulk delete in the
    # UI), reset the id sequence so the next insert is id=1 instead of
    # picking up where Postgres left off in the 900s. No-op when leads
    # exist already.
    reset_lead_sequence(session)

    if pre_deduped:
        rows = list(canonical_rows)
    else:
        rows, dedup_count = dedupe_within_file(canonical_rows)
        stats.deduped_within_file = dedup_count

    for row in rows:
        email_raw = row.get("email")
        if not isinstance(email_raw, str) or not email_raw.strip():
            stats._bump_skip(SKIP_MISSING_EMAIL)
            continue

        email = email_raw.strip().lower()
        if not is_valid_email(email):
            stats._bump_skip(SKIP_INVALID_EMAIL)
            log.warning("ingest_skip", extra={"reason": SKIP_INVALID_EMAIL, "email": email})
            continue

        row["email"] = email  # normalize before upsert

        try:
            with session.begin_nested():  # SAVEPOINT — isolates this row
                outcome = _upsert_one(session, row)
        except IntegrityError as exc:
            stats._bump_skip(SKIP_INTEGRITY)
            log.warning(
                "ingest_skip",
                extra={"reason": SKIP_INTEGRITY, "email": email, "error": str(exc.orig)},
            )
            continue
        except SQLAlchemyError as exc:
            stats._bump_skip(SKIP_OTHER)
            log.warning(
                "ingest_skip",
                extra={"reason": SKIP_OTHER, "email": email, "error": str(exc)},
            )
            continue
        except Exception as exc:
            stats._bump_skip(SKIP_OTHER)
            log.warning(
                "ingest_skip",
                extra={"reason": SKIP_OTHER, "email": email, "error": repr(exc)},
            )
            continue

        if outcome == "inserted":
            stats.inserted += 1
        else:
            stats.updated += 1

    return stats
