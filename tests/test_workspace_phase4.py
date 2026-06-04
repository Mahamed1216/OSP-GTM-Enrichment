"""Phase 4 workspace tests.

Verifies:
  - Creating a workspace creates exactly one new row.
  - Duplicate slug is rejected.
  - New workspace has zero leads.
  - New workspace does not see OSP leads.
  - New workspace does not see OSP generated content.
  - New workspace does not see OSP engagement snapshots.
  - Copy prompts creates prompt rows with the new workspace_id.
  - Editing new workspace prompt does not edit OSP prompt.
  - Workspace selector can switch to the new workspace.
  - Engagement campaign resolver uses workspace campaign ID.
  - Instantly API key resolver uses workspace key when present and env fallback when blank.
"""
from __future__ import annotations

import pytest

from src.db import session_scope
from src.models import (
    GeneratedContent,
    InstantlyAnalyticsSnapshot,
    Lead,
    PromptConfig,
    Workspace,
)
from src.workspace import (
    create_workspace,
    get_active_workspaces,
    get_api_key_for_workspace,
    get_campaign_id_for_workspace,
    get_default_workspace_id,
    seed_default_workspace,
)
from app.lib.db_queries import kpi_counts, latest_instantly_snapshot, list_leads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_osp() -> int:
    seed_default_workspace()
    osp_id = get_default_workspace_id()
    assert osp_id is not None
    return osp_id


def _make_lead(session, email: str, workspace_id: int) -> Lead:
    lead = Lead(
        first_name="Test", last_name="User",
        email=email, company_domain="example.com",
        workspace_id=workspace_id,
    )
    session.add(lead)
    session.flush()
    return lead


def _seed_osp_prompt(osp_id: int, channel: str = "email", content: str = "OSP prompt text") -> None:
    with session_scope() as session:
        session.add(PromptConfig(
            channel=channel,
            content=content,
            is_active=True,
            workspace_id=osp_id,
        ))


# ---------------------------------------------------------------------------
# 1. Creating a workspace creates one new row
# ---------------------------------------------------------------------------

class TestCreateWorkspaceBasic:
    def test_creates_exactly_one_row(self):
        _seed_osp()
        before = len(get_active_workspaces())
        create_workspace(
            name="Test Client",
            slug="test-client",
            instantly_campaign_id="camp-abc",
        )
        after = len(get_active_workspaces())
        assert after == before + 1

    def test_new_workspace_has_correct_fields(self):
        _seed_osp()
        result = create_workspace(
            name="Acme Corp",
            slug="acme-corp",
            instantly_campaign_id="camp-xyz",
            notes="Test notes",
        )
        assert result["name"] == "Acme Corp"
        assert result["slug"] == "acme-corp"
        assert result["instantly_campaign_id"] == "camp-xyz"
        assert result["notes"] == "Test notes"
        assert result["is_default"] is False
        assert result["is_active"] is True

    def test_new_workspace_is_not_default(self):
        _seed_osp()
        result = create_workspace(
            name="Second WS",
            slug="second-ws",
            instantly_campaign_id="camp-2",
        )
        assert result["is_default"] is False

    def test_workspace_appears_in_active_list(self):
        _seed_osp()
        create_workspace(
            name="New Client",
            slug="new-client",
            instantly_campaign_id="camp-nc",
        )
        slugs = [ws["slug"] for ws in get_active_workspaces()]
        assert "new-client" in slugs


# ---------------------------------------------------------------------------
# 2. Duplicate slug is rejected
# ---------------------------------------------------------------------------

class TestDuplicateSlugRejected:
    def test_duplicate_slug_raises_value_error(self):
        _seed_osp()
        create_workspace(
            name="First",
            slug="my-slug",
            instantly_campaign_id="camp-1",
        )
        with pytest.raises(ValueError, match="slug"):
            create_workspace(
                name="Second",
                slug="my-slug",
                instantly_campaign_id="camp-2",
            )

    def test_duplicate_name_raises_value_error(self):
        _seed_osp()
        create_workspace(
            name="Duplicate Name",
            slug="dup-name-1",
            instantly_campaign_id="camp-1",
        )
        with pytest.raises(ValueError, match="already exists"):
            create_workspace(
                name="Duplicate Name",
                slug="dup-name-2",
                instantly_campaign_id="camp-2",
            )

    def test_bad_slug_raises_value_error(self):
        _seed_osp()
        for bad in ("My Slug", "UPPER", "-starts-with-dash", "ends-with-dash-"):
            with pytest.raises(ValueError):
                create_workspace(
                    name=f"WS {bad}",
                    slug=bad,
                    instantly_campaign_id="camp-x",
                )

    def test_empty_name_raises_value_error(self):
        _seed_osp()
        with pytest.raises(ValueError, match="name"):
            create_workspace(name="", slug="ok-slug", instantly_campaign_id="c")

    def test_missing_campaign_id_raises_value_error(self):
        _seed_osp()
        with pytest.raises(ValueError, match="campaign"):
            create_workspace(name="WS", slug="ws", instantly_campaign_id="")


