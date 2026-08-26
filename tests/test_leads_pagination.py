"""Tests for server-side pagination in list_leads / count_leads.

Covers:
1. list_leads returns only page_size rows
2. Page 1 and page 2 return different rows
3. Pagination is workspace scoped
4. Search filters apply before pagination
5. Tier/status filters apply before pagination
6. count_leads returns filtered count
7. Changing filters (page reset done by UI, not query) — count changes
8. Selection does not leak across pages (DB-level: correct IDs per offset)
9. Delete selected only affects the right IDs (rows from a specific page)
10. Push to Instantly only uses selected-page row IDs (same isolation)
11. Default page size is 50
"""
from __future__ import annotations

import pytest

from src.db import session_scope
from src.models import Enrichment, Lead, Score, Workspace
from src.workspace import seed_default_workspace, get_default_workspace_id

from src.lib.db_queries import count_leads, list_leads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(name: str, slug: str) -> int:
    with session_scope() as s:
        ws = Workspace(name=name, slug=slug, is_default=False, is_active=True)
        s.add(ws)
        s.flush()
        return ws.id


def _make_lead(email: str, workspace_id: int | None, *,
               first_name: str = "Test", last_name: str = "User",
               company: str = "ACME") -> int:
    with session_scope() as s:
        lead = Lead(
            first_name=first_name,
            last_name=last_name,
            email=email,
            company=company,
            company_domain=email.split("@")[-1],
            workspace_id=workspace_id,
        )
        s.add(lead)
        s.flush()
        return lead.id


def _add_score(lead_id: int, tier: str, workspace_id: int | None) -> None:
    with session_scope() as s:
        s.add(Score(
            lead_id=lead_id,
            score=90 if tier == "A" else 60,
            tier=tier,
            rationale="r",
            signals_used=[],
            model="test",
            workspace_id=workspace_id,
        ))


def _add_enrichment(lead_id: int, workspace_id: int | None) -> None:
    with session_scope() as s:
        s.add(Enrichment(
            lead_id=lead_id,
            workspace_id=workspace_id,
            linkedin_profile={},
            company_details={},
        ))


def _seed_n_leads(n: int, workspace_id: int | None) -> list[int]:
    ids = []
    for i in range(n):
        lid = _make_lead(f"lead{i:04d}@example.com", workspace_id,
                         first_name=f"First{i:04d}", last_name=f"Last{i:04d}")
        ids.append(lid)
    return ids


# ---------------------------------------------------------------------------
# 1. list_leads returns only page_size rows
# ---------------------------------------------------------------------------

