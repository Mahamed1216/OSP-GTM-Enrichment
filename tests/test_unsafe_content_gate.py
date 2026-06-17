"""Production safety: internal review / placeholder text must never be sent to
prospects or pushed to Instantly via ANY path (deliver_email, overwrite, bulk).

The generator persists a real email GeneratedContent row whose body is the
"NEEDS REVIEW: ..." marker when buyer research finds nothing. These tests prove
every send path now refuses such content while valid content still sends.
"""
from __future__ import annotations

import asyncio

from src.db import session_scope
from src.delivery import instantly as inst
from src.delivery.eligibility import filter_eligible, is_unsafe_internal_content
from src.delivery.verify_email import VerifyResult
from src.models import GeneratedContent, Lead, Score, now_utc
from src.workspace import create_workspace

NEEDS_REVIEW_BODY = (
    "NEEDS REVIEW: No direct buyer account, lookalike buyer account, "
    "or trigger based buyer segment found."
)
VALID_BODY = "Hi Alex — saw your Series B; thought OSP could help your GTM motion."


def _seed_lead(workspace_id, email, *, body=VALID_BODY, subject="Quick idea", tier="A", verified=True):
    with session_scope() as s:
        lead = Lead(
            first_name="A", last_name="B", email=email, workspace_id=workspace_id,
            email_verification_status="valid" if verified else None,
            email_verification_provider="instantly" if verified else None,
            email_verified_at=now_utc() if verified else None,
        )
        s.add(lead)
        s.flush()
        s.add(Score(lead_id=lead.id, score=92, tier=tier, rationale="r",
                    signals_used=[], model="x", workspace_id=workspace_id))
        s.add(GeneratedContent(lead_id=lead.id, kind="email", subject=subject, body=body,
                               prompt_version="v1", model="m", workspace_id=workspace_id))
        s.flush()
        return lead.id


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------

def test_predicate_flags_internal_and_empty_but_not_valid():
    assert is_unsafe_internal_content("", NEEDS_REVIEW_BODY) is True
    assert is_unsafe_internal_content("NEEDS REVIEW", "anything") is True   # subject too
    assert is_unsafe_internal_content("Hi", "") is True                      # empty body
    assert is_unsafe_internal_content("Hi", "   ") is True                   # whitespace
    assert is_unsafe_internal_content("Hi", "SKIP: B2C motion.") is True
    assert is_unsafe_internal_content("Hi", "No direct buyer account here") is True
    assert is_unsafe_internal_content("Quick idea", VALID_BODY) is False


# ---------------------------------------------------------------------------
# deliver_email
# ---------------------------------------------------------------------------

def test_deliver_email_blocks_needs_review_content(monkeypatch):
    lid = _seed_lead(None, "nr@example.com", body=NEEDS_REVIEW_BODY)

    posted = {"hit": False}

    async def fake_verify(_id, **k):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(*a, **k):
        posted["hit"] = True
        return {"id": "remote-x"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    result = asyncio.run(inst.deliver_email(lid, dry_run=False))
    assert result.delivered is False
    assert result.skip_reason == "unsafe_internal_review_content"
    assert posted["hit"] is False  # never pushed to Instantly

    # The blocked lead must not be marked sent.
    with session_scope() as s:
        c = s.execute(
            select_latest_email(lid)
        ).scalar_one()
        assert c.delivery_status != "sent"
        assert c.delivered_at is None


def test_deliver_email_sends_valid_content(monkeypatch):
    lid = _seed_lead(None, "ok@example.com", body=VALID_BODY)

    async def fake_verify(_id, **k):
        return VerifyResult(status="valid", provider="instantly", raw={})

    async def fake_post(payload, api_key=None):
        return {"id": "remote-ok"}

    monkeypatch.setattr(inst, "verify_email", fake_verify)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)

    result = asyncio.run(inst.deliver_email(lid, dry_run=False))
    assert result.delivered is True
    assert result.delivery_id == "remote-ok"


# ---------------------------------------------------------------------------
# overwrite_lead_copy
# ---------------------------------------------------------------------------

def test_overwrite_blocks_needs_review_content(monkeypatch):
    ws = create_workspace("Safe One", "safe-one", "camp-1", instantly_api_key="key-1")
    lid = _seed_lead(ws["id"], "nr2@example.com", body=NEEDS_REVIEW_BODY)

    called = {"hit": False}

    async def boom(*a, **k):
        called["hit"] = True
        return []

    monkeypatch.setattr(inst, "find_leads_by_email", boom)
    monkeypatch.setattr(inst, "patch_lead", boom)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", boom)

    result = asyncio.run(inst.overwrite_lead_copy(lid, workspace_id=ws["id"]))
    assert result.status == "skipped"
    assert "unsafe_internal_review_content" in (result.detail or "")
    assert called["hit"] is False  # never touched Instantly (no overwrite of valid copy)


def test_overwrite_proceeds_for_valid_content(monkeypatch):
    ws = create_workspace("Safe Two", "safe-two", "camp-2", instantly_api_key="key-2")
    lid = _seed_lead(ws["id"], "ok2@example.com", body=VALID_BODY)

    async def fake_find(*a, **k):
        return []  # no match -> create path

    async def fake_post(*a, **k):
        return {"id": "remote-2"}

    async def fake_get(*a, **k):
        return {}

    monkeypatch.setattr(inst, "find_leads_by_email", fake_find)
    monkeypatch.setattr(inst, "_post_lead_to_campaign", fake_post)
    monkeypatch.setattr(inst, "get_lead", fake_get)

    result = asyncio.run(inst.overwrite_lead_copy(lid, workspace_id=ws["id"]))
    assert result.status == "created"


# ---------------------------------------------------------------------------
# bulk-push eligibility filter
# ---------------------------------------------------------------------------

def test_bulk_filter_excludes_unsafe_content_and_keeps_valid():
    unsafe = _seed_lead(None, "bulk-nr@example.com", body=NEEDS_REVIEW_BODY)
    valid = _seed_lead(None, "bulk-ok@example.com", body=VALID_BODY)

    with session_scope() as s:
        eligible, skipped = filter_eligible([unsafe, valid], s)

    assert valid in eligible
    assert unsafe not in eligible
    assert unsafe in skipped["unsafe_content"]


# ---------------------------------------------------------------------------
# Local import helper (kept at bottom to avoid clutter at top).
# ---------------------------------------------------------------------------

from sqlalchemy import select  # noqa: E402


def select_latest_email(lead_id: int):
    return (
        select(GeneratedContent)
        .where(GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email")
        .order_by(GeneratedContent.id.desc())
        .limit(1)
    )
