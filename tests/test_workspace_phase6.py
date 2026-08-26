"""Phase 6 workspace tests.

Verifies:
  - Winners: OSP winners don't appear in Test Client; non-OSP workspaces use DB only.
  - JSON winners migrated to OSP DB on startup.
  - Self-improvement: recommendations scoped to workspace.
  - Approve recommendation updates only the correct workspace prompt.
  - Regenerate with feedback inherits workspace from source row.
  - Instantly API key resolver uses workspace key over env.
  - Instantly campaign resolver uses workspace campaign ID.
  - CSV import assigns leads to selected workspace_id.
  - Same email can exist in different workspaces.
  - Same email dedupes inside the same workspace.
  - Imported leads in Test Client do not appear in OSP Leads page.
"""
from __future__ import annotations

import pytest

from src.db import session_scope
from src.models import (
    GeneratedContent,
    Lead,
    PromptConfig,
    PromptRecommendation,
    WinningExample,
)
from src.workspace import (
    create_workspace,
    get_api_key_for_workspace,
    get_campaign_id_for_workspace,
    get_default_workspace_id,
    migrate_json_winners_to_osp_db,
    seed_default_workspace,
)


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
        first_name="T", last_name="U", email=email,
        company_domain="ex.com", workspace_id=workspace_id,
    )
    session.add(lead)
    session.flush()
    return lead


def _add_winning_example(workspace_id: int, content_type: str = "email") -> None:
    with session_scope() as session:
        session.add(WinningExample(
            lead_context={},
            subject="Test winner",
            body="Winner body",
            reply_rate=0.5,
            workspace_id=workspace_id,
            content_type=content_type,
        ))


# ---------------------------------------------------------------------------
# 1–4. Winners library workspace scoping
# ---------------------------------------------------------------------------

class TestWinnersWorkspaceScoping:
    def test_osp_winners_not_visible_in_other_workspace(self):
        from src.content.winners import load_top_winners_for
        osp_id = _seed_osp()
        _add_winning_example(osp_id, "email")
        other_ws = create_workspace(name="No Win WS", slug="no-win-ws", instantly_campaign_id="c-nw")

        other_winners = load_top_winners_for("email", k=3, workspace_id=other_ws["id"])
        assert other_winners == []

    def test_other_workspace_winners_not_visible_in_osp(self):
        from src.content.winners import load_top_winners_for
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Win WS", slug="win-ws", instantly_campaign_id="c-w")
        _add_winning_example(other_ws["id"], "email")

        # OSP DB has no WinningExample rows (only the other workspace does).
        osp_winners = load_top_winners_for("email", k=3, workspace_id=osp_id)
        osp_bodies = [w["body"] for w in osp_winners]
        assert "Winner body" not in osp_bodies

    def test_non_osp_workspace_returns_empty_if_no_db_winners(self):
        from src.content.winners import load_top_winners_for
        _seed_osp()
        other_ws = create_workspace(name="Empty Win WS", slug="empty-win-ws", instantly_campaign_id="c-ew")
        winners = load_top_winners_for("email", k=3, workspace_id=other_ws["id"])
        assert winners == []

    def test_negatives_empty_for_non_osp_workspace(self):
        from src.content.winners import load_top_negatives
        _seed_osp()
        other_ws = create_workspace(name="Neg WS", slug="neg-ws", instantly_campaign_id="c-ng")
        negatives = load_top_negatives("email", k=2, workspace_id=other_ws["id"])
        assert negatives == []

    def test_osp_db_winners_used_when_present(self):
        from src.content.winners import load_top_winners_for
        osp_id = _seed_osp()
        _add_winning_example(osp_id, "email")

        osp_winners = load_top_winners_for("email", k=3, workspace_id=osp_id)
        assert len(osp_winners) >= 1
        assert any(w["body"] == "Winner body" for w in osp_winners)


# ---------------------------------------------------------------------------
# 2. JSON winners migration
# ---------------------------------------------------------------------------

