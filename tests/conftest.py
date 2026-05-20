"""Shared test fixtures: in-memory SQLite, fresh schema per test."""
import os

# Set before importing src.* so settings load with this URL
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Patch the db module's engine + SessionLocal to use a single in-memory DB
# shared across the test process (StaticPool keeps the same connection alive)
from sqlalchemy.pool import StaticPool

from src import db as _db

_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
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
