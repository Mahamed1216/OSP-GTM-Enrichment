"""Phase 7: OSP Lead Engine API integration tests — 18 required + extras.

All tests use the in-memory SQLite DB from conftest.py (fresh_db autouse).
No live HTTP calls — LeadSourceClient is monkeypatched where needed.

Required test matrix (18):
  1.  Lead source settings save per workspace
  2.  OSP and Test Client can have different client slugs and API settings
  3.  Test connection calls health and client config (not contacts)
  4.  Preview contacts does not import
  5.  Import creates leads in selected workspace
  6.  Imported leads do not appear in other workspaces
  7.  Same external_contact_id can exist in different workspaces
  8.  Same external_contact_id dedupes inside same workspace
  9.  Same email dedupes inside same workspace
  10. Same email allowed in different workspaces
  11. Missing identity contact is skipped
  12. Raw payload is stored
  13. Signals are stored in raw payload
  14. Import log stores created, updated, skipped, error counts
  15. API key is masked in UI/helper output
  16. Import does not auto send
  17. Import does not push to Instantly
  18. Import does not auto run pipeline
"""
from __future__ import annotations

import httpx as _httpx
import pytest
from sqlalchemy import select

import src.lead_source.client as client_mod
from src.db import session_scope
from src.lead_source.client import EXTERNAL_SOURCE
from src.lead_source.ingest import (
    SKIP_DUPLICATE,
    SKIP_MISSING_IDENTITY,
    SKIP_NO_EMAIL_NO_MATCH,
    import_contacts,
    preview_contacts,
    start_import_log,
)
from src.lead_source.settings import (
    LeadSourceConfig,
    load_lead_source_config,
    mask_api_key,
    save_lead_source_config,
)
from src.models import Enrichment, GeneratedContent, Lead, LeadSourceImport, Score
from src.workspace import create_workspace, seed_default_workspace


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _seed_ws() -> int:
    seed_default_workspace()
    from src.workspace import get_default_workspace_id
    ws_id = get_default_workspace_id()
    assert ws_id is not None
    return ws_id


def _ws(name: str, slug: str) -> int:
    ws = create_workspace(name=name, slug=slug, instantly_campaign_id=f"camp-{slug}")
    return ws["id"]


class _FakeResp:
    """Minimal httpx.Response replacement for monkeypatching."""
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore

    def json(self):
        return self._data


_CLIENT_RESP = {
    "slug": "osp",
    "name": "OSP",
    "status": "active",
    "icps": [{"slug": "saas-cto", "enabled": True}, {"slug": "saas-vp", "enabled": True}],
    "enrich_lanes": [],
}

_HEALTH_RESP = {"status": "ok"}


def _make_contact(
    *,
    id: str = "ext-001",
    email: str = "alice@example.com",
    first_name: str = "Alice",
    last_name: str = "Smith",
    company_name: str = "Acme Corp",
    company_domain: str = "acme.com",
    linkedin_url: str = "",
    title: str = "VP of Sales",
    mobile_phone: str = "",
    signals: list | None = None,
    enrichment_status: str = "enriched",
    source: str = "test",
) -> dict:
    return {
        "id": id,
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company_name": company_name,
        "company_domain": company_domain,
        "linkedin_url": linkedin_url,
        "title": title,
        "mobile_phone": mobile_phone,
        "signals": signals if signals is not None else [],
        "enrichment_status": enrichment_status,
        "source": source,
        "created_at": "2026-01-01T00:00:00",
    }


def _log(ws_id: int, *, client_slug: str = "osp") -> int:
    return start_import_log(ws_id, client_slug, requested_limit=25)


def _import(contacts, ws_id, *, slug="osp", log_id=None):
    if log_id is None:
        log_id = _log(ws_id, client_slug=slug)
    return import_contacts(contacts, workspace_id=ws_id, client_slug=slug, import_id=log_id)


# ---------------------------------------------------------------------------
# Test 1: Lead source settings save per workspace
# ---------------------------------------------------------------------------

def test_lead_source_settings_save_per_workspace():
    ws_id = _seed_ws()

    cfg = LeadSourceConfig(
        enabled=True,
        api_base_url="https://leads.osp.tools",
        api_key="key-abc-1234",
        client_slug="osp",
        daily_fetch_limit=50,
        default_icp="saas-cto",
        default_status_filter="enriched",
        include_suppressed=True,
    )
    save_lead_source_config(cfg, ws_id)
    loaded = load_lead_source_config(ws_id)

    assert loaded.enabled is True
    assert loaded.api_base_url == "https://leads.osp.tools"
    assert loaded.client_slug == "osp"
    assert loaded.api_key == "key-abc-1234"
    assert loaded.daily_fetch_limit == 50
    assert loaded.default_icp == "saas-cto"
    assert loaded.default_status_filter == "enriched"
    assert loaded.include_suppressed is True