class TestJsonWinnersMigration:
    def test_migration_inserts_rows_for_osp(self, tmp_path, monkeypatch):
        from src.content import winners as wmod
        osp_id = _seed_osp()

        fake_json = tmp_path / "winning_examples.json"
        import json
        fake_json.write_text(json.dumps([
            {
                "content_type": "email",
                "winner_reason": "seed",
                "subject": "Migrated winner",
                "body": "Migrated body",
                "reply_rate": 0.3,
                "lead_context": {"title": "CEO"},
                "is_active": True,
            }
        ]), encoding="utf-8")
        monkeypatch.setattr(wmod, "WINNERS_PATH", fake_json)

        count = migrate_json_winners_to_osp_db()
        assert count >= 1

        with session_scope() as session:
            from sqlalchemy import select as _sel
            rows = session.execute(
                _sel(WinningExample).where(WinningExample.workspace_id == osp_id)
            ).scalars().all()
            assert any(r.subject == "Migrated winner" for r in rows)

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        from src.content import winners as wmod
        osp_id = _seed_osp()

        fake_json = tmp_path / "winning_examples.json"
        import json
        fake_json.write_text(json.dumps([
            {"content_type": "email", "winner_reason": "seed", "subject": "X", "body": "B", "reply_rate": 0.1}
        ]), encoding="utf-8")
        monkeypatch.setattr(wmod, "WINNERS_PATH", fake_json)

        count1 = migrate_json_winners_to_osp_db()
        count2 = migrate_json_winners_to_osp_db()  # second call should be no-op
        assert count1 >= 1
        assert count2 == 0

    def test_migration_does_not_affect_other_workspaces(self, tmp_path, monkeypatch):
        from src.content import winners as wmod
        osp_id = _seed_osp()
        other_ws = create_workspace(name="No Migrate WS", slug="no-migrate-ws", instantly_campaign_id="c-nm")

        fake_json = tmp_path / "winning_examples.json"
        import json
        fake_json.write_text(json.dumps([
            {"content_type": "email", "winner_reason": "seed", "subject": "OSP Only", "body": "B", "reply_rate": 0.1}
        ]), encoding="utf-8")
        monkeypatch.setattr(wmod, "WINNERS_PATH", fake_json)

        migrate_json_winners_to_osp_db()

        with session_scope() as session:
            from sqlalchemy import select as _sel
            count = session.execute(
                _sel(WinningExample).where(WinningExample.workspace_id == other_ws["id"])
            ).scalar() or 0
            assert count == 0


# ---------------------------------------------------------------------------
# 5–8. Self-improvement loop workspace scoping
# ---------------------------------------------------------------------------

class TestRecommendationWorkspaceScoping:
    def test_recommendations_scoped_by_workspace(self):
        from src.feedback.self_improvement import list_recommendations, save_recommendation
        from src.feedback.self_improvement import Diagnosis, LOOP_READY
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Rec WS", slug="rec-ws", instantly_campaign_id="c-rec")

        diag = Diagnosis(
            loop_status=LOOP_READY,
            bottleneck="open_rate", channel="email",
            confidence="standard",
            diagnosis="Low open rate",
            current_metric_label="open_rate", current_metric_value=0.1,
            target_metric_value=0.3,
            recommended_change="Improve subject",
            expected_impact="Higher opens",
            risk_level="low",
            proposed_addendum="# SELF IMPROVEMENT ADDENDUM\nBetter subject.",
            previous_prompt_snapshot="old",
            sample_size=200,
            hours_since_latest_send=48.0,
        )
        save_recommendation(diag, workspace_id=osp_id)
        save_recommendation(diag, workspace_id=other_ws["id"])

        osp_recs = list_recommendations(workspace_id=osp_id)
        other_recs = list_recommendations(workspace_id=other_ws["id"])
        assert len(osp_recs) >= 1
        assert len(other_recs) >= 1

        # OSP recs should not appear in other workspace list
        osp_ids = {r["id"] for r in osp_recs}
        other_ids = {r["id"] for r in other_recs}
        assert osp_ids.isdisjoint(other_ids)

    def test_approve_recommendation_updates_correct_workspace_prompt(self):
        from sqlalchemy import select as _sel
        from src.feedback.self_improvement import save_recommendation, approve_recommendation, Diagnosis, LOOP_READY
        from src.prompts.loader import get_effective_prompt, save_overlay
        from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY

        osp_id = _seed_osp()
        other_ws = create_workspace(name="Approve WS", slug="approve-ws", instantly_campaign_id="c-app")

        # Set OSP prompt
        save_overlay("email", "OSP base prompt", workspace_id=osp_id)
        # Set other workspace prompt
        save_overlay("email", "Other base prompt", workspace_id=other_ws["id"])

        diag = Diagnosis(
            loop_status=LOOP_READY,
            bottleneck="open_rate", channel="email",
            confidence="standard",
            diagnosis="Low opens",
            current_metric_label="open_rate", current_metric_value=0.1,
            target_metric_value=0.3,
            recommended_change="Subject change",
            expected_impact="Better opens",
            risk_level="low",
            proposed_addendum="Try shorter subject lines for better open rates.",
            previous_prompt_snapshot="Other base prompt",
            sample_size=200,
            hours_since_latest_send=48.0,
        )
        rec_id = save_recommendation(diag, workspace_id=other_ws["id"])
        assert rec_id is not None

        approve_recommendation(rec_id, approved_by="test", workspace_id=other_ws["id"])

        # OSP prompt should be unchanged
        osp_prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=osp_id)
        assert "OSP base prompt" in osp_prompt.strip()

        # Other workspace prompt should have been updated
        other_prompt = get_effective_prompt("email", DEFAULT_EMAIL_PROMPT_BODY, workspace_id=other_ws["id"])
        assert "shorter subject lines" in other_prompt or "SELF IMPROVEMENT" in other_prompt


