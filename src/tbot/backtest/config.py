"""Backtest configuration loaded from YAML."""

from datetime import date
from pathlib import Path
from typing import Self

import yaml
from pydantic import model_validator

from tbot.core.config import StrategyConfig, TradingConfig
from tbot.risk.guard import GuardConfig

__all__ = ["BacktestConfig", "StrategyConfig", "load_config"]


class BacktestConfig(TradingConfig):
    start: date
    end: date | None = None  # exclusive; None runs to the latest stored bar
    guard: GuardConfig | None = None  # the session guard; None trades without one

    @model_validator(mode="after")
    def check_period(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    def with_period(self, start: date, end: date | None) -> Self:
        if end is not None and end <= start:
            raise ValueError("end must be after start")
        return self.model_copy(update={"start": start, "end": end})


def load_config(path: Path) -> BacktestConfig:
    return BacktestConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
