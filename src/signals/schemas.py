"""Pydantic schema for the hiring-signal LLM classifier output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SignalStrength = Literal["none", "low", "medium", "high"]
RecencyEstimate = Literal["last_30_days", "last_90_days", "last_180_days", "unknown"]
TierUplift = Literal["none", "C_to_B", "C_to_A", "B_to_A"]

# Departments the classifier is allowed to bucket roles into. Free-form
# strings are tolerated by the schema (list[str]) but the prompt steers the
# model toward this vocabulary so the UI filters stay predictable.
DEPARTMENTS = (
    "sales", "revenue", "GTM", "RevOps", "operations", "marketing",
    "data", "AI", "support", "customer success", "engineering", "other",
)


class HiringSignalResult(BaseModel):
    """Structured hiring-signal classification.

    `tier_uplift_recommendation` is the model's suggestion; the deterministic
    rules in src.signals.uplift override it before anything is applied to a
    Score row (same pattern as buyer_accounts._compute_buyer_fallback_mode).
    """

    hiring_signal_found: bool = False
    hiring_signal_strength: SignalStrength = "none"
    relevant_roles_found: list[str] = Field(default_factory=list)
    relevant_departments: list[str] = Field(default_factory=list)
    recency_estimate: RecencyEstimate = "unknown"
    why_it_matters: str = ""
    tier_uplift_recommendation: TierUplift = "none"
    recommended_email_angle: str = ""
    source_urls: list[str] = Field(default_factory=list)
