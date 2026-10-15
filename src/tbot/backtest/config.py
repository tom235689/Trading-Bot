"""Backtest configuration loaded from YAML."""

from datetime import date
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, model_validator

from tbot.core.config import StrategyConfig, TradingConfig, load_yaml
from tbot.risk.guard import GuardConfig

__all__ = ["BacktestConfig", "StrategyConfig", "load_config"]


class BacktestConfig(TradingConfig):
    start: date
    end: date | None = None  # exclusive; None runs to the latest stored bar
    guard: GuardConfig | None = None  # the session guard; None trades without one
    # Live's exchange stop this far below the last close; 0 (the default) simulates none.
    protective_stop_pct: float = Field(default=0.0, ge=0, lt=1)

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
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # a validation's base file: say which file it is
        raise ValueError(f"{path}: not valid YAML: {exc}") from None
    return BacktestConfig.model_validate(raw)
