"""ICP configuration — user-editable JSON config that feeds scoring,
content generation, and Tavily news queries.

Phase 4 hotfix: ICP config is now stored per workspace in the `workspaces.icp_config`
JSON column. The Settings page reads/writes from the DB via `load_workspace_icp_config`
and `save_workspace_icp_config`. Content generators and scoring still use the file-based
`load_icp_config()` so they are unchanged.

Singleton file at ``data/icp_config.json``. Still used as:
  1. Seed source for the OSP workspace on first startup (backfill_osp_icp_config).
  2. Fallback for content generators / scoring until they are workspace-aware (Phase 5).

Atomic write on save (tmp + os.replace) so a concurrent reader never
sees a half-written file. Falls back to ``default_icp_config()`` when
the file is missing, so a fresh checkout behaves identically to today's
hardcoded values.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)

# Resolved relative to the repo root (parent of the src/ directory).
# Using an absolute path avoids working-directory sensitivity on Streamlit Cloud.
_REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _REPO_ROOT / "data" / "icp_config.json"
# Legacy relative form kept as backup for callers that override path= explicitly.
_CONFIG_PATH_RELATIVE = Path("data/icp_config.json")


class CompanyProfile(BaseModel):
    name: str = "Our Company"
    one_liner: str = ""
    value_props: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)


class ICPDefinition(BaseModel):
    target_industries: list[str] = Field(default_factory=list)
    target_company_sizes: list[str] = Field(default_factory=list)
    target_geographies: list[str] = Field(default_factory=list)
    target_tech_stack_signals: list[str] = Field(default_factory=list)
    target_company_stages: list[str] = Field(default_factory=list)


class BuyerPersona(BaseModel):
    target_titles: list[str] = Field(default_factory=list)
    seniority_levels: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    top_pain_points: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)


class IntentSignals(BaseModel):
    positive_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)


class ICPConfig(BaseModel):
    company: CompanyProfile = Field(default_factory=CompanyProfile)
    icp: ICPDefinition = Field(default_factory=ICPDefinition)
    persona: BuyerPersona = Field(default_factory=BuyerPersona)
    signals: IntentSignals = Field(default_factory=IntentSignals)
    news_search_terms: list[str] = Field(default_factory=list)
    generate_content_for_all_tiers: bool = False
    # Content-type toggles (workspace scoped via the workspaces.icp_config JSON).
    # Email is on by default; call scripts and LinkedIn DMs are OFF by default to
    # save LLM cost. Workspaces whose icp_config was saved before these fields
    # existed pick up the defaults automatically (call/LinkedIn off).
    generate_email_enabled: bool = True
    generate_call_script_enabled: bool = False
    generate_linkedin_dm_enabled: bool = False
    # Buyer-research Tavily news window. Buyer/company-news research searches
    # for relevant company signals within this many days (default 90) instead
    # of only the last few days, so older-but-relevant funding/hiring/expansion
    # signals aren't missed. See src/enrichment/buyer_accounts.py.
    buyer_research_news_window_days: int = 90
    # Layered Tavily research toggles (Company Researcher / Crawl2RAG style).
    # Crawl the company website + extract top URLs for richer company context.
    # Default on; may use more Tavily credits.
    buyer_research_use_crawl: bool = True
    buyer_research_use_extract: bool = True


# ---------------------------------------------------------------------------
# Defaults — verbatim from today's hardcoded prompts so pre-first-edit
# behaviour is identical to today. Sources cited inline.
# ---------------------------------------------------------------------------

def default_icp_config() -> ICPConfig:
    return ICPConfig(
        company=CompanyProfile(
            name="Our Company",
            # src/prompts/email.py:7-8
            one_liner="B2B SaaS company that helps sales / revenue teams cut wasted pipeline effort",
            value_props=["<edit me in Settings>"],
            differentiators=["<edit me in Settings>"],
        ),
        icp=ICPDefinition(
            # src/prompts/scoring.py:14-15
            target_industries=[
                "B2B SaaS", "Data/AI", "Fintech", "Cloud", "DevTools", "Healthtech",
            ],
            # src/prompts/scoring.py:16
            target_company_sizes=["50-2000 employees"],
            target_geographies=["<edit me in Settings>"],
            target_tech_stack_signals=["<edit me in Settings>"],
            # src/prompts/scoring.py:16, 29-30
            target_company_stages=["Series B", "Series C", "Series D", "stable mid-market"],
        ),
        persona=BuyerPersona(
            # src/prompts/scoring.py:12-13
            target_titles=[
                "Sales leaders", "RevOps leaders", "Growth leaders",
                "Marketing leaders", "VPs", "CROs", "Heads", "Directors",
            ],
            seniority_levels=["VP", "Director", "Head"],
            departments=["Sales", "RevOps", "Growth", "Marketing"],
            # src/prompts/scoring.py:19-20
            top_pain_points=[
                "data fragmentation", "outbound efficiency", "rep ramp",
                "pipeline visibility", "CRM hygiene",
            ],
            common_objections=["<edit me in Settings>"],
        ),
        signals=IntentSignals(
            positive_signals=["<edit me in Settings>"],
            disqualifiers=["<edit me in Settings>"],
        ),
        news_search_terms=["outbound sales", "SDR automation", "revenue operations"],
        generate_content_for_all_tiers=False,
    )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_icp_config(path: Path = CONFIG_PATH) -> ICPConfig:
    """Load config from disk; return defaults if missing or malformed."""
    if not path.exists():
        return default_icp_config()
    try:
        raw = path.read_text(encoding="utf-8")
        return ICPConfig.model_validate_json(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        log.warning("icp_config_load_failed", extra={"path": str(path), "error": str(exc)})
        return default_icp_config()


def save_icp_config(cfg: ICPConfig, path: Path = CONFIG_PATH) -> None:
    """Atomic write: serialise to a tmp file, then os.replace.

    Concurrent readers see either the previous version or the new one,
    never partial bytes. Caller is responsible for cache invalidation
    (st.cache_data.clear() in the Streamlit page).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = cfg.model_dump_json(indent=2)
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Don't leak a half-written .tmp on failure.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _bullets(items: list[str]) -> str:
    cleaned = [s for s in (str(x).strip() for x in items) if s]
    if not cleaned:
        return "—"
    return "\n".join(f"- {s}" for s in cleaned)


