from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Tier = Literal["A", "B", "C", "D"]


class Settings(BaseSettings):
    # env_ignore_empty: an env var set to "" is treated as unset, so the field
    # default applies. Hosting dashboards (Vercel among them) save an
    # optional-but-blank variable as an empty string rather than omitting it,
    # and every non-str field then fails validation — `TIER_A_MIN=""` is not a
    # valid int, `SEND_MIN_TIER=""` is not a valid tier. Because Settings() is
    # instantiated at import time, that took down the whole backend.
    #
    # This is deliberately global rather than per-field: it protects the bool
    # and Path fields too, and any field added later.
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # Anthropic
    anthropic_api_key: str = ""
    scoring_model: str = "claude-opus-4-7"
    content_model: str = "claude-sonnet-4-6"

    # Apify
    apify_api_token: str = ""
    apify_actor_linkedin_profile: str = "dev_fusion/Linkedin-Profile-Scraper"
    apify_actor_company_details: str = "rigelbytes/linkedin-company-details"

    # Tavily
    tavily_api_key: str = ""

    # Instantly
    instantly_api_key: str = ""
    instantly_campaign_id: str = ""
    instantly_webhook_secret: str = ""

    # Email verification
    email_verifier: Literal["instantly", "neverbounce", "millionverifier"] = "instantly"
    neverbounce_api_key: str = ""
    millionverifier_api_key: str = ""

    # Tier thresholds
    tier_a_min: int = 85
    tier_b_min: int = 70
    send_min_tier: Tier = "B"


    # Storage
    database_url: str = "sqlite:///sdr.db"
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    # Rating identity (no auth — single hardcoded SDR for the demo)
    rater_id: str = "demo_sdr"

    def tier_for_score(self, score: int) -> Tier:
        if score >= self.tier_a_min:
            return "A"
        if score >= self.tier_b_min:
            return "B"
        return "C"

    def should_send(self, tier: Tier) -> bool:
        # "D" is the ICP-skip tier (e.g., confirmed B2C with no B2B
        # motion). It ranks below C so it's blocked from delivery under
        # every send_min_tier setting.
        rank = {"A": 3, "B": 2, "C": 1, "D": 0}
        return rank[tier] >= rank[self.send_min_tier]


settings = Settings()
