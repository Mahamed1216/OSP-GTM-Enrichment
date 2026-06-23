"""Deterministic hiring-signal tier-uplift rules.

This is the pure, well-tested core of the rescue layer. The LLM classifier
*suggests* a `tier_uplift_recommendation`, but the actual uplift applied to a
Score row is always recomputed here from the lead's base tier/score and the
classified signal strength + relevant-role count — never trusted blindly.

Rules (from the feature spec):
  - HIGH signal:
      C -> B by default.
      C -> A when there are MULTIPLE highly-relevant high-weight roles AND
            base ICP fit is already decent.
      B -> A on the same "multiple relevant roles + decent fit" condition.
  - MEDIUM signal:
      C -> B only when base ICP fit is "not terrible".
  - LOW / NONE signal:
      no bump (signal is still stored).

Uplift only ever RAISES a tier. Tier "D" (the B2C / ICP-skip tier) is never
rescued — a confirmed poor fit stays out of the funnel.
"""
from __future__ import annotations

from dataclasses import dataclass

# High-weight roles. A match against this set is what makes a hiring signal
# "highly relevant" for the C->A / B->A jump. Matching is substring,
# case-insensitive, so "VP, Revenue Operations" matches "revenue operations".
HIGH_WEIGHT_ROLES: tuple[str, ...] = (
    "revops",
    "revenue operations",
    "sales operations",
    "sales ops",
    "sdr manager",
    "bdr manager",
    "sales development",
    "business development",
    "gtm operations",
    "go-to-market operations",
    "growth",
    "demand generation",
    "marketing operations",
    "ai automation",
    "sales automation",
    "data operations",
    "customer success operations",
    "implementation operations",
)

# "Decent" base fit needed before a HIGH signal can jump two tiers (C->A) or
# lift a B to A. "Not terrible" is the lower bar for a MEDIUM C->B bump.
DECENT_FIT_MIN_SCORE = 60
NOT_TERRIBLE_MIN_SCORE = 50

# How many high-weight roles count as "multiple highly relevant" roles.
MULTI_ROLE_THRESHOLD = 2

# Final tier produced by each applied uplift.
_UPLIFT_TARGET_TIER = {
    "C_to_B": "B",
    "C_to_A": "A",
    "B_to_A": "A",
}


def count_high_weight_roles(roles: list[str]) -> int:
    """Count roles that match the high-weight set (case-insensitive substring)."""
    n = 0
    for raw in roles or []:
        role = (raw or "").strip().lower()
        if not role:
            continue
        if any(key in role for key in HIGH_WEIGHT_ROLES):
            n += 1
    return n


def compute_tier_uplift(
    *,
    base_tier: str,
    base_score: int,
    strength: str,
    relevant_role_count: int,
) -> str:
    """Return the uplift recommendation: none | C_to_B | C_to_A | B_to_A.

    Pure function — no DB, no I/O. `relevant_role_count` is the number of
    high-weight roles found (see count_high_weight_roles).
    """
    tier = (base_tier or "").strip().upper()
    strength = (strength or "none").strip().lower()

    # Tier D is the ICP-skip tier — never rescue a confirmed poor fit.
    if tier not in ("A", "B", "C"):
        return "none"

    multi_relevant = relevant_role_count >= MULTI_ROLE_THRESHOLD

    if strength == "high":
        if tier == "C":
            if multi_relevant and base_score >= DECENT_FIT_MIN_SCORE:
                return "C_to_A"
            return "C_to_B"
        if tier == "B":
            if multi_relevant and base_score >= DECENT_FIT_MIN_SCORE:
                return "B_to_A"
            return "none"
        return "none"  # already A

    if strength == "medium":
        if tier == "C" and base_score >= NOT_TERRIBLE_MIN_SCORE:
            return "C_to_B"
        return "none"

    # low / none / unknown — store the signal, no bump.
    return "none"


@dataclass
class UpliftOutcome:
    recommendation: str        # none | C_to_B | C_to_A | B_to_A
    applied: bool              # True when the Score row tier actually changed
    new_tier: str
    new_score: int


def resolve_uplift(
    *,
    base_tier: str,
    base_score: int,
    strength: str,
    relevant_role_count: int,
    tier_a_min: int,
    tier_b_min: int,
) -> UpliftOutcome:
    """Compute the recommendation AND the resulting tier/score.

    The score is raised to the floor of the target tier's band so tier and
    score stay mutually consistent (a B-tier row never shows a sub-70 score).
    The original base_score is preserved on the LeadSignal for provenance.
    """
    rec = compute_tier_uplift(
        base_tier=base_tier,
        base_score=base_score,
        strength=strength,
        relevant_role_count=relevant_role_count,
    )
    if rec == "none":
        return UpliftOutcome(
            recommendation="none",
            applied=False,
            new_tier=(base_tier or "").strip().upper(),
            new_score=base_score,
        )
    target_tier = _UPLIFT_TARGET_TIER[rec]
    floor = tier_a_min if target_tier == "A" else tier_b_min
    new_score = max(base_score, floor)
    return UpliftOutcome(
        recommendation=rec,
        applied=True,
        new_tier=target_tier,
        new_score=new_score,
    )
