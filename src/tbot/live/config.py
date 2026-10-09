"""Paper and live session configuration (YAML) and secrets (environment)."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tbot.core.config import TradingConfig
from tbot.risk.guard import GuardConfig


class SessionConfig(TradingConfig):
    ledger: Path
    guard: GuardConfig = GuardConfig()
    heartbeat_seconds: int = Field(default=300, ge=30)
    stale_after_seconds: int = Field(default=600, ge=60)  # bar overdue by this much: alert
    batch_wait_seconds: float = Field(default=5.0, ge=0)  # wait for streams closing together
    summary_hour_utc: int = Field(default=0, ge=0, le=23)
    backup_days: int = Field(default=14, ge=0)  # daily ledger copies kept; 0 turns them off
    # Answer /status and /fills from the Telegram chat. One session per bot token may poll.
    telegram_commands: bool = False


class PaperConfig(SessionConfig):
    ledger: Path = Path("data/paper.sqlite")


class LiveConfig(SessionConfig):
    mode: Literal["testnet", "live"]
    ledger: Path = Path("data/live.sqlite")  # data/testnet.sqlite in testnet mode
    # budget: the bot owns initial_cash and what it buys; other balances are left alone.
    # account: the bot owns the whole spot account (use only for a dedicated account).
    ownership: Literal["budget", "account"] = "budget"
    protective_stop_pct: float = Field(default=0.2, ge=0, lt=1)  # 0 disables exchange stops
    reconcile_seconds: int = Field(default=300, ge=30)
    reconcile_tolerance: float = Field(default=0.002, ge=0)  # relative mismatch that is noise
    recv_window: int = Field(default=5000, ge=1000, le=60000)

    @model_validator(mode="before")
    @classmethod
    def ledger_per_mode(cls, data: Any) -> Any:
        if isinstance(data, dict) and "ledger" not in data and data.get("mode") == "testnet":
            return {**data, "ledger": Path("data/testnet.sqlite")}
        return data


def load_paper_config(path: Path) -> PaperConfig:
    return PaperConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_live_config(path: Path) -> LiveConfig:
    return LiveConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Settings(BaseSettings):
    """Secrets and endpoints from the environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="TBOT_", env_file=".env", extra="ignore")

    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    heartbeat_url: str | None = None
    binance_api_key: str | None = None
    binance_api_secret: str | None = None


def save_settings(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file and keep everything else.

    A missing file starts from .env.example next to it. The file is replaced in one step.
    """
    template = path.with_name(".env.example")
    source = path if path.exists() else template if template.exists() else None
    text = source.read_bytes().decode("utf-8-sig") if source else ""  # as is: CRLF stays
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for key, value in values.items():
        line = f"{key}={value}"
        found = [i for i, old in enumerate(lines) if old.split("=", 1)[0].strip() == key]
        for i in found:  # every copy: the last one is the one that counts
            lines[i] = line
        if not found:
            lines.append(line)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes((newline.join(lines) + newline).encode("utf-8"))
    partial.replace(path)
