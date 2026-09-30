"""Configuration shared by backtest, paper, and live modes."""

from collections.abc import Mapping
from typing import Any, Self

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


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_cash: float = Field(default=10_000.0, gt=0)
    costs: CostModel = CostModel()
    risk: RiskLimits = RiskLimits()
    rebalance: RebalanceRules = RebalanceRules()
    strategies: list[StrategyConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def check_allocations(self) -> Self:
        if sum(s.allocation for s in self.strategies) > 1 + 1e-9:
            raise ValueError("strategy allocations must sum to at most 1")
        return self

    def with_params(self, params: Mapping[str, Any]) -> Self:
        """Copy with these params set on the first strategy; the others keep their values."""
        merged = {**self.strategies[0].params, **params}
        first = self.strategies[0].model_copy(update={"params": merged})
        return self.model_copy(update={"strategies": [first, *self.strategies[1:]]})

    def with_cost_multiplier(self, multiplier: float) -> Self:
        costs = CostModel(
            fee_rate=self.costs.fee_rate * multiplier,
            slippage_bps=self.costs.slippage_bps * multiplier,
        )
        return self.model_copy(update={"costs": costs})
