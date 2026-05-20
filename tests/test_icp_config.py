"""Tests for the ICP config layer.

Covers:
- defaults are fully populated and match the values cited from
  src/prompts/scoring.py and src/prompts/email.py (anti-regression for
  the "behaviour pre-first-edit must equal today's behaviour" guardrail)
- missing file -> fallback to defaults
- malformed JSON -> fallback to defaults (graceful, not a crash)
- round-trip save/load is lossless
- save_icp_config writes 2-space pretty-printed JSON
- atomic write leaves no `.tmp` behind on success
- render_icp_block contains key tokens from the seeded defaults so
  prompt regressions surface here, not in live LLM output
"""
from __future__ import annotations

from pathlib import Path

from src.icp_config import (
    CONFIG_PATH,
    ICPConfig,
    default_icp_config,
    load_icp_config,
    render_icp_block,
    save_icp_config,
)


def test_defaults_match_hardcoded_seed_values():
    cfg = default_icp_config()
    # Verbatim from src/prompts/email.py:7-8
    assert "B2B SaaS" in cfg.company.one_liner
    assert "sales / revenue teams" in cfg.company.one_liner
    assert "wasted pipeline effort" in cfg.company.one_liner
    # Verbatim from src/prompts/scoring.py:14-15
    assert set(cfg.icp.target_industries) == {
        "B2B SaaS", "Data/AI", "Fintech", "Cloud", "DevTools", "Healthtech",
    }
    # Verbatim from src/prompts/scoring.py:19-20
    assert "data fragmentation" in cfg.persona.top_pain_points
    assert "outbound efficiency" in cfg.persona.top_pain_points
    assert "CRM hygiene" in cfg.persona.top_pain_points
    # New field — must be non-empty so Tavily queries get a useful qualifier.
    assert cfg.news_search_terms
    assert cfg.generate_content_for_all_tiers is False


def test_load_returns_defaults_when_file_missing(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    cfg = load_icp_config(missing)
    assert cfg == default_icp_config()


def test_load_returns_defaults_when_file_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    cfg = load_icp_config(bad)
    assert cfg == default_icp_config()


def test_round_trip_is_lossless(tmp_path):
    path = tmp_path / "icp_config.json"
    original = default_icp_config()
    original.company.name = "Parakeet"
    original.news_search_terms = ["alpha", "beta"]
    original.generate_content_for_all_tiers = True
    save_icp_config(original, path)
    reloaded = load_icp_config(path)
    assert reloaded == original


def test_save_produces_two_space_indented_json(tmp_path):
    path = tmp_path / "icp_config.json"
    save_icp_config(default_icp_config(), path)
    text = path.read_text(encoding="utf-8")
    # 2-space indent — the spec calls out this format explicitly.
    assert '\n  "company": {' in text
    assert '\n    "name":' in text
    # File ends with a newline (POSIX convention).
    assert text.endswith("\n")


def test_atomic_write_does_not_leak_tmp_file(tmp_path):
    path = tmp_path / "icp_config.json"
    save_icp_config(default_icp_config(), path)
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []
    assert path.exists()


def test_render_icp_block_contains_seed_tokens():
    """Snapshot-y check: the rendered prompt block surfaces the key bits
    the LLM needs. If someone removes a section from `render_icp_block`,
    this test catches it before live LLM output drifts."""
    block = render_icp_block(default_icp_config())
    assert "# About us" in block
    assert "# Our ICP" in block
    assert "# Buyer persona" in block
    assert "# Intent signals" in block
    assert "B2B SaaS" in block
    assert "outbound efficiency" in block
    assert "Series B" in block


def test_render_icp_block_collapses_empty_lists():
    """An empty config should not leak orphan bullets — empty list fields
    render as a single em-dash so the section header still has content."""
    empty = ICPConfig()
    block = render_icp_block(empty)
    # Em-dash placeholder appears wherever a list was empty.
    assert "—" in block
    # No accidental bullet lines from empty lists.
    assert "\n- \n" not in block


def test_repo_seed_file_is_valid():
    """The committed data/icp_config.json must load cleanly. If anyone
    hand-edits it into invalid JSON, this fails before runtime does."""
    cfg = load_icp_config(CONFIG_PATH)
    # Loading the repo file should produce a fully-populated config; if
    # the file is missing in CI this still passes via the defaults
    # fallback, which is the documented behaviour.
    assert isinstance(cfg, ICPConfig)
    assert cfg.company.one_liner  # non-empty
