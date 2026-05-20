"""ICP configuration — user-editable JSON config that feeds scoring,
content generation, and Tavily news queries.

Singleton file at ``data/icp_config.json``. Loaded once per pipeline run
and threaded through downstream consumers so a mid-run save never
splits a single lead's outputs across two configs.

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

CONFIG_PATH = Path("data/icp_config.json")


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