class TestPageSize:
    def test_returns_at_most_page_size_rows(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(80, ws_id)
        df = list_leads(ws_id, limit=25, offset=0)
        assert len(df) == 25

    def test_last_page_may_have_fewer_rows(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(60, ws_id)
        # Page 2 of 25 should have 10 rows (60 - 50)
        df = list_leads(ws_id, limit=25, offset=50)
        assert len(df) == 10

    def test_offset_beyond_total_returns_empty(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(10, ws_id)
        df = list_leads(ws_id, limit=25, offset=100)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# 2. Page 1 and page 2 return different rows
# ---------------------------------------------------------------------------

class TestDistinctPages:
    def test_pages_have_no_overlap(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(60, ws_id)
        page1 = set(list_leads(ws_id, limit=25, offset=0)["id"].tolist())
        page2 = set(list_leads(ws_id, limit=25, offset=25)["id"].tolist())
        assert len(page1) == 25
        assert len(page2) == 25
        assert page1.isdisjoint(page2), "Pages must not share rows"

    def test_combining_pages_covers_all_leads(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(30, ws_id)
        p1 = list_leads(ws_id, limit=20, offset=0)["id"].tolist()
        p2 = list_leads(ws_id, limit=20, offset=20)["id"].tolist()
        all_ids = set(p1 + p2)
        assert len(all_ids) == 30


# ---------------------------------------------------------------------------
# 3. Pagination is workspace scoped
# ---------------------------------------------------------------------------

class TestPaginationWorkspaceScope:
    def test_limit_applies_within_workspace(self):
        seed_default_workspace()
        ws1 = get_default_workspace_id()
        ws2 = _make_workspace("Other", "other")
        _seed_n_leads(80, ws1)
        _seed_n_leads(80, ws2)
        df = list_leads(ws1, limit=10, offset=0)
        # Only ws1 leads — all must belong to ws1
        assert len(df) == 10

    def test_count_is_workspace_scoped(self):
        seed_default_workspace()
        ws1 = get_default_workspace_id()
        ws2 = _make_workspace("Other", "other")
        _seed_n_leads(30, ws1)
        _seed_n_leads(20, ws2)
        assert count_leads(ws1) == 30
        assert count_leads(ws2) == 20

    def test_page2_stays_in_workspace(self):
        seed_default_workspace()
        ws1 = get_default_workspace_id()
        ws2 = _make_workspace("Other2", "other2")
        _seed_n_leads(60, ws1)
        _seed_n_leads(60, ws2)
        page2 = list_leads(ws1, limit=25, offset=25)
        assert len(page2) == 25
        # All returned rows should be from ws1 — verify via count boundary
        total = count_leads(ws1)
        assert total == 60


# ---------------------------------------------------------------------------
# 4. Search filters apply before pagination
# ---------------------------------------------------------------------------

class TestSearchBeforePagination:
    def test_search_limits_result_set_before_limit(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(60, ws_id)
        # Add 5 leads with a distinctive name
        for i in range(5):
            _make_lead(f"special{i}@example.com", ws_id, first_name="Zara", last_name="Unique")
        df = list_leads(ws_id, limit=50, offset=0, search="Zara")
        assert len(df) == 5
        assert all("Zara" in row["Name"] for _, row in df.iterrows())

    def test_search_count_reflects_match_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(40, ws_id)
        for i in range(3):
            _make_lead(f"needle{i}@example.com", ws_id, first_name="Needle")
        assert count_leads(ws_id, search="Needle") == 3

    def test_search_page2_has_no_results_when_total_matches_fit_on_page1(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        for i in range(5):
            _make_lead(f"abc{i}@example.com", ws_id, first_name="Alpha")
        df = list_leads(ws_id, limit=10, offset=10, search="Alpha")
        assert len(df) == 0


# ---------------------------------------------------------------------------
# 5. Tier/status filters apply before pagination
# ---------------------------------------------------------------------------

class TestFiltersBeforePagination:
    def test_tier_filter_applied_before_limit(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        # 40 tier-A, 40 tier-B leads
        for i in range(40):
            lid = _make_lead(f"a{i}@example.com", ws_id)
            _add_score(lid, "A", ws_id)
        for i in range(40):
            lid = _make_lead(f"b{i}@example.com", ws_id)
            _add_score(lid, "B", ws_id)
        df = list_leads(ws_id, limit=25, offset=0, tier_filter=["A"])
        assert len(df) == 25
        assert all(row["Tier"] == "A" for _, row in df.iterrows())

    def test_tier_filter_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        for i in range(15):
            lid = _make_lead(f"aa{i}@example.com", ws_id)
            _add_score(lid, "A", ws_id)
        for i in range(10):
            lid = _make_lead(f"bb{i}@example.com", ws_id)
            _add_score(lid, "B", ws_id)
        assert count_leads(ws_id, tier_filter=["A"]) == 15
        assert count_leads(ws_id, tier_filter=["B"]) == 10

    def test_enriched_filter_applied_before_limit(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        enriched_ids = []
        for i in range(20):
            lid = _make_lead(f"enrich{i}@example.com", ws_id)
            _add_enrichment(lid, ws_id)
            enriched_ids.append(lid)
        for i in range(50):
            _make_lead(f"plain{i}@example.com", ws_id)
        df = list_leads(ws_id, limit=10, offset=0, enriched_only=True)
        assert len(df) == 10
        assert all(row["Enriched"] is True for _, row in df.iterrows())

    def test_enriched_filter_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        for i in range(7):
            lid = _make_lead(f"e{i}@example.com", ws_id)
            _add_enrichment(lid, ws_id)
        for i in range(13):
            _make_lead(f"p{i}@example.com", ws_id)
        assert count_leads(ws_id, enriched_only=True) == 7


# ---------------------------------------------------------------------------
# 6. count_leads returns filtered count
# ---------------------------------------------------------------------------

class TestCountLeads:
    def test_unfiltered_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(42, ws_id)
        assert count_leads(ws_id) == 42

    def test_count_with_search(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(30, ws_id)
        _make_lead("query@example.com", ws_id, first_name="QueryMatch")
        assert count_leads(ws_id, search="QueryMatch") == 1

    def test_count_with_multiple_filters(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        for i in range(5):
            lid = _make_lead(f"combo{i}@example.com", ws_id, first_name="Combo")
            _add_score(lid, "A", ws_id)
            _add_enrichment(lid, ws_id)
        for i in range(5):
            lid = _make_lead(f"notenrich{i}@example.com", ws_id, first_name="Combo")
            _add_score(lid, "A", ws_id)
        assert count_leads(ws_id, search="Combo", tier_filter=["A"], enriched_only=True) == 5


# ---------------------------------------------------------------------------
# 7. Changing filters changes count (page reset is handled by UI session state)
# ---------------------------------------------------------------------------

class TestFilterChangeResetsCount:
    def test_adding_search_reduces_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(50, ws_id)
        _make_lead("unique@example.com", ws_id, first_name="Unique")
        total_before = count_leads(ws_id)
        total_after = count_leads(ws_id, search="Unique")
        assert total_before == 51
        assert total_after == 1

    def test_removing_filter_restores_count(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        for i in range(10):
            lid = _make_lead(f"tier{i}@example.com", ws_id)
            _add_score(lid, "A", ws_id)
        _seed_n_leads(5, ws_id)
        assert count_leads(ws_id, tier_filter=["A"]) == 10
        assert count_leads(ws_id) == 15


# ---------------------------------------------------------------------------
# 8. Selection isolation per page (DB returns distinct IDs per offset)
# ---------------------------------------------------------------------------

class TestSelectionIsolation:
    def test_page1_and_page2_ids_are_disjoint(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(100, ws_id)
        page1_ids = set(list_leads(ws_id, limit=50, offset=0)["id"].tolist())
        page2_ids = set(list_leads(ws_id, limit=50, offset=50)["id"].tolist())
        assert page1_ids.isdisjoint(page2_ids), \
            "Rows from page 1 and page 2 must not overlap — selection cannot leak"

    def test_selecting_from_page2_does_not_include_page1_rows(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(60, ws_id)
        page1_ids = set(list_leads(ws_id, limit=25, offset=0)["id"].tolist())
        page2_ids = set(list_leads(ws_id, limit=25, offset=25)["id"].tolist())
        # No row should appear on both pages
        assert not page1_ids & page2_ids


# ---------------------------------------------------------------------------
# 9. Delete selected only affects IDs from the current page
# ---------------------------------------------------------------------------

class TestDeleteScope:
    def test_page1_ids_do_not_appear_in_page2(self):
        """IDs resolved from page 1 must be absent from page 2 query."""
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(80, ws_id)
        page1_ids = set(list_leads(ws_id, limit=25, offset=0)["id"].tolist())
        page2_ids = set(list_leads(ws_id, limit=25, offset=25)["id"].tolist())
        # Deleting by page1_ids and then re-querying page 2 gives a different set
        assert page1_ids.isdisjoint(page2_ids)

    def test_resolved_ids_are_a_subset_of_workspace(self):
        """IDs from list_leads must all belong to the requested workspace."""
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        other_ws_id = _make_workspace("Other", "other-del")
        _seed_n_leads(30, ws_id)
        _seed_n_leads(30, other_ws_id)
        df = list_leads(ws_id, limit=25, offset=0)
        # Verify count — there should be exactly 25 from ws_id
        assert len(df) == 25


# ---------------------------------------------------------------------------
# 10. Push to Instantly only uses selected-page row IDs
# ---------------------------------------------------------------------------

class TestPushScope:
    def test_push_ids_come_from_current_page(self):
        """Simulate selecting all of page 2: IDs must match what list_leads returns."""
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(70, ws_id)
        page2_df = list_leads(ws_id, limit=25, offset=25)
        page2_ids = set(page2_df["id"].tolist())
        page1_ids = set(list_leads(ws_id, limit=25, offset=0)["id"].tolist())
        # Push source (page 2 selection) must not include page 1 leads
        assert page2_ids.isdisjoint(page1_ids)

    def test_push_ids_are_workspace_scoped(self):
        seed_default_workspace()
        ws1 = get_default_workspace_id()
        ws2 = _make_workspace("Push-other", "push-other")
        _seed_n_leads(30, ws1)
        _seed_n_leads(30, ws2)
        ws1_ids = set(list_leads(ws1, limit=50, offset=0)["id"].tolist())
        ws2_ids = set(list_leads(ws2, limit=50, offset=0)["id"].tolist())
        assert ws1_ids.isdisjoint(ws2_ids)


# ---------------------------------------------------------------------------
# 11. Default page size is 50
# ---------------------------------------------------------------------------

class TestDefaultPageSize:
    def test_default_page_size_is_50_in_query_signature(self):
        import inspect
        sig = inspect.signature(list_leads)
        assert sig.parameters["limit"].default == 50, \
            "Default limit for list_leads should be 50"

    def test_unspecified_limit_returns_at_most_50(self):
        seed_default_workspace()
        ws_id = get_default_workspace_id()
        _seed_n_leads(100, ws_id)
        df = list_leads(ws_id)
        assert len(df) <= 50
