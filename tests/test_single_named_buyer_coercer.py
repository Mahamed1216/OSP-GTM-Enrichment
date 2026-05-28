"""Coverage for the Structure 1 single-named-buyer hybrid coercer.

The coercer fires when buyer discovery surfaces exactly one named buyer
account at medium-or-better confidence. The Zerve/NCAA case is the
canonical fixture — `likely_buyer_accounts = ["NCAA"]`, confidence
"medium", three noisy buyer segments — and proves the rewrite anchors
on NCAA instead of falling back to segment-only framing.
"""
from __future__ import annotations

from src.prompts.sanitize import (
    _clean_segment_to_industry,
    _extract_team_qualifier,
    coerce_structure_1_single_named_buyer,
)


# Fixture matching the user-reported Zerve / NCAA case.
ZERVE_NCAA_FIXTURE = {
    "lead_first_name": "Greg",
    "lead_company": "Zerve",
    "likely_buyer_accounts": ["NCAA"],
    "buyer_confidence": "medium",
    "buyer_segments": [
        "financial services analytics teams",
        "market research platforms",
        "vertical SaaS companies in analytics",
    ],
}


def _segment_only_body() -> str:
    """The kind of body the LLM produces when it ignores NCAA."""
    return (
        "Greg,\n\n"
        "Not sure if you're already working with financial services "
        "analytics teams, market research platforms, or vertical SaaS "
        "companies with data science functions? They seem like a great "
        "fit for what Zerve does.\n\n"
        "Happy to show you how we could get you in front of teams like "
        "that. Just let me know."
    )


def test_zerve_ncaa_body_includes_named_buyer():
    body = _segment_only_body()
    new_body, warning = coerce_structure_1_single_named_buyer(
        body,
        lead_company=ZERVE_NCAA_FIXTURE["lead_company"],
        named_buyer=ZERVE_NCAA_FIXTURE["likely_buyer_accounts"][0],
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    assert "NCAA" in new_body, (
        f"Coercer must include the named buyer in the rewritten body. "
        f"Got:\n{new_body}"
    )
    assert warning == (
        "Rewrote Structure 1 email to include single named buyer."
    )


def test_zerve_ncaa_preserves_greeting_and_company():
    new_body, _ = coerce_structure_1_single_named_buyer(
        _segment_only_body(),
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    # Greeting untouched on its own line.
    assert new_body.lstrip().startswith("Greg,\n")
    # Lead company echoed in the "great fit" line.
    assert "what Zerve does" in new_body


def test_zerve_ncaa_uses_hybrid_intro_and_cta():
    new_body, _ = coerce_structure_1_single_named_buyer(
        _segment_only_body(),
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    # Hybrid intro: "teams at <Named>, or similar [<qualifier>] teams in <A> and <B>"
    assert "teams at NCAA" in new_body
    assert "similar" in new_body
    # Industry stems recovered from the noisy segment strings.
    assert "financial services" in new_body
    assert "market research" in new_body
    # Hybrid CTA verbatim (spec point 3).
    assert (
        "Happy to show you how we could get you in front of teams like "
        "that. Just let me know."
    ) in new_body


def test_zerve_ncaa_drops_noisy_segment_tails():
    """Noisy structural suffixes ('platforms', 'companies in analytics')
    must not survive verbatim — the hybrid intro should read with the
    cleaned industry stems, not the raw segment strings."""
    new_body, _ = coerce_structure_1_single_named_buyer(
        _segment_only_body(),
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    intro_line = next(
        line for line in new_body.splitlines() if "Not sure if you're" in line
    )
    # "platforms" was a trailing structural noun on "market research
    # platforms" and must be stripped.
    assert "platforms" not in intro_line
    # "vertical SaaS companies" should not survive as a literal segment.
    assert "vertical SaaS" not in intro_line
    assert "companies in analytics" not in intro_line


def test_clean_segment_recovers_industry_stems():
    assert _clean_segment_to_industry(
        "financial services analytics teams"
    ) == "financial services"
    assert _clean_segment_to_industry(
        "market research platforms"
    ) == "market research"
    # "vertical SaaS companies in analytics" -> the lead-stripper removes
    # the "vertical SaaS ... in" prefix and leaves the bare term.
    assert _clean_segment_to_industry(
        "vertical SaaS companies in analytics"
    ) == "analytics"
    assert _clean_segment_to_industry(
        "healthcare organizations"
    ) == "healthcare"


def test_extract_team_qualifier_finds_shared_term():
    # "analytics" appears in 2 of 3 segments.
    assert _extract_team_qualifier(
        ZERVE_NCAA_FIXTURE["buyer_segments"]
    ) == "analytics"


def test_no_op_when_body_already_mentions_named_buyer():
    body = (
        "Greg,\n\n"
        "Not sure if you're already working with teams at NCAA, or "
        "similar analytics teams in financial services and market "
        "research? They seem like a great fit for what Zerve does.\n\n"
        "Happy to show you how we could get you in front of teams like "
        "that. Just let me know."
    )
    new_body, warning = coerce_structure_1_single_named_buyer(
        body,
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    assert new_body == body
    assert warning is None


def test_no_op_when_fewer_than_two_segments():
    new_body, warning = coerce_structure_1_single_named_buyer(
        _segment_only_body(),
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=["financial services analytics teams"],
    )
    assert warning is None
    assert "NCAA" not in new_body


def test_no_op_on_structure_2_body():
    body = (
        "Greg,\n\n"
        "Instead of hiring your founding SDR, I can basically guarantee "
        "you more results without the onboarding time.\n\n"
        "We have SDRs already trained selling to your buyers. All US "
        "based. Want to meet one of them?"
    )
    new_body, warning = coerce_structure_1_single_named_buyer(
        body,
        lead_company="Zerve",
        named_buyer="NCAA",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    assert new_body == body
    assert warning is None


def test_no_op_when_named_buyer_blank():
    new_body, warning = coerce_structure_1_single_named_buyer(
        _segment_only_body(),
        lead_company="Zerve",
        named_buyer="",
        segments=ZERVE_NCAA_FIXTURE["buyer_segments"],
    )
    assert new_body == _segment_only_body()
    assert warning is None
