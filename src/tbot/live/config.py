"""Paper trading configuration (YAML) and secrets (environment)."""

from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tbot.core.config import TradingConfig


class PaperConfig(TradingConfig):
    ledger: Path = Path("data/paper.sqlite")
    heartbeat_seconds: int = Field(default=300, ge=30)
    stale_after_seconds: int = Field(default=600, ge=60)  # bar overdue by this much: alert
    batch_wait_seconds: float = Field(default=5.0, ge=0)  # wait for streams closing together
    summary_hour_utc: int = Field(default=0, ge=0, le=23)


def load_paper_config(path: Path) -> PaperConfig:
    return PaperConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Settings(BaseSettings):
    """Secrets and endpoints from the environment or a .env file. All optional."""

    model_config = SettingsConfigDict(env_prefix="TBOT_", env_file=".env", extra="ignore")

    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    heartbeat_url: str | None = None
