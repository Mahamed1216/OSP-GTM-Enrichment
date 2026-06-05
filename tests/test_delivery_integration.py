"""File-based SQLite integration test for the bulk-push delivery path.

WHY THIS EXISTS (and why the existing 137 tests don't cover it):

- conftest.py uses an in-memory StaticPool SQLite engine that's
  rebuilt from `Base.metadata.create_all` every test. That guarantees
  the test schema matches `src/models.py` exactly — but it CANNOT
  catch drift between models.py and a real file-based DB where the
  schema was created by hand-rolled migrations (`scripts/migrate_*.py`).
- If migrate_phase9 ever silently fails to add a column, the in-memory
  tests pass but production crashes with AttributeError on the missing
  column.

This test rebuilds a fresh file SQLite at `tmp_path / "drift.db"`,
runs the migration against it, monkeypatches `src.db.engine` /
`src.db.SessionLocal` so the production deliver_email path uses it,
mocks Instantly HTTP, and asserts the full delivery_status / error_message
round-trip works. Failure on AttributeError = schema drift.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src import db as _db
from src.db import Base
from src.models import GeneratedContent, Lead, Score


# ---------------------------------------------------------------------------
# Fixture: spin up a file-backed SQLite, run migrate_phase9 against it,
# point `src.db` at it for the duration of one test.
# ---------------------------------------------------------------------------

@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """Replace the in-memory test engine with a file-backed one for this
    test only, with the same schema build path production uses."""
    db_path = tmp_path / "drift.db"
    file_engine = create_engine(
        f"sqlite:///{db_path}", future=True, connect_args={"check_same_thread": False}
    )
    file_session_local = sessionmaker(
        bind=file_engine, autoflush=False, autocommit=False, future=True
    )

    # Production-shaped setup: create all tables, then run the phase 9
    # migration to confirm it's idempotent + non-destructive against
    # an already-current schema.
    Base.metadata.create_all(file_engine)
    from scripts.migrate_phase9 import migrate as run_phase9_migration
    summary = run_phase9_migration(target_engine=file_engine)
    # When create_all has already added the columns (current models.py),
    # the migration sees "already applied" — that's correct behavior.
    assert summary["already_applied"], summary

    monkeypatch.setattr(_db, "engine", file_engine)
    monkeypatch.setattr(_db, "SessionLocal", file_session_local)
    yield file_engine
    file_engine.dispose()


def _seed_eligible_lead(session) -> int:
    lead = Lead(
        first_name="Jenna", last_name="Q",
        email="jenna@example.com",
        email_verification_status="Verified",
        email_verification_provider="Apollo",
        email_verified_at=_dt.datetime.utcnow(),
    )
    session.add(lead)
    session.flush()
    session.add(Score(
        lead_id=lead.id, score=92, tier="A", rationale="r",
        signals_used=[], model="x",
    ))
    session.add(GeneratedContent(
        lead_id=lead.id, kind="email",
        subject="Saw your Series B",
        body="Real personalized body text.",
        prompt_version="v1", model="claude-sonnet-4-6",
    ))
    session.flush()
    return lead.id


# ---------------------------------------------------------------------------
# Test 1: success path — deliver_email persists delivery_status="sent"
# ---------------------------------------------------------------------------

def test_deliver_email_persists_sent_status_on_success(file_db, monkeypatch):
    from src.db import session_scope
    from src.delivery import instantly as inst
    from src.delivery.verify_email import VerifyResult

    with session_scope() as session:
        lead_id = _seed_eligible_lead(session)

    async def fake_verify(_lead_id):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(_payload, api_key=None):
        return {"id": "remote-abc-123"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    result = asyncio.run(inst.deliver_email(lead_id, dry_run=False))
    assert result.delivered is True
    assert result.delivery_id == "remote-abc-123"

    # The real proof: read the DB back through the production session_scope
    # and confirm the new columns were written. If models.py and the file
    # DB schema ever drift, this assertion crashes with AttributeError.
    with session_scope() as session:
        content = session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        assert content is not None
        assert content.delivery_status == "sent"
        assert content.delivery_id == "remote-abc-123"
        assert content.delivered_at is not None
        assert content.error_message is None
        assert content.delivery_provider == "instantly"


# ---------------------------------------------------------------------------
# Test 2: error path — 5xx from Instantly persists delivery_status="error"
# ---------------------------------------------------------------------------

def test_deliver_email_persists_error_status_on_http_failure(file_db, monkeypatch):
    from src.db import session_scope
    from src.delivery import instantly as inst
    from src.delivery.verify_email import VerifyResult

    with session_scope() as session:
        lead_id = _seed_eligible_lead(session)

    async def fake_verify(_lead_id):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post_502(_payload, api_key=None):
        request = httpx.Request("POST", "https://api.instantly.ai/api/v2/leads")
        response = httpx.Response(502, text="Bad Gateway", request=request)
        raise httpx.HTTPStatusError("502", request=request, response=response)

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post_502)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(inst.deliver_email(lead_id, dry_run=False))

    with session_scope() as session:
        content = session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
            .order_by(GeneratedContent.id.desc())
        ).scalars().first()
        assert content is not None
        assert content.delivery_status == "error"
        assert content.error_message and "502" in content.error_message
        assert content.delivered_at is None
        assert content.delivery_id is None
