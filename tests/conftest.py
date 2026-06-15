"""Shared test fixtures: file-based SQLite, fresh schema per test.

Why a file (not ``:memory:`` + StaticPool):

An in-memory StaticPool engine shares ONE physical DBAPI connection across
the whole process. That single-connection model is unfaithful to every real
deployment (file-SQLite and Postgres both use a multi-connection QueuePool),
and it breaks any code path that briefly checks out its own connection while
a session is mid-transaction. Concretely, the ``Lead.workspace_id`` column
default (``_default_workspace_id`` in ``src/models.py``) opens a short-lived
AUTOCOMMIT connection during ``flush()``; under StaticPool that is the *same*
connection the session is using, so it destroyed the active SAVEPOINT and
made per-row CSV ingest skip every row (``no such savepoint``). A file DB
hands out separate connections, so the checkout no longer collides — exactly
matching production behavior.
"""
import atexit
import os
import tempfile
from pathlib import Path

# Create the temp DB path BEFORE importing src.* so settings bind to it.
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".sqlite", prefix="sdr_test_")
os.close(_TEST_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import db as _db

# File-based engine → default (Queue) pool, separate connections per checkout,
# just like production. check_same_thread=False keeps async tests happy.
_test_engine = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"check_same_thread": False},
    future=True,
)
_db.engine = _test_engine
_db.SessionLocal = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False, future=True)

from src.db import Base, session_scope  # noqa: E402  must come after patch
from src.models import Lead  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(_test_engine)
    Base.metadata.create_all(_test_engine)
    yield
    Base.metadata.drop_all(_test_engine)


@atexit.register
def _cleanup_test_db() -> None:
    try:
        _test_engine.dispose()
    except Exception:
        pass
    try:
        Path(_TEST_DB_PATH).unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def sample_lead_id() -> int:
    with session_scope() as session:
        lead = Lead(
            first_name="Test",
            last_name="User",
            email="test@example.com",
            title="VP of Sales",
            company="ExampleCo",
            company_domain="example.com",
            industry="B2B SaaS",
            linkedin_url="https://www.linkedin.com/in/test-user",
            company_linkedin_url="https://www.linkedin.com/company/example",
        )
        session.add(lead)
        session.flush()
        return lead.id
