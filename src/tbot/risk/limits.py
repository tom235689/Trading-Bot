"""Exposure limits on combined target weights."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class RiskLimits(BaseModel):
    """Backtest defaults are permissive; live configs should tighten them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_symbol_weight: float = Field(default=1.0, gt=0, le=1)
    max_gross_exposure: float = Field(default=1.0, gt=0)
    long_only: bool = True
    target_volatility: float = Field(default=0.0, ge=0)  # annualized; 0 disables scaling
    volatility_lookback_days: int = Field(default=30, ge=1)

    def apply(
        self, weights: Mapping[str, float], scales: Mapping[str, float] | None = None
    ) -> dict[str, float]:
        """Scale weights by volatility, clip each, then scale all down to the gross cap."""
        scales = scales or {}
        low = 0.0 if self.long_only else -self.max_symbol_weight
        clipped = {
            s: min(max(w * scales.get(s, 1.0), low), self.max_symbol_weight)
            for s, w in weights.items()
        }
        gross = sum(abs(w) for w in clipped.values())
        if gross <= self.max_gross_exposure:
            return clipped
        scale = self.max_gross_exposure / gross
        return {s: w * scale for s, w in clipped.items()}
