"""Dedup + per-row resilient upsert behavior for CSV ingest.

Covers the spec's required-behavior matrix:
- duplicate email within same file is merged (not double-inserted)
- existing DB email gets updated, not re-inserted
- missing/blank email skipped with reason
- malformed email skipped with reason
- a single bad row does not abort the rest of the batch
- update never overwrites a populated DB field with an empty CSV value
- dedup prefers the row with more populated fields, then fills gaps
"""
from sqlalchemy import select

from src.db import session_scope
from src.ingest_writer import (
    SKIP_INVALID_EMAIL,
    SKIP_MISSING_EMAIL,
    dedupe_within_file,
    ingest_rows,
)
from src.models import Lead


def _row(email, **extras) -> dict:
    base = {
        "first_name": "Alex",
        "last_name": "Stone",
        "email": email,
        "title": "VP Sales",
        "company": "Acme Inc",
        "company_domain": "acme.com",
        "industry": "B2B SaaS",
        "linkedin_url": "https://linkedin.com/in/alexstone",
        "company_linkedin_url": "https://linkedin.com/company/acme",
    }
    base.update(extras)
    return base


# --------------------------------------------------------------------------
# 1. Duplicate email within file → 1 inserted, 1 deduped_within_file
# --------------------------------------------------------------------------

def test_dup_email_in_csv_deduped():
    rows = [_row("dup@example.com"), _row("dup@example.com", first_name="Other")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 1
    assert stats.updated == 0
    assert stats.deduped_within_file == 1
    assert stats.skipped == 0

    with session_scope() as session:
        leads = session.execute(select(Lead).where(Lead.email == "dup@example.com")).scalars().all()
        assert len(leads) == 1


# --------------------------------------------------------------------------
# 2. CSV email already in DB → 0 inserted, 1 updated
# --------------------------------------------------------------------------

def test_existing_email_updates():
    with session_scope() as session:
        session.add(Lead(first_name="Old", last_name="Name", email="seed@example.com"))

    rows = [_row("seed@example.com", first_name="New")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 0
    assert stats.updated == 1
    assert stats.skipped == 0

    with session_scope() as session:
        lead = session.execute(select(Lead).where(Lead.email == "seed@example.com")).scalar_one()
        assert lead.first_name == "New"  # truthy CSV overrode
        assert lead.title == "VP Sales"  # filled in from CSV


# --------------------------------------------------------------------------
# 3. Empty email → 0 inserted, 1 skipped (reason=missing_email)
# --------------------------------------------------------------------------

def test_missing_email_skipped():
    rows = [_row("", first_name="NoEmail")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 0
    assert stats.skipped == 1
    assert stats.skip_reasons.get(SKIP_MISSING_EMAIL) == 1


# --------------------------------------------------------------------------
# 4. Invalid email format → 0 inserted, 1 skipped (reason=invalid_email_format)
# --------------------------------------------------------------------------

def test_invalid_email_format_skipped():
    rows = [_row("not-an-email")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 0
    assert stats.skipped == 1
    assert stats.skip_reasons.get(SKIP_INVALID_EMAIL) == 1


# --------------------------------------------------------------------------
# 5. 100 rows, 1 invalid → 99 inserted, 1 skipped (batch resilience)
# --------------------------------------------------------------------------

def test_one_bad_row_does_not_abort_batch():
    rows = [_row(f"user{i}@example.com") for i in range(99)]
    rows.append(_row("garbage-not-an-email"))

    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 99, f"got stats={stats}"
    assert stats.skipped == 1
    assert stats.skip_reasons.get(SKIP_INVALID_EMAIL) == 1

    with session_scope() as session:
        count = session.execute(select(Lead)).scalars().all()
        assert len(count) == 99


# --------------------------------------------------------------------------
# 6. Update preserves existing populated field when CSV field is empty
# --------------------------------------------------------------------------

def test_update_preserves_existing_populated_field_when_csv_empty():
    with session_scope() as session:
        session.add(Lead(
            first_name="Pre", last_name="Seed", email="keep@example.com",
            title="VP Sales", company="OriginalCo",
        ))

    # CSV row has the same email but blank title and blank company
    rows = [_row("keep@example.com", first_name="Updated", title="", company="")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.updated == 1

    with session_scope() as session:
        lead = session.execute(select(Lead).where(Lead.email == "keep@example.com")).scalar_one()
        # truthy field overwritten
        assert lead.first_name == "Updated"
        # empty fields in CSV did NOT clobber existing values
        assert lead.title == "VP Sales"
        assert lead.company == "OriginalCo"


# --------------------------------------------------------------------------
# 7. dedup: prefer row with more populated fields, fill gaps from loser
# --------------------------------------------------------------------------

def test_dedup_prefers_row_with_more_fields():
    sparse = {
        "first_name": "A",
        "last_name": "Sparse",
        "email": "merge@example.com",
        "title": "",
        "company": "",
        "company_domain": "",
        "industry": "Loser-Industry",  # winner has none here — should survive
        "linkedin_url": "",
        "company_linkedin_url": "",
    }
    rich = {
        "first_name": "B",
        "last_name": "Rich",
        "email": "merge@example.com",
        "title": "VP",
        "company": "RichCo",
        "company_domain": "rich.example",
        "industry": "",  # empty — should be filled from sparse
        "linkedin_url": "https://linkedin.com/in/rich",
        "company_linkedin_url": "https://linkedin.com/company/richco",
    }

    deduped, count = dedupe_within_file([sparse, rich])
    assert count == 1
    assert len(deduped) == 1

    merged = deduped[0]
    # winner's values dominate
    assert merged["first_name"] == "B"
    assert merged["title"] == "VP"
    assert merged["company"] == "RichCo"
    # winner's empty fields filled from loser
    assert merged["industry"] == "Loser-Industry"


# --------------------------------------------------------------------------
# Bonus regression: case-insensitive dedup on email
# --------------------------------------------------------------------------

def test_dedup_is_case_insensitive_on_email():
    rows = [_row("Mixed@Example.COM"), _row("mixed@example.com", first_name="lowercased")]
    with session_scope() as session:
        stats = ingest_rows(rows, session)

    assert stats.inserted == 1
    assert stats.deduped_within_file == 1

    with session_scope() as session:
        leads = session.execute(select(Lead)).scalars().all()
        assert len(leads) == 1
        assert leads[0].email == "mixed@example.com"
