"""Content-generation type toggles (cost-saving hotfix).

Default: email on, call script off, LinkedIn DM off. Every generation path
must respect the toggles and skip disabled types cleanly — no LLM call, no
placeholder/failed record, and existing saved content is never deleted.

LLM is never hit: generate_email is monkeypatched where a generation path is
exercised; disabled paths must short-circuit before any LLM/generator call.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.db import session_scope
from src.icp_config import (
    ICPConfig,
    is_content_type_enabled,
    save_workspace_icp_config,
)
from src.models import GeneratedContent, Lead
from src.workspace import create_workspace, get_default_workspace_id, seed_default_workspace


def _seed_osp() -> int:
    seed_default_workspace()
    osp = get_default_workspace_id()
    assert osp is not None
    return osp


def _make_lead(email: str, ws: int) -> int:
    with session_scope() as s:
        lead = Lead(first_name="T", last_name="U", email=email, company="Acme",
                    company_domain="acme.com", workspace_id=ws)
        s.add(lead)
        s.flush()
        return lead.id


def _set_toggles(ws: int, *, email=True, call=False, dm=False) -> None:
    cfg = ICPConfig(
        generate_email_enabled=email,
        generate_call_script_enabled=call,
        generate_linkedin_dm_enabled=dm,
    )
    save_workspace_icp_config(cfg, workspace_id=ws)


# ---------------------------------------------------------------------------
# 1. Default workspace settings generate email only
# ---------------------------------------------------------------------------

def test_defaults_email_on_call_and_dm_off():
    c = ICPConfig()
    assert c.generate_email_enabled is True
    assert c.generate_call_script_enabled is False
    assert c.generate_linkedin_dm_enabled is False


def test_is_content_type_enabled_defaults():
    osp = _seed_osp()  # OSP workspace seeded with default config
    assert is_content_type_enabled("email", osp) is True
    assert is_content_type_enabled("call_script", osp) is False
    assert is_content_type_enabled("linkedin_msg", osp) is False


# ---------------------------------------------------------------------------
# 2 & 3. Call script / LinkedIn DM generators skip when disabled
# ---------------------------------------------------------------------------

def test_call_script_skipped_when_disabled(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("c@x.com", osp)

    # Generator must NOT hit the LLM when disabled.
    import src.content.call_script as cs

    async def _boom(**kwargs):
        raise AssertionError("LLM must not be called when call_script is disabled")

    monkeypatch.setattr(cs, "generate_json", _boom)

    from src.content.call_script import generate_call_script
    out = asyncio.run(generate_call_script(lead_id, workspace_id=osp))
    assert out is None
    with session_scope() as s:
        rows = s.execute(
            select(GeneratedContent).where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "call_script",
            )
        ).scalars().all()
        assert rows == []  # no placeholder / failed record written


def test_linkedin_dm_skipped_when_disabled(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("d@x.com", osp)

    import src.content.linkedin_msg as lm

    async def _boom(**kwargs):
        raise AssertionError("LLM must not be called when linkedin_msg is disabled")

    monkeypatch.setattr(lm, "generate_json", _boom)

    from src.content.linkedin_msg import generate_linkedin_msg
    out = asyncio.run(generate_linkedin_msg(lead_id, workspace_id=osp))
    assert out is None
    with session_scope() as s:
        rows = s.execute(
            select(GeneratedContent).where(
                GeneratedContent.lead_id == lead_id,
                GeneratedContent.kind == "linkedin_msg",
            )
        ).scalars().all()
        assert rows == []


def test_call_script_runs_when_enabled(monkeypatch):
    osp = _seed_osp()
    _set_toggles(osp, email=True, call=True, dm=False)
    lead_id = _make_lead("e@x.com", osp)

    import src.content.call_script as cs
    from src.content.call_script import CallScript, Objection

    async def _fake(**kwargs):
        return CallScript(
            opener="hi", value_prop="vp",
            objections=[Objection(objection=f"o{i}", response="r") for i in range(3)],
            close="bye",
        )

    monkeypatch.setattr(cs, "generate_json", _fake)
    from src.content.call_script import generate_call_script
    out = asyncio.run(generate_call_script(lead_id, workspace_id=osp))
    assert out is not None  # generates only when enabled


# ---------------------------------------------------------------------------
# 4. Bulk pipeline does not generate call scripts/DMs when disabled
# ---------------------------------------------------------------------------

def test_bulk_pipeline_skips_disabled_kinds(monkeypatch):
    osp = _seed_osp()  # defaults: call/dm off
    lead_id = _make_lead("f@x.com", osp)

    called: list[int] = []

    async def _fake_email(lid, *, workspace_id=None, **k):
        called.append(lid)
        return object()

    async def _boom(*a, **k):
        raise AssertionError("disabled generator was invoked")

    import src.lib.pipeline_runner as pr
    monkeypatch.setattr(pr, "_KIND_GENERATOR", {
        "email": _fake_email,
        "call_script": _boom,
        "linkedin_msg": _boom,
    })

    updates = []
    asyncio.run(pr._phase_content([lead_id], updates.append, workspace_id=osp))
    # email ran; call/dm reported disabled, never invoked.
    payloads = [u.payload or {} for u in updates if u.phase == "content"]
    disabled = [k for p in payloads for k in p.get("disabled_kinds", [])]
    assert "call_script" in disabled
    assert "linkedin_msg" in disabled
    assert called == [lead_id]


# ---------------------------------------------------------------------------
# 5 & 6. Evergreen process does not generate call scripts / DMs when disabled
# ---------------------------------------------------------------------------

def test_evergreen_process_skips_disabled(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("g@x.com", osp)

    ran: list[str] = []

    async def _email(lid, *, workspace_id=None):
        ran.append("email")
        return object()

    async def _boom(lid, *, workspace_id=None):
        raise AssertionError("disabled generator invoked in evergreen")

    monkeypatch.setattr("src.content.email.generate_email", _email)
    monkeypatch.setattr("src.content.call_script.generate_call_script", _boom)
    monkeypatch.setattr("src.content.linkedin_msg.generate_linkedin_msg", _boom)

    from src.lead_source.scheduler import _generate_content_one
    count = asyncio.run(_generate_content_one(lead_id, osp))
    assert ran == ["email"]
    assert count == 1  # only email counted


# ---------------------------------------------------------------------------
# 7. Manual regenerate respects toggles
# ---------------------------------------------------------------------------

def test_regenerate_refused_for_disabled_kind(monkeypatch):
    osp = _seed_osp()  # call_script disabled by default
    lead_id = _make_lead("h@x.com", osp)
    with session_scope() as s:
        row = GeneratedContent(
            lead_id=lead_id, kind="call_script", subject=None, body="old script",
            signals_cited=[], prompt_version="v", model="m", workspace_id=osp,
        )
        s.add(row)
        s.flush()
        cid = row.id

    from src.feedback.regenerate import RegenerateRefused, regenerate_direct
    import pytest
    with pytest.raises(RegenerateRefused):
        asyncio.run(regenerate_direct(cid, "make it better"))


# ---------------------------------------------------------------------------
# 8 & 9. Existing saved call scripts / LinkedIn DMs are not deleted
# ---------------------------------------------------------------------------

def test_existing_call_script_and_dm_preserved(monkeypatch):
    osp = _seed_osp()  # call/dm disabled
    lead_id = _make_lead("i@x.com", osp)
    with session_scope() as s:
        s.add(GeneratedContent(lead_id=lead_id, kind="call_script", subject=None,
                               body="saved script", signals_cited=[], prompt_version="v",
                               model="m", workspace_id=osp))
        s.add(GeneratedContent(lead_id=lead_id, kind="linkedin_msg", subject=None,
                               body="saved dm", signals_cited=[], prompt_version="v",
                               model="m", workspace_id=osp))

    # Run the (disabled) generators — must be no-ops, leaving saved rows intact.
    import src.content.call_script as cs
    import src.content.linkedin_msg as lm

    async def _boom(**k):
        raise AssertionError("LLM must not run")

    monkeypatch.setattr(cs, "generate_json", _boom)
    monkeypatch.setattr(lm, "generate_json", _boom)
    from src.content.call_script import generate_call_script
    from src.content.linkedin_msg import generate_linkedin_msg
    asyncio.run(generate_call_script(lead_id, workspace_id=osp))
    asyncio.run(generate_linkedin_msg(lead_id, workspace_id=osp))

    with session_scope() as s:
        scripts = s.execute(select(GeneratedContent.body).where(
            GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "call_script"
        )).scalars().all()
        dms = s.execute(select(GeneratedContent.body).where(
            GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "linkedin_msg"
        )).scalars().all()
    assert scripts == ["saved script"]
    assert dms == ["saved dm"]


# ---------------------------------------------------------------------------
# 10. Workspace A settings do not affect Workspace B
# ---------------------------------------------------------------------------

def test_toggles_workspace_isolated():
    osp = _seed_osp()
    other = create_workspace(name="WS B", slug="ws-b", instantly_campaign_id="c-b")["id"]
    _set_toggles(osp, email=True, call=True, dm=True)        # OSP: all on
    _set_toggles(other, email=True, call=False, dm=False)    # B: only email

    assert is_content_type_enabled("call_script", osp) is True
    assert is_content_type_enabled("call_script", other) is False
    assert is_content_type_enabled("linkedin_msg", osp) is True
    assert is_content_type_enabled("linkedin_msg", other) is False


# ---------------------------------------------------------------------------
# 11. Email generation still works (enabled by default)
# ---------------------------------------------------------------------------

def test_email_generation_still_works(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("j@x.com", osp)

    import src.content.email as em
    from src.content.email import EmailResult

    async def _fake(**kwargs):
        return EmailResult(subject="hi", body="real body here", signals_cited=[])

    monkeypatch.setattr(em, "generate_json", _fake)
    from src.content.email import generate_email
    out = asyncio.run(generate_email(lead_id, workspace_id=osp))
    assert out is not None
    assert out.subject == "hi"
    with session_scope() as s:
        rows = s.execute(select(GeneratedContent).where(
            GeneratedContent.lead_id == lead_id, GeneratedContent.kind == "email"
        )).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 12 & 13. Disabled generation never pushes or sends
# ---------------------------------------------------------------------------

def test_disabled_generation_does_not_push_or_send(monkeypatch):
    osp = _seed_osp()
    lead_id = _make_lead("k@x.com", osp)

    import src.delivery.instantly as inst

    def _boom_deliver(*a, **k):
        raise AssertionError("delivery/push must never run from content generation")

    monkeypatch.setattr(inst, "deliver_email", _boom_deliver, raising=False)

    import src.content.call_script as cs
    async def _boom(**k):
        raise AssertionError("LLM must not run")
    monkeypatch.setattr(cs, "generate_json", _boom)

    from src.content.call_script import generate_call_script
    out = asyncio.run(generate_call_script(lead_id, workspace_id=osp))
    assert out is None  # skipped cleanly, nothing sent/pushed