# ---------------------------------------------------------------------------
# Test 2: Different workspaces have independent client slugs and API settings
# ---------------------------------------------------------------------------

def test_different_workspaces_have_independent_settings():
    ws1_id = _seed_ws()
    ws2_id = _ws("Veep", "veep-t2")

    save_lead_source_config(
        LeadSourceConfig(client_slug="osp", api_key="key-osp", default_icp="saas-cto"),
        ws1_id,
    )
    save_lead_source_config(
        LeadSourceConfig(client_slug="veep", api_key="key-veep", default_icp=""),
        ws2_id,
    )

    c1 = load_lead_source_config(ws1_id)
    c2 = load_lead_source_config(ws2_id)

    assert c1.client_slug == "osp"
    assert c2.client_slug == "veep"
    assert c1.api_key != c2.api_key
    assert c1.default_icp == "saas-cto"
    assert c2.default_icp == ""


# ---------------------------------------------------------------------------
# Test 3: Test connection calls health AND client config (not contacts)
# ---------------------------------------------------------------------------

def test_test_connection_calls_health_and_client_config(monkeypatch):
    call_log: list[str] = []

    def fake_get(url, **kwargs):
        call_log.append(url)
        if "health" in url:
            return _FakeResp(200, _HEALTH_RESP)
        if "/clients/osp" in url and "contacts" not in url:
            return _FakeResp(200, _CLIENT_RESP)
        return _FakeResp(200, {})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    from src.lead_source.client import LeadSourceClient
    result = LeadSourceClient("https://leads.osp.tools", "key").test_connection("osp")

    assert result["ok"] is True
    assert result["health_status_code"] == 200
    assert result["client_status_code"] == 200
    assert result["client_name"] == "OSP"
    assert "saas-cto" in result["icp_slugs"]
    # Must call health and client config — NOT the contacts endpoint
    assert any("health" in u for u in call_log)
    assert any("/clients/osp" in u and "contacts" not in u for u in call_log)
    assert not any("contacts" in u for u in call_log), "test_connection must not call /contacts"


def test_test_connection_failure_is_safe(monkeypatch):
    monkeypatch.setattr(client_mod.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(_httpx.ConnectError("refused")))

    from src.lead_source.client import LeadSourceClient
    result = LeadSourceClient("https://bad.example.com", "k").test_connection("osp")
    assert result["ok"] is False
    assert result["error"] is not None
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Test 4: Preview contacts does not import anything
# ---------------------------------------------------------------------------

def test_preview_contacts_does_not_import(monkeypatch):
    ws_id = _seed_ws()

    contacts = [_make_contact(id="prev-1", email="preview@example.com")]

    def fake_get(url, **kwargs):
        if "contacts" in url:
            return _FakeResp(200, {"contacts": contacts, "count": 1, "limit": 5, "offset": 0})
        return _FakeResp(200, {})

    monkeypatch.setattr(client_mod.httpx, "get", fake_get)

    result = preview_contacts(ws_id, "osp", "https://leads.osp.tools", "key", limit=5)

    assert len(result) == 1
    assert result[0]["id"] == "prev-1"

    # Nothing imported — leads table must be empty
    with session_scope() as session:
        count = session.execute(select(Lead)).scalars().all()
    assert count == [], "preview_contacts must not create any Lead rows"


# ---------------------------------------------------------------------------
# Test 5: Import creates leads in selected workspace
# ---------------------------------------------------------------------------

def test_import_creates_leads_in_workspace():
    ws_id = _seed_ws()
    result = _import([_make_contact(id="t5", email="bob@example.com")], ws_id)

    assert result.created == 1
    with session_scope() as session:
        leads = session.execute(
            select(Lead).where(Lead.workspace_id == ws_id, Lead.email == "bob@example.com")
        ).scalars().all()
        assert len(leads) == 1
        ws_check = leads[0].workspace_id
    assert ws_check == ws_id


# ---------------------------------------------------------------------------
# Test 6: Imported leads do not appear in other workspaces
# ---------------------------------------------------------------------------

