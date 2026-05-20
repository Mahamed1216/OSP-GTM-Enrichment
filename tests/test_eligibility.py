"""Phase 9d — eligibility filter categorization.

Build 7 leads, one per skip code + one fully-eligible. Assert
each lands in exactly the expected bucket.
"""
import pytest

from src.config import settings
from src.db import session_scope
from src.delivery.eligibility import filter_eligible
from src.models import GeneratedContent, Lead, Score


@pytest.fixture(autouse=True)
def _pin_send_min_tier(monkeypatch):
    """Pin SEND_MIN_TIER to 'B' so the test doesn't depend on .env override."""
    monkeypatch.setattr(settings, "send_min_tier", "B")


def _lead(session, email: str, *, verified: bool = True) -> int:
    lead = Lead(
        first_name="E", last_name="L", email=email,
        email_verification_status="valid" if verified else "invalid",
    )
    session.add(lead)
    session.flush()
    return lead.id


def _score(session, lead_id: int, tier: str = "A") -> None:
    session.add(Score(
        lead_id=lead_id, score=90, tier=tier, rationale="r",
        signals_used=[], model="x",
    ))


def _content(
    session, lead_id: int, *,
    body: str = "hi",
    delivery_status: str | None = None,
    delivery_id: str | None = None,
) -> int:
    c = GeneratedContent(
        lead_id=lead_id, kind="email", subject="s", body=body,
        prompt_version="v1", model="x",
        delivery_status=delivery_status, delivery_id=delivery_id,
    )
    session.add(c)
    session.flush()
    return c.id


def test_filter_eligible_categorizes_every_skip_path():
    with session_scope() as session:
        # 1. Fully eligible: verified email, A-tier, has content, never sent.
        good = _lead(session, "good@example.com")
        _score(session, good, "A")
        _content(session, good, body="real body")

        # 2. missing_email
        missing = Lead(first_name="X", last_name="Y", email="")
        session.add(missing)
        session.flush()
        missing_id = missing.id

        # 3. email_unverified (status="invalid")
        unverified = _lead(session, "bad@example.com", verified=False)
        _score(session, unverified, "A")
        _content(session, unverified)

        # 4. below_tier (no Score row)
        no_score = _lead(session, "noscore@example.com")
        _content(session, no_score)

        # 5. below_tier (C tier, SEND_MIN_TIER defaults to B)
        c_tier = _lead(session, "ctier@example.com")
        _score(session, c_tier, "C")
        _content(session, c_tier)

        # 6. no_content (no GeneratedContent row at all)
        no_content = _lead(session, "nocontent@example.com")
        _score(session, no_content, "A")

        # 7. already_sent
        sent = _lead(session, "sent@example.com")
        _score(session, sent, "A")
        _content(session, sent, delivery_status="sent", delivery_id="abc-123")

        # 8. in_progress
        prog = _lead(session, "prog@example.com")
        _score(session, prog, "A")
        _content(session, prog, delivery_status="in_progress")

        ids = [good, missing_id, unverified, no_score, c_tier, no_content, sent, prog]

        eligible, skipped = filter_eligible(ids, session)

    assert eligible == [good], f"expected only good lead eligible, got {eligible}"
    assert skipped["missing_email"] == [missing_id]
    assert skipped["email_unverified"] == [unverified]
    assert sorted(skipped["below_tier"]) == sorted([no_score, c_tier])
    assert skipped["no_content"] == [no_content]
    assert skipped["already_sent"] == [sent]
    assert skipped["in_progress"] == [prog]


def test_filter_eligible_empty_input_returns_empty():
    with session_scope() as session:
        eligible, skipped = filter_eligible([], session)
    assert eligible == []
    assert all(v == [] for v in skipped.values())


def test_filter_eligible_accepts_apollo_verified_status():
    """Apollo-imported leads come with email_verification_status='Verified'
    (capitalized) and email_verification_provider='Apollo'. Must pass
    the email_verified gate — Apollo's real-time verification is valid."""
    from datetime import datetime

    with session_scope() as session:
        lead = Lead(
            first_name="Jenna", last_name="Quisenberry",
            email="jenna@example.com",
            email_verification_status="Verified",
            email_verification_provider="Apollo",
            email_verified_at=datetime.utcnow(),
        )
        session.add(lead)
        session.flush()
        lead_id = lead.id
        _score(session, lead_id, "A")
        _content(session, lead_id, body="real")

        eligible, skipped = filter_eligible([lead_id], session)

    assert eligible == [lead_id], f"Apollo-verified lead must pass; got skipped={skipped}"
    assert skipped["email_unverified"] == []


def test_filter_eligible_accepts_vendor_metadata_only():
    """Even if the status string is in an unrecognized format, having
    BOTH a provider name and a verified_at timestamp means SOME vendor
    has verified it — trust it."""
    from datetime import datetime

    with session_scope() as session:
        lead = Lead(
            first_name="X", last_name="Y",
            email="vendor@example.com",
            email_verification_status="deliverable",  # not "verified" or "valid"
            email_verification_provider="NeverBounce",
            email_verified_at=datetime.utcnow(),
        )
        session.add(lead)
        session.flush()
        lead_id = lead.id
        _score(session, lead_id, "A")
        _content(session, lead_id, body="real")

        eligible, skipped = filter_eligible([lead_id], session)

    assert eligible == [lead_id]


def test_filter_eligible_skips_when_no_verification_evidence():
    """No status, no provider, no timestamp → must skip as unverified."""
    with session_scope() as session:
        lead = Lead(first_name="X", last_name="Y", email="raw@example.com")
        session.add(lead)
        session.flush()
        lead_id = lead.id
        _score(session, lead_id, "A")
        _content(session, lead_id, body="real")

        eligible, skipped = filter_eligible([lead_id], session)

    assert eligible == []
    assert skipped["email_unverified"] == [lead_id]
