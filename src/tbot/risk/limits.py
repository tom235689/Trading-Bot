"""Exposure limits on combined target weights."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class RiskLimits(BaseModel):
    """Backtest defaults are permissive; live configs should tighten them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_symbol_weight: float = Field(default=1.0, gt=0, le=1)
    max_gross_exposure: float = Field(default=1.0, gt=0)
    long_only: bool = True

    def apply(self, weights: Mapping[str, float]) -> dict[str, float]:
        """Clip each weight, then scale all down to the gross exposure cap."""
        low = 0.0 if self.long_only else -self.max_symbol_weight
        clipped = {s: min(max(w, low), self.max_symbol_weight) for s, w in weights.items()}
        gross = sum(abs(w) for w in clipped.values())
        if gross <= self.max_gross_exposure:
            return clipped
        scale = self.max_gross_exposure / gross
        return {s: w * scale for s, w in clipped.items()}