def test_imported_leads_scoped_to_workspace():
    ws1_id = _seed_ws()
    ws2_id = _ws("Other", "other-t6")

    _import([_make_contact(id="t6", email="carol@example.com")], ws1_id)

    with session_scope() as session:
        ws2_leads = session.execute(
            select(Lead).where(Lead.workspace_id == ws2_id)
        ).scalars().all()
    assert ws2_leads == []


# ---------------------------------------------------------------------------
# Test 7: Same external_contact_id can exist in different workspaces
# ---------------------------------------------------------------------------

def test_same_external_contact_id_allowed_in_different_workspaces():
    ws1_id = _seed_ws()
    ws2_id = _ws("WS2", "ws2-t7")

    # Same external id, different workspaces → both should be created
    r1 = _import([_make_contact(id="shared-ext", email="t7a@example.com",
                                first_name="A", last_name="One")], ws1_id)
    r2 = _import([_make_contact(id="shared-ext", email="t7b@example.com",
                                first_name="B", last_name="Two")], ws2_id)

    assert r1.created == 1
    assert r2.created == 1

    with session_scope() as session:
        rows = session.execute(
            select(Lead).where(Lead.external_contact_id == "shared-ext")
        ).scalars().all()
        ids = {r.workspace_id for r in rows}
    assert ws1_id in ids and ws2_id in ids


# ---------------------------------------------------------------------------
# Test 8: Same external_contact_id dedupes inside same workspace
# ---------------------------------------------------------------------------