# ---------------------------------------------------------------------------
# 3. New workspace has zero leads
# ---------------------------------------------------------------------------

class TestNewWorkspaceZeroLeads:
    def test_new_workspace_has_no_leads(self):
        _seed_osp()
        new_ws = create_workspace(
            name="Empty WS",
            slug="empty-ws",
            instantly_campaign_id="camp-e",
        )
        df = list_leads(workspace_id=new_ws["id"])
        assert df.empty

    def test_new_workspace_kpi_leads_total_is_zero(self):
        _seed_osp()
        new_ws = create_workspace(
            name="Zero WS",
            slug="zero-ws",
            instantly_campaign_id="camp-z",
        )
        kpis = kpi_counts(workspace_id=new_ws["id"])
        assert kpis["leads_total"] == 0


# ---------------------------------------------------------------------------
# 4. New workspace does not see OSP leads
# ---------------------------------------------------------------------------

class TestNewWorkspaceDoesNotSeeOspLeads:
    def test_osp_leads_invisible_to_new_workspace(self):
        osp_id = _seed_osp()
        with session_scope() as session:
            _make_lead(session, "osp@example.com", osp_id)
        new_ws = create_workspace(
            name="Isolated WS",
            slug="isolated-ws",
            instantly_campaign_id="camp-i",
        )
        df = list_leads(workspace_id=new_ws["id"])
        assert df.empty

    def test_osp_kpi_unaffected_by_new_workspace(self):
        osp_id = _seed_osp()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
        create_workspace(
            name="Other WS",
            slug="other-ws",
            instantly_campaign_id="camp-o",
        )
        osp_kpis = kpi_counts(workspace_id=osp_id)
        assert osp_kpis["leads_total"] == 1


# ---------------------------------------------------------------------------
# 5. New workspace does not see OSP generated content
# ---------------------------------------------------------------------------

class TestNewWorkspaceDoesNotSeeOspContent:
    def test_osp_content_invisible_to_new_workspace(self):
        from app.lib.db_queries import ready_to_send_count
        osp_id = _seed_osp()
        with session_scope() as session:
            lead = _make_lead(session, "b@osp.com", osp_id)
            session.add(GeneratedContent(
                lead_id=lead.id, kind="email",
                subject="s", body="b",
                prompt_version="v1", model="x",
                workspace_id=osp_id,
            ))
        new_ws = create_workspace(
            name="Content WS",
            slug="content-ws",
            instantly_campaign_id="camp-c",
        )
        assert ready_to_send_count(workspace_id=new_ws["id"]) == 0


# ---------------------------------------------------------------------------
# 6. New workspace does not see OSP engagement snapshots
# ---------------------------------------------------------------------------

class TestNewWorkspaceDoesNotSeeOspSnapshots:
    def test_osp_snapshot_invisible_to_new_workspace(self):
        osp_id = _seed_osp()
        with session_scope() as session:
            session.add(InstantlyAnalyticsSnapshot(
                campaign_id="c-osp",
                workspace_id=osp_id,
                leads_count=10, contacted_count=5,
                emails_sent_count=5, open_count=1,
                reply_count=0, bounced_count=0,
                click_count=0, unsubscribed_count=0, completed_count=0,
                raw={},
            ))
        new_ws = create_workspace(
            name="Snap WS",
            slug="snap-ws",
            instantly_campaign_id="camp-s",
        )
        snap = latest_instantly_snapshot(workspace_id=new_ws["id"])
        assert snap is None


# ---------------------------------------------------------------------------
# 7. Copy prompts creates prompt rows with the new workspace_id
# ---------------------------------------------------------------------------

class TestCopyPromptsCreatesRows:
    def test_copy_prompts_creates_rows_for_new_workspace(self):
        osp_id = _seed_osp()
        _seed_osp_prompt(osp_id, "email", "OSP email prompt")
        _seed_osp_prompt(osp_id, "linkedin_msg", "OSP linkedin prompt")

        new_ws = create_workspace(
            name="Prompt WS",
            slug="prompt-ws",
            instantly_campaign_id="camp-p",
            copy_prompts_from_workspace_id=osp_id,
        )
        new_id = new_ws["id"]

        with session_scope() as session:
            rows = session.execute(
                __import__("sqlalchemy", fromlist=["select"]).select(PromptConfig)
                .where(PromptConfig.workspace_id == new_id)
            ).scalars().all()
            assert len(rows) >= 2
            channels = {r.channel for r in rows}
            assert "email" in channels

    def test_copy_prompts_content_matches_source(self):
        osp_id = _seed_osp()
        _seed_osp_prompt(osp_id, "email", "My custom OSP email")

        new_ws = create_workspace(
            name="Content Match WS",
            slug="content-match-ws",
            instantly_campaign_id="camp-cm",
            copy_prompts_from_workspace_id=osp_id,
        )
        new_id = new_ws["id"]

        with session_scope() as session:
            from sqlalchemy import select
            row = session.execute(
                select(PromptConfig)
                .where(PromptConfig.workspace_id == new_id)
                .where(PromptConfig.channel == "email")
            ).scalar_one_or_none()
            assert row is not None
            assert row.content == "My custom OSP email"


