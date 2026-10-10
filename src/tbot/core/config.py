"""Configuration shared by backtest, paper, and live modes."""

from collections.abc import Mapping
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel
from tbot.risk.limits import RiskLimits


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML in which a key given twice is an error: else the last copy silently wins,
    and the `initial_cash: 500` in view is not the one in force."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[str] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            continue
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "in a mapping", node.start_mark, f"key {key!r} given twice", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_yaml(text: str) -> Any:
    """Parse a config file: safe YAML, every key once."""
    return yaml.load(text, Loader=_UniqueKeyLoader)


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
        normalized = [symbol.replace("/", "").upper() for symbol in symbols]
        if len(set(normalized)) < len(normalized):  # a repeat would take a share it never uses
            raise ValueError(f"a symbol is listed twice: {symbols}")
        return normalized


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
