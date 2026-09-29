"""Portfolio-level risk guard: daily loss limit, drawdown kill switch, stale data."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GuardConfig(BaseModel):
    """Zero disables a rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_loss_limit: float = Field(default=0.03, ge=0, lt=1)  # below day open: no new entries
    max_drawdown: float = Field(default=0.15, ge=0, lt=1)  # below peak: flatten and halt
    stale_seconds: float = Field(default=900, ge=0)  # bar older than this: skip the event


class Mode(StrEnum):
    NORMAL = "normal"
    REDUCE_ONLY = "reduce_only"  # only orders that shrink a position
    BLOCK = "block"  # no orders this event
    HALT = "halt"  # flatten everything, then no orders until resumed


class GuardState(BaseModel):
    """Persisted between runs so a restart cannot reset the kill switch."""

    model_config = ConfigDict(extra="forbid")

    peak_equity: float = 0.0
    day: date | None = None
    day_open_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""


@dataclass(frozen=True)
class Decision:
    mode: Mode
    reason: str = ""


class RiskGuard:
    def __init__(self, config: GuardConfig, state: GuardState | None = None) -> None:
        self.config = config
        self.state = state or GuardState()

    def check(self, now: datetime, equity: float, bar_time: datetime) -> Decision:
        """Update peak and day-open equity, then decide what this event may do."""
        state, cfg = self.state, self.config
        if state.day != now.date():
            state.day = now.date()
            state.day_open_equity = equity
        state.peak_equity = max(state.peak_equity, equity)

        if state.halted:
            return Decision(Mode.HALT, state.halt_reason)
        if cfg.max_drawdown and equity < state.peak_equity * (1 - cfg.max_drawdown):
            drawdown = equity / state.peak_equity - 1
            self.halt(f"drawdown {drawdown:.1%} beyond {cfg.max_drawdown:.0%} limit")
            return Decision(Mode.HALT, state.halt_reason)
        if cfg.stale_seconds and (now - bar_time).total_seconds() > cfg.stale_seconds:
            age = (now - bar_time).total_seconds()
            return Decision(Mode.BLOCK, f"bar is {age:.0f}s old")
        if cfg.daily_loss_limit and equity < state.day_open_equity * (1 - cfg.daily_loss_limit):
            loss = equity / state.day_open_equity - 1
            return Decision(
                Mode.REDUCE_ONLY, f"day loss {loss:.1%} beyond {cfg.daily_loss_limit:.0%}"
            )
        return Decision(Mode.NORMAL)

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        """Human action: clear the kill switch and start the drawdown count from here."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.peak_equity = 0.0

    @staticmethod
    def filter_orders(
        orders: Mapping[str, float], positions: Mapping[str, float], mode: Mode
    ) -> dict[str, float]:
        if mode == Mode.NORMAL:
            return dict(orders)
        if mode != Mode.REDUCE_ONLY:
            return {}
        allowed = {}
        for symbol, quantity in orders.items():
            position = positions.get(symbol, 0.0)
            if position and (quantity > 0) != (position > 0):
                allowed[symbol] = (
                    max(quantity, -position) if position > 0 else min(quantity, -position)
                )
        return allowed

    @staticmethod
    def flatten_orders(positions: Mapping[str, float]) -> dict[str, float]:
        return {symbol: -quantity for symbol, quantity in positions.items() if quantity}