# ---------------------------------------------------------------------------
# 9–10. Regenerate with feedback workspace scoping
# ---------------------------------------------------------------------------

class TestRegenerateWithFeedbackWorkspaceScoping:
    def test_regenerate_direct_uses_source_row_workspace(self, monkeypatch):
        import asyncio
        from src.feedback.regenerate import regenerate_direct

        osp_id = _seed_osp()
        other_ws = create_workspace(name="Regen WS", slug="regen-ws", instantly_campaign_id="c-rg")

        captured_workspace_ids = []

        async def fake_generate(lead_id, *, regeneration_feedback=None, workspace_id=None):
            captured_workspace_ids.append(workspace_id)
            with session_scope() as session:
                session.add(GeneratedContent(
                    lead_id=lead_id, kind="email",
                    subject="new", body="new body",
                    signals_cited=[], prompt_version="v1", model="x",
                    workspace_id=workspace_id,
                ))

        import src.feedback.regenerate as regen_mod
        monkeypatch.setattr(regen_mod, "_KIND_DISPATCH", {"email": fake_generate})

        with session_scope() as session:
            lead = _make_lead(session, "regen@example.com", other_ws["id"])
            src_content = GeneratedContent(
                lead_id=lead.id, kind="email",
                subject="original", body="original body",
                signals_cited=[], prompt_version="v1", model="x",
                workspace_id=other_ws["id"],
            )
            session.add(src_content)
            session.flush()
            src_id = src_content.id

        asyncio.run(regenerate_direct(src_id, "Please improve"))

        assert other_ws["id"] in captured_workspace_ids


# ---------------------------------------------------------------------------
# 11–13. Instantly API key and campaign ID routing
# ---------------------------------------------------------------------------