def _csv(items: list[str]) -> str:
    cleaned = [s for s in (str(x).strip() for x in items) if s]
    return ", ".join(cleaned) if cleaned else "—"


def render_icp_block(cfg: ICPConfig) -> str:
    """Render the ICP config as a prompt section appended to SYSTEM.

    Shared between scoring + all three content prompts so the model sees
    the same picture regardless of which generator is calling.
    """
    company_line = f"{cfg.company.name} — {cfg.company.one_liner}" if cfg.company.one_liner else cfg.company.name
    return (
        "# About us\n"
        f"{company_line}\n"
        "\n"
        "Value props:\n"
        f"{_bullets(cfg.company.value_props)}\n"
        "\n"
        "Differentiators:\n"
        f"{_bullets(cfg.company.differentiators)}\n"
        "\n"
        "# Our ICP\n"
        f"Target industries: {_csv(cfg.icp.target_industries)}\n"
        f"Target company sizes: {_csv(cfg.icp.target_company_sizes)}\n"
        f"Target stages: {_csv(cfg.icp.target_company_stages)}\n"
        f"Tech-stack signals we look for: {_csv(cfg.icp.target_tech_stack_signals)}\n"
        f"Target geographies: {_csv(cfg.icp.target_geographies)}\n"
        "\n"
        "# Buyer persona\n"
        f"Titles: {_csv(cfg.persona.target_titles)}\n"
        f"Seniority: {_csv(cfg.persona.seniority_levels)}\n"
        f"Departments: {_csv(cfg.persona.departments)}\n"
        "Top pain points:\n"
        f"{_bullets(cfg.persona.top_pain_points)}\n"
        "Common objections:\n"
        f"{_bullets(cfg.persona.common_objections)}\n"
        "\n"
        "# Intent signals\n"
        "Positive signals (boost score / cite if present):\n"
        f"{_bullets(cfg.signals.positive_signals)}\n"
        "Disqualifiers (mark these leads as poor fit — DO NOT target):\n"
        f"{_bullets(cfg.signals.disqualifiers)}\n"
    )


# ---------------------------------------------------------------------------
# Phase 4 hotfix: workspace-scoped load / save
# ---------------------------------------------------------------------------