def test_same_external_contact_id_deduped_in_same_workspace():
    ws_id = _seed_ws()
    contact = _make_contact(id="dup-ext", email="t8@example.com",
                            first_name="D", last_name="Up")

    r1 = _import([contact], ws_id)
    r2 = _import([contact], ws_id)   # identical contact second time

    assert r1.created == 1
    assert r2.created == 0
    assert r2.skipped >= 1

    with session_scope() as session:
        rows = session.execute(
            select(Lead).where(
                Lead.external_contact_id == "dup-ext",
                Lead.workspace_id == ws_id,
            )
        ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test 9: Same email dedupes inside same workspace
# ---------------------------------------------------------------------------

def test_same_email_deduped_in_same_workspace():
    ws_id = _seed_ws()

    # Two contacts with different external IDs but same email
    c1 = _make_contact(id="e9-a", email="dup-email@example.com",
                       first_name="X", last_name="One")
    c2 = _make_contact(id="e9-b", email="dup-email@example.com",
                       first_name="X", last_name="One")  # different ext id, same email

    r1 = _import([c1], ws_id)
    r2 = _import([c2], ws_id)

    assert r1.created == 1
    # c2 has a different external_contact_id so won't match via primary dedup,
    # but email dedup fires as fallback.
    assert r2.created == 0

    with session_scope() as session:
        rows = session.execute(
            select(Lead).where(Lead.email == "dup-email@example.com",
                               Lead.workspace_id == ws_id)
        ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test 10: Same email allowed in different workspaces
# ---------------------------------------------------------------------------

def test_same_email_allowed_in_different_workspaces():
    ws1_id = _seed_ws()
    ws2_id = _ws("WS2", "ws2-t10")

    email = "cross-ws@example.com"
    _import([_make_contact(id="t10a", email=email, first_name="P", last_name="Q")], ws1_id)
    _import([_make_contact(id="t10b", email=email, first_name="R", last_name="S")], ws2_id)

    with session_scope() as session:
        rows = session.execute(
            select(Lead).where(Lead.email == email)
        ).scalars().all()
        ws_ids = {r.workspace_id for r in rows}
    assert len(rows) == 2
    assert ws1_id in ws_ids and ws2_id in ws_ids


# ---------------------------------------------------------------------------
# Test 11: Missing identity contact is skipped
# ---------------------------------------------------------------------------

def test_missing_identity_contact_is_skipped():
    ws_id = _seed_ws()
    no_id = {
        "id": "t11",
        "email": None, "first_name": None, "last_name": None,
        "company_domain": None, "linkedin_url": None,
        "enrichment_status": "pending", "source": "test",
        "created_at": "2026-01-01T00:00:00", "signals": [],
    }
    result = _import([no_id], ws_id)

    assert result.created == 0
    assert result.skipped == 1
    assert SKIP_MISSING_IDENTITY in result.skip_reasons


# ---------------------------------------------------------------------------
# Test 12: Raw payload is stored
# ---------------------------------------------------------------------------

def test_raw_payload_is_stored():
    ws_id = _seed_ws()
    contact = _make_contact(id="t12", email="raw@example.com")
    _import([contact], ws_id)

    with session_scope() as session:
        lead = session.execute(
            select(Lead).where(Lead.email == "raw@example.com")
        ).scalar_one()
        raw = lead.lead_source_raw
        ext_id = lead.external_contact_id
        src = lead.external_source

    assert raw is not None
    assert raw.get("id") == "t12"
    assert raw.get("company_name") == contact["company_name"]
    assert ext_id == "t12"
    assert src == EXTERNAL_SOURCE


# ---------------------------------------------------------------------------
# Test 13: Signals are stored in raw payload
# ---------------------------------------------------------------------------

def test_signals_stored_in_raw_payload():
    ws_id = _seed_ws()
    contact = _make_contact(
        id="t13", email="signals@example.com",
        first_name="Sig", last_name="Test",
        signals=[{"type": "hiring", "value": "engineering", "source": "linkedin", "confidence": 0.9}],
    )
    _import([contact], ws_id)

    with session_scope() as session:
        lead = session.execute(
            select(Lead).where(Lead.email == "signals@example.com")
        ).scalar_one()
        sigs = lead.lead_source_raw.get("signals", [])

    assert len(sigs) == 1
    assert sigs[0]["type"] == "hiring"
    assert sigs[0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Test 14: Import log stores created, updated, skipped, error counts
# ---------------------------------------------------------------------------

def test_import_log_stores_counts():
    ws_id = _seed_ws()
    log_id = _log(ws_id)

    contacts = [
        _make_contact(id="t14a", email="log1@example.com",
                      first_name="L", last_name="One"),   # created
        _make_contact(id="t14b", email="log2@example.com",
                      first_name="L", last_name="Two"),   # created
        {   # missing identity → skipped
            "id": "t14c", "email": None, "first_name": None, "last_name": None,
            "company_domain": None, "linkedin_url": None,
            "enrichment_status": "pending", "source": "test",
            "created_at": "2026-01-01T00:00:00", "signals": [],
        },
    ]
    result = import_contacts(contacts, workspace_id=ws_id, client_slug="osp", import_id=log_id)

    assert result.created == 2
    assert result.skipped == 1

    with session_scope() as session:
        imp = session.get(LeadSourceImport, log_id)
        assert imp.status == "completed"
        assert imp.fetched_count == 3
        assert imp.created_count == 2
        assert imp.skipped_count == 1
        assert imp.error_count == 0
        assert imp.raw_summary is not None


# ---------------------------------------------------------------------------
# Test 15: API key is masked in helper output
# ---------------------------------------------------------------------------

def test_api_key_is_masked():
    key = "sk-super-secret-key-9876"
    masked = mask_api_key(key)
    assert "9876" in masked
    assert "secret" not in masked
    assert "super" not in masked
    assert key not in masked


def test_mask_api_key_edge_cases():
    assert mask_api_key("") == ""
    short = mask_api_key("xy")
    assert "xy" in short


# ---------------------------------------------------------------------------
# Test 16: Import does not auto send anything
# ---------------------------------------------------------------------------

def test_import_does_not_auto_send():
    ws_id = _seed_ws()
    _import([_make_contact(id="t16", email="nosend@example.com")], ws_id)

    with session_scope() as session:
        content = session.execute(select(GeneratedContent)).scalars().all()
    assert content == [], "Import must never create GeneratedContent rows"


# ---------------------------------------------------------------------------
# Test 17: Import does not push to Instantly
# ---------------------------------------------------------------------------

def test_import_does_not_push_to_instantly():
    ws_id = _seed_ws()
    _import([_make_contact(id="t17", email="nopush@example.com")], ws_id)

    with session_scope() as session:
        lead = session.execute(
            select(Lead).where(Lead.email == "nopush@example.com")
        ).scalar_one()
        lead_id = lead.id
        contents = session.execute(
            select(GeneratedContent).where(GeneratedContent.lead_id == lead_id)
        ).scalars().all()

    assert contents == [], "Import must not push to Instantly (no GeneratedContent)"


# ---------------------------------------------------------------------------
# Test 18: Import does not auto run pipeline (no Enrichment, Score, Content)
# ---------------------------------------------------------------------------

def test_import_does_not_auto_run_pipeline():
    ws_id = _seed_ws()
    _import([_make_contact(id="t18", email="nopipe@example.com",
                           first_name="No", last_name="Pipe")], ws_id)

    with session_scope() as session:
        assert session.execute(select(GeneratedContent)).scalars().all() == []
        assert session.execute(select(Enrichment)).scalars().all() == []
        assert session.execute(select(Score)).scalars().all() == []
