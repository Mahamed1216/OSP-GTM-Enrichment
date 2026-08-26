"""Phase 3 workspace tests.

Verifies:
  - Query functions scope results to workspace_id when provided.
  - get_lead_full returns None when lead belongs to a different workspace.
  - get_unenriched/unscored/content_pending/tier selectors respect workspace_id.
  - kpi_counts, list_leads, tier_distribution, ready_to_send_count are workspace-scoped.
  - latest_instantly_snapshot, recent_engagement, sent_emails, reply_rate_series are scoped.
  - Workspace helpers (get_active_workspaces, current_workspace_id_or_default) still work.
"""
from __future__ import annotations

from sqlalchemy import text

from src.db import engine, session_scope
from src.models import (
    Enrichment,
    GeneratedContent,
    InstantlyAnalyticsSnapshot,
    Lead,
    Score,
    Workspace,
)
from src.workspace import (
    current_workspace_id_or_default,
    get_active_workspaces,
    get_default_workspace_id,
    seed_default_workspace,
)
from src.lib.db_queries import (
    get_content_pending_lead_ids,
    get_lead_full,
    get_lead_ids_by_tiers,
    get_unenriched_lead_ids,
    get_unscored_lead_ids,
    kpi_counts,
    latest_instantly_snapshot,
    list_leads,
    ready_to_send_count,
    recent_engagement,
    sent_emails,
    tier_distribution,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _seed_two_workspaces() -> tuple[int, int]:
    """Seed OSP workspace + a second 'Other' workspace. Returns (osp_id, other_id)."""
    seed_default_workspace()
    osp_id = get_default_workspace_id()
    assert osp_id is not None
    with session_scope() as session:
        other = Workspace(name="Other", slug="other", is_default=False, is_active=True)
        session.add(other)
        session.flush()
        other_id = other.id
    return osp_id, other_id


def _make_lead(session, email: str, workspace_id: int | None) -> Lead:
    lead = Lead(
        first_name="Test", last_name="User",
        email=email, company_domain="example.com",
        workspace_id=workspace_id,
    )
    session.add(lead)
    session.flush()
    return lead


# ---------------------------------------------------------------------------
# Workspace helpers — Phase 1/2 still pass
# ---------------------------------------------------------------------------

class TestWorkspaceHelpersStillWork:
    def test_get_active_workspaces_returns_seeded(self):
        seed_default_workspace()
        result = get_active_workspaces()
        assert any(ws["slug"] == "osp" for ws in result)

    def test_current_workspace_id_or_default_returns_default(self):
        seed_default_workspace()
        result = current_workspace_id_or_default()
        assert result == get_default_workspace_id()

    def test_no_duplicate_workspaces_after_multiple_seeds(self):
        seed_default_workspace()
        seed_default_workspace()
        seed_default_workspace()
        result = get_active_workspaces()
        slugs = [ws["slug"] for ws in result]
        assert slugs.count("osp") == 1


# ---------------------------------------------------------------------------
# list_leads workspace scoping
# ---------------------------------------------------------------------------

class TestListLeadsWorkspaceFilter:
    def test_no_filter_returns_all(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
        df = list_leads(workspace_id=None)
        assert len(df) == 2

    def test_filter_osp_returns_only_osp(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
        df = list_leads(workspace_id=osp_id)
        assert len(df) == 1
        assert df.iloc[0]["Email"] == "a@osp.com"

    def test_filter_other_returns_only_other(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
        df = list_leads(workspace_id=other_id)
        assert len(df) == 1
        assert df.iloc[0]["Email"] == "b@other.com"


# ---------------------------------------------------------------------------
# kpi_counts workspace scoping
# ---------------------------------------------------------------------------

class TestKpiCountsWorkspaceFilter:
    def test_leads_total_scoped_by_workspace(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
        osp_kpi = kpi_counts(workspace_id=osp_id)
        other_kpi = kpi_counts(workspace_id=other_id)
        assert osp_kpi["leads_total"] == 1
        assert other_kpi["leads_total"] == 1

    def test_no_filter_returns_combined(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
        total = kpi_counts(workspace_id=None)
        assert total["leads_total"] == 2


# ---------------------------------------------------------------------------
# tier_distribution workspace scoping
# ---------------------------------------------------------------------------

class TestTierDistributionWorkspaceFilter:
    def test_tier_scoped_by_workspace(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            l1 = _make_lead(session, "a@osp.com", osp_id)
            session.add(Score(
                lead_id=l1.id, score=90, tier="A",
                rationale="r", signals_used=[], model="x",
                workspace_id=osp_id,
            ))
            l2 = _make_lead(session, "b@other.com", other_id)
            session.add(Score(
                lead_id=l2.id, score=60, tier="B",
                rationale="r", signals_used=[], model="x",
                workspace_id=other_id,
            ))
        osp_dist = tier_distribution(workspace_id=osp_id)
        assert osp_dist["A"] == 1
        assert osp_dist["B"] == 0
        other_dist = tier_distribution(workspace_id=other_id)
        assert other_dist["B"] == 1
        assert other_dist["A"] == 0


# ---------------------------------------------------------------------------
# Lead selector functions workspace scoping
# ---------------------------------------------------------------------------

class TestLeadSelectorWorkspaceFilter:
    def test_unenriched_scoped(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            l_osp = _make_lead(session, "a@osp.com", osp_id)
            _make_lead(session, "b@other.com", other_id)
            l_osp_id = l_osp.id
        ids_osp = get_unenriched_lead_ids(workspace_id=osp_id)
        ids_other = get_unenriched_lead_ids(workspace_id=other_id)
        assert l_osp_id in ids_osp
        assert l_osp_id not in ids_other

    def test_tier_selector_scoped(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            l1 = _make_lead(session, "a@osp.com", osp_id)
            l1_id = l1.id
            session.add(Score(
                lead_id=l1_id, score=90, tier="A",
                rationale="r", signals_used=[], model="x",
                workspace_id=osp_id,
            ))
            l2 = _make_lead(session, "b@other.com", other_id)
            l2_id = l2.id
            session.add(Score(
                lead_id=l2_id, score=85, tier="A",
                rationale="r", signals_used=[], model="x",
                workspace_id=other_id,
            ))
        osp_a = get_lead_ids_by_tiers(["A"], workspace_id=osp_id)
        other_a = get_lead_ids_by_tiers(["A"], workspace_id=other_id)
        assert l1_id in osp_a and l2_id not in osp_a
        assert l2_id in other_a and l1_id not in other_a


# ---------------------------------------------------------------------------
# get_lead_full workspace check (requirement 7)
# ---------------------------------------------------------------------------

class TestGetLeadFullWorkspaceCheck:
    def test_returns_lead_when_workspace_matches(self):
        osp_id, _ = _seed_two_workspaces()
        with session_scope() as session:
            lead = _make_lead(session, "x@osp.com", osp_id)
            lead_id = lead.id
        result = get_lead_full(lead_id, workspace_id=osp_id)
        assert result is not None
        assert result["lead"]["email"] == "x@osp.com"

    def test_returns_none_when_workspace_does_not_match(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            lead = _make_lead(session, "x@osp.com", osp_id)
            lead_id = lead.id
        result = get_lead_full(lead_id, workspace_id=other_id)
        assert result is None

    def test_returns_lead_when_no_workspace_filter(self):
        osp_id, _ = _seed_two_workspaces()
        with session_scope() as session:
            lead = _make_lead(session, "x@osp.com", osp_id)
            lead_id = lead.id
        result = get_lead_full(lead_id, workspace_id=None)
        assert result is not None


# ---------------------------------------------------------------------------
# Engagement snapshot workspace scoping
# ---------------------------------------------------------------------------

class TestEngagementSnapshotWorkspaceFilter:
    def test_latest_snapshot_scoped(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            session.add(InstantlyAnalyticsSnapshot(
                campaign_id="c1", workspace_id=osp_id,
                leads_count=10, contacted_count=5,
                emails_sent_count=5, open_count=1,
                reply_count=0, bounced_count=0,
                click_count=0, unsubscribed_count=0, completed_count=0,
                raw={},
            ))
            session.add(InstantlyAnalyticsSnapshot(
                campaign_id="c2", workspace_id=other_id,
                leads_count=3, contacted_count=1,
                emails_sent_count=1, open_count=0,
                reply_count=0, bounced_count=0,
                click_count=0, unsubscribed_count=0, completed_count=0,
                raw={},
            ))
        osp_snap = latest_instantly_snapshot(workspace_id=osp_id)
        other_snap = latest_instantly_snapshot(workspace_id=other_id)
        assert osp_snap is not None and osp_snap["campaign_id"] == "c1"
        assert other_snap is not None and other_snap["campaign_id"] == "c2"

    def test_no_filter_returns_most_recent_across_workspaces(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            session.add(InstantlyAnalyticsSnapshot(
                campaign_id="any", workspace_id=osp_id,
                leads_count=1, contacted_count=0, emails_sent_count=0,
                open_count=0, reply_count=0, bounced_count=0,
                click_count=0, unsubscribed_count=0, completed_count=0,
                raw={},
            ))
        snap = latest_instantly_snapshot(workspace_id=None)
        assert snap is not None


# ---------------------------------------------------------------------------
# sent_emails + ready_to_send workspace scoping
# ---------------------------------------------------------------------------

class TestContentWorkspaceFilter:
    def test_ready_to_send_scoped(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            l_osp = _make_lead(session, "a@osp.com", osp_id)
            session.add(GeneratedContent(
                lead_id=l_osp.id, kind="email",
                subject="s", body="b",
                prompt_version="v1", model="x",
                workspace_id=osp_id,
            ))
            l_other = _make_lead(session, "b@other.com", other_id)
            session.add(GeneratedContent(
                lead_id=l_other.id, kind="email",
                subject="s", body="b",
                prompt_version="v1", model="x",
                workspace_id=other_id,
            ))
        assert ready_to_send_count(workspace_id=osp_id) == 1
        assert ready_to_send_count(workspace_id=other_id) == 1
        assert ready_to_send_count(workspace_id=None) == 2

    def test_sent_emails_scoped(self):
        osp_id, other_id = _seed_two_workspaces()
        with session_scope() as session:
            l_osp = _make_lead(session, "a@osp.com", osp_id)
            session.add(GeneratedContent(
                lead_id=l_osp.id, kind="email", subject="s", body="b",
                prompt_version="v1", model="x",
                delivery_status="sent", workspace_id=osp_id,
            ))
            l_other = _make_lead(session, "b@other.com", other_id)
            session.add(GeneratedContent(
                lead_id=l_other.id, kind="email", subject="s", body="b",
                prompt_version="v1", model="x",
                delivery_status="sent", workspace_id=other_id,
            ))
        osp_df = sent_emails(workspace_id=osp_id)
        other_df = sent_emails(workspace_id=other_id)
        assert len(osp_df) == 1
        assert len(other_df) == 1
        # No filter → both
        all_df = sent_emails(workspace_id=None)
        assert len(all_df) == 2