# ---------------------------------------------------------------------------
# 8. Editing new workspace prompt does not edit OSP prompt
# ---------------------------------------------------------------------------

class TestPromptIsolation:
    def test_editing_new_ws_prompt_does_not_affect_osp(self):
        from sqlalchemy import select
        from src.prompts.loader import save_overlay
        osp_id = _seed_osp()
        _seed_osp_prompt(osp_id, "email", "OSP original")

        new_ws = create_workspace(
            name="Edit Test WS",
            slug="edit-test-ws",
            instantly_campaign_id="camp-et",
            copy_prompts_from_workspace_id=osp_id,
        )
        new_id = new_ws["id"]

        save_overlay("email", "New workspace edited prompt", workspace_id=new_id)

        with session_scope() as session:
            osp_row = session.execute(
                select(PromptConfig)
                .where(PromptConfig.workspace_id == osp_id)
                .where(PromptConfig.channel == "email")
            ).scalar_one_or_none()
            new_row = session.execute(
                select(PromptConfig)
                .where(PromptConfig.workspace_id == new_id)
                .where(PromptConfig.channel == "email")
            ).scalar_one_or_none()

            assert osp_row is not None
            assert osp_row.content == "OSP original"
            assert new_row is not None
            assert new_row.content.strip() == "New workspace edited prompt"


# ---------------------------------------------------------------------------
# 9. Workspace selector can switch to the new workspace
# ---------------------------------------------------------------------------

class TestWorkspaceSelector:
    def test_new_workspace_appears_in_active_list(self):
        _seed_osp()
        new_ws = create_workspace(
            name="Selector WS",
            slug="selector-ws",
            instantly_campaign_id="camp-sel",
        )
        active = get_active_workspaces()
        ids = [ws["id"] for ws in active]
        assert new_ws["id"] in ids

    def test_both_workspaces_in_active_list(self):
        osp_id = _seed_osp()
        new_ws = create_workspace(
            name="Both WS",
            slug="both-ws",
            instantly_campaign_id="camp-both",
        )
        active = get_active_workspaces()
        ids = [ws["id"] for ws in active]
        assert osp_id in ids
        assert new_ws["id"] in ids


# ---------------------------------------------------------------------------
# 10. Engagement campaign resolver uses workspace campaign ID
# ---------------------------------------------------------------------------

class TestCampaignIdResolver:
    def test_workspace_campaign_id_used_over_env(self, monkeypatch):
        _seed_osp()
        new_ws = create_workspace(
            name="Campaign WS",
            slug="campaign-ws",
            instantly_campaign_id="ws-specific-campaign-id",
        )
        campaign_id = get_campaign_id_for_workspace(new_ws["id"])
        assert campaign_id == "ws-specific-campaign-id"

    def test_env_fallback_when_workspace_has_no_campaign_id(self, monkeypatch):
        # Create workspace with a campaign_id, then test default workspace
        # falls back to env if its column is empty.
        from src.workspace import get_default_workspace
        _seed_osp()
        default_ws = get_default_workspace()
        if default_ws and not default_ws.get("instantly_campaign_id"):
            monkeypatch.setattr(
                "src.workspace.settings.instantly_campaign_id", "env-campaign-id"
            )
            resolved = get_campaign_id_for_workspace(default_ws["id"])
            assert resolved == "env-campaign-id"


# ---------------------------------------------------------------------------
# 11. Instantly API key resolver
# ---------------------------------------------------------------------------

class TestApiKeyResolver:
    def test_workspace_api_key_used_when_present(self):
        _seed_osp()
        new_ws = create_workspace(
            name="API Key WS",
            slug="api-key-ws",
            instantly_campaign_id="camp-ak",
            instantly_api_key="workspace-specific-key",
        )
        key = get_api_key_for_workspace(new_ws["id"])
        assert key == "workspace-specific-key"

    def test_env_fallback_when_workspace_key_blank(self, monkeypatch):
        _seed_osp()
        new_ws = create_workspace(
            name="No Key WS",
            slug="no-key-ws",
            instantly_campaign_id="camp-nk",
            instantly_api_key=None,
        )
        monkeypatch.setattr(
            "src.workspace.settings.instantly_api_key", "env-api-key"
        )
        key = get_api_key_for_workspace(new_ws["id"])
        assert key == "env-api-key"

    def test_returns_none_when_neither_workspace_nor_env(self, monkeypatch):
        _seed_osp()
        new_ws = create_workspace(
            name="No Key Either WS",
            slug="no-key-either-ws",
            instantly_campaign_id="camp-nke",
        )
        monkeypatch.setattr("src.workspace.settings.instantly_api_key", "")
        key = get_api_key_for_workspace(new_ws["id"])
        assert key is None
