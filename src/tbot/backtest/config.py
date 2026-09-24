"""Backtest configuration loaded from YAML."""

from datetime import date
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel
from tbot.risk.limits import RiskLimits


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    symbols: list[str] = Field(min_length=1)
    timeframe: Timeframe
    allocation: float = Field(gt=0, le=1)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        return [symbol.replace("/", "").upper() for symbol in symbols]


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date | None = None  # exclusive; None runs to the latest stored bar
    initial_cash: float = Field(default=10_000.0, gt=0)
    costs: CostModel = CostModel()
    risk: RiskLimits = RiskLimits()
    rebalance: RebalanceRules = RebalanceRules()
    strategies: list[StrategyConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be after start")
        if sum(s.allocation for s in self.strategies) > 1 + 1e-9:
            raise ValueError("strategy allocations must sum to at most 1")
        return self


def load_config(path: Path) -> BacktestConfig:
    return BacktestConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
