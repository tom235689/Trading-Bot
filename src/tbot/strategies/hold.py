"""Buy and hold: the benchmark a strategy has to beat."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from tbot.strategies.base import Strategy, StrategyContext
from tbot.strategies.registry import register_strategy


class HoldParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@register_strategy
class Hold(Strategy):
    """Holds every symbol at an equal share all the time. Validation runs it next to the
    candidate: in a rising market, a strategy that is merely long looks good too."""

    name = "hold"

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        super().__init__(symbols)
        self.params = HoldParams.model_validate(params or {})

    @property
    def warmup(self) -> int:
        return 1

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        return dict.fromkeys(self.symbols, 1.0 / len(self.symbols))
