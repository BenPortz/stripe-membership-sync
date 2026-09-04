"""Application configuration loaded from environment and config.yaml."""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache

import yaml
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Environment-driven settings with validation."""

    # Stripe
    stripe_webhook_secret: str = Field(..., description="Stripe webhook signing secret (whsec_...)")

    # WordPress
    wordpress_url: str = Field(..., description="WordPress site URL (https://yoursite.com)")
    wordpress_username: str = Field(..., description="WordPress admin username")
    wordpress_app_password: str = Field(..., description="WordPress Application Password")

    # PMPro
    pmpro_default_level_id: int = Field(1, description="Default PMPro membership level ID")

    # App
    environment: str = Field("development", description="development | staging | production")
    log_level: str = Field("INFO", description="Logging level")
    database_url: str = Field(
        "sqlite+aiosqlite:///./events.db",
        description="Database connection string",
    )
    api_key: str = Field(
        ...,
        min_length=16,
        description="API key for the admin endpoints. Required.",
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_membership_map(path: str = "config.yaml") -> dict:
    """Load the Stripe price to PMPro level mapping from YAML config."""
    config_path = Path(path)
    if not config_path.exists():
        return {"membership_map": [], "default_level_id": 1, "auto_create_users": True}

    with open(config_path) as f:
        config = yaml.safe_load(f)

    return config


def get_level_for_price(price_id: str, config: dict | None = None) -> dict | None:
    """Look up the PMPro level for a given Stripe price ID.

    Returns a dict with pmpro_level_id, level_name and duration, or None
    if no mapping is found.
    """
    if config is None:
        config = load_membership_map()

    for mapping in config.get("membership_map", []):
        if mapping["stripe_price_id"] == price_id:
            return {
                "pmpro_level_id": mapping["pmpro_level_id"],
                "level_name": mapping["level_name"],
                "duration": mapping.get("duration", "month"),
            }

    # Fall back to default level
    default_id = config.get("default_level_id", 1)
    return {
        "pmpro_level_id": default_id,
        "level_name": "Default",
        "duration": "month",
    }