def load_workspace_icp_config(workspace_id=None):
    try:
        from src.db import session_scope
        from src.models import Workspace
        _ws_id = workspace_id
        if _ws_id is None:
            from src.workspace import get_default_workspace_id
            _ws_id = get_default_workspace_id()
        if _ws_id is not None:
            with session_scope() as session:
                ws = session.get(Workspace, _ws_id)
                if ws is not None and ws.icp_config:
                    try:
                        return ICPConfig.model_validate(ws.icp_config)
                    except Exception as exc:
                        log.warning("workspace_icp_config_invalid", extra={"workspace_id": _ws_id, "error": str(exc)})
        try:
            from src.workspace import get_default_workspace_id as _gd
            default_id = _gd()
        except Exception:
            default_id = None
        if _ws_id is not None and _ws_id == default_id:
            return load_icp_config()
    except Exception as exc:
        log.warning("load_workspace_icp_config_failed", extra={"workspace_id": workspace_id, "error": f"{type(exc).__name__}: {exc}"})
    return default_icp_config()


# Map a GeneratedContent.kind to its ICPConfig toggle field.
_CONTENT_KIND_FLAG = {
    "email": "generate_email_enabled",
    "call_script": "generate_call_script_enabled",
    "linkedin_msg": "generate_linkedin_dm_enabled",
}


def is_content_type_enabled(kind: str, workspace_id=None) -> bool:
    """Return True when the workspace allows generating `kind` content.

    Defaults (when the workspace has no saved config or an older one): email
    on, call_script off, linkedin_msg off. Unknown kinds default to enabled so
    a new content type is never silently suppressed by this gate.
    """
    flag = _CONTENT_KIND_FLAG.get(kind)
    if flag is None:
        return True
    try:
        cfg = load_workspace_icp_config(workspace_id)
        return bool(getattr(cfg, flag))
    except Exception as exc:  # pragma: no cover - defensive; never block email
        log.warning(
            "content_type_toggle_load_failed",
            extra={"kind": kind, "workspace_id": workspace_id, "error": str(exc)},
        )
        return kind == "email"


def save_workspace_icp_config(cfg, workspace_id=None):
    from src.db import session_scope
    from src.models import Workspace
    _ws_id = workspace_id
    if _ws_id is None:
        from src.workspace import get_default_workspace_id
        _ws_id = get_default_workspace_id()
    if _ws_id is None:
        raise RuntimeError("No workspace to save settings to.")
    with session_scope() as session:
        ws = session.get(Workspace, _ws_id)
        if ws is None:
            raise ValueError(f"Workspace {_ws_id} not found.")
        ws.icp_config = cfg.model_dump()
    # Write-through for the default workspace so file-based pipeline stays in sync.
    # Skipped in SQLite (test) environments to avoid polluting test artifacts.
    try:
        from src.db import engine as _engine
        from src.workspace import get_default_workspace_id as _gd
        if _ws_id == _gd() and _engine.dialect.name != "sqlite":
            save_icp_config(cfg)
    except Exception as exc:
        log.warning("icp_config_file_write_through_failed", extra={"workspace_id": _ws_id, "error": f"{type(exc).__name__}: {exc}"})


def copy_workspace_icp_config(from_workspace_id, to_workspace_id):
    try:
        cfg = load_workspace_icp_config(from_workspace_id)
        save_workspace_icp_config(cfg, to_workspace_id)
        return True
    except Exception as exc:
        log.warning("copy_workspace_icp_config_failed", extra={"from": from_workspace_id, "to": to_workspace_id, "error": str(exc)})
        return False


def is_default_icp_config(cfg_dict: dict) -> bool:
    """Return True if cfg_dict matches the hardcoded default_icp_config() exactly.

    Used by backfill logic to detect configs that were accidentally seeded
    with defaults and should be overwritten with real data from the file.
    """
    try:
        stored = ICPConfig.model_validate(cfg_dict)
        return stored == default_icp_config()
    except Exception:
        return False


def _resolve_config_path() -> Path:
    """Return the best available path to icp_config.json.

    Tries the absolute module-relative path first; falls back to the
    relative path for any caller that sets CONFIG_PATH to a custom value.
    """
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    if _CONFIG_PATH_RELATIVE.exists():
        return _CONFIG_PATH_RELATIVE
    return CONFIG_PATH  # return canonical even if absent so callers can check .exists()