class TestInstantlyWorkspaceRouting:
    def test_workspace_api_key_used_over_env(self, monkeypatch):
        _seed_osp()
        ws = create_workspace(
            name="API Key Test WS",
            slug="api-key-test-ws",
            instantly_campaign_id="camp-test",
            instantly_api_key="workspace-api-key",
        )
        monkeypatch.setattr("src.workspace.settings.instantly_api_key", "env-key")
        resolved = get_api_key_for_workspace(ws["id"])
        assert resolved == "workspace-api-key"

    def test_env_fallback_when_workspace_key_blank(self, monkeypatch):
        _seed_osp()
        ws = create_workspace(
            name="No Key WS 6",
            slug="no-key-ws-6",
            instantly_campaign_id="camp-nk",
        )
        monkeypatch.setattr("src.workspace.settings.instantly_api_key", "env-fallback-key")
        resolved = get_api_key_for_workspace(ws["id"])
        assert resolved == "env-fallback-key"

    def test_campaign_id_used_from_workspace(self):
        _seed_osp()
        ws = create_workspace(
            name="Campaign WS 6",
            slug="campaign-ws-6",
            instantly_campaign_id="workspace-campaign-123",
        )
        resolved = get_campaign_id_for_workspace(ws["id"])
        assert resolved == "workspace-campaign-123"

    def test_deliver_email_signature_accepts_workspace_id(self):
        import inspect
        from src.delivery.instantly import deliver_email
        sig = inspect.signature(deliver_email)
        assert "workspace_id" in sig.parameters


# ---------------------------------------------------------------------------
# 15–18. CSV import workspace scoping
# ---------------------------------------------------------------------------

class TestCsvImportWorkspaceScoping:
    def test_import_assigns_leads_to_workspace(self):
        from src.ingest_writer import ingest_rows
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Import WS", slug="import-ws", instantly_campaign_id="c-imp")

        rows = [{"first_name": "John", "last_name": "Doe", "email": "john@import.com"}]
        with session_scope() as session:
            stats = ingest_rows(rows, session, workspace_id=other_ws["id"])

        assert stats.inserted == 1

        with session_scope() as session:
            from sqlalchemy import select as _sel
            lead = session.execute(
                _sel(Lead).where(Lead.email == "john@import.com")
                .where(Lead.workspace_id == other_ws["id"])
            ).scalar_one_or_none()
            assert lead is not None
            assert lead.workspace_id == other_ws["id"]

    def test_same_email_allowed_in_different_workspaces(self):
        from src.ingest_writer import ingest_rows
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Dup Email WS", slug="dup-email-ws", instantly_campaign_id="c-de")

        rows = [{"first_name": "Jane", "last_name": "Doe", "email": "jane@shared.com"}]
        with session_scope() as session:
            stats1 = ingest_rows(rows, session, workspace_id=osp_id)
        with session_scope() as session:
            stats2 = ingest_rows(rows, session, workspace_id=other_ws["id"])

        # Both should succeed
        assert stats1.inserted == 1
        assert stats2.inserted == 1

        with session_scope() as session:
            from sqlalchemy import select as _sel
            leads = session.execute(
                _sel(Lead).where(Lead.email == "jane@shared.com")
            ).scalars().all()
            assert len(leads) == 2
            ws_ids = {l.workspace_id for l in leads}
            assert osp_id in ws_ids
            assert other_ws["id"] in ws_ids

    def test_same_email_dedupes_within_workspace(self):
        from src.ingest_writer import ingest_rows
        osp_id = _seed_osp()

        rows = [
            {"first_name": "Bob", "last_name": "Smith", "email": "bob@dup.com"},
            {"first_name": "Bob", "last_name": "Smith", "email": "bob@dup.com"},
        ]
        with session_scope() as session:
            stats = ingest_rows(rows, session, workspace_id=osp_id)

        # One inserted, one deduped within file
        assert stats.inserted == 1
        assert stats.deduped_within_file == 1

    def test_imported_leads_not_visible_in_other_workspace(self):
        from src.ingest_writer import ingest_rows
        from src.lib.db_queries import list_leads
        osp_id = _seed_osp()
        other_ws = create_workspace(name="Isolated Import WS", slug="isolated-import-ws", instantly_campaign_id="c-ii")

        rows = [{"first_name": "Alice", "last_name": "Wonder", "email": "alice@isolated.com"}]
        with session_scope() as session:
            ingest_rows(rows, session, workspace_id=other_ws["id"])

        osp_df = list_leads(workspace_id=osp_id)
        osp_emails = list(osp_df["Email"]) if not osp_df.empty else []
        assert "alice@isolated.com" not in osp_emails

    def test_ingest_rows_accepts_workspace_id_signature(self):
        import inspect
        from src.ingest_writer import ingest_rows
        sig = inspect.signature(ingest_rows)
        assert "workspace_id" in sig.parameters
