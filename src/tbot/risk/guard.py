"""Portfolio-level risk guard: daily loss limit, drawdown kill switch, stale data, and a
turnover limit."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

TURNOVER_WINDOW = timedelta(hours=24)


class GuardConfig(BaseModel):
    """Zero disables a rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_loss_limit: float = Field(default=0.03, ge=0, lt=1)  # while below day open: no entries
    # Below the peak by this much: flatten and halt. Set it beyond the strategy's normal
    # drawdowns (validation's Monte Carlo); 0.45 suits the shipped Donchian settings.
    max_drawdown: float = Field(default=0.45, ge=0, lt=1)
    stale_seconds: float = Field(default=900, ge=0)  # bar older than this: skip the event
    # Traded in the last 24 hours beyond this many times equity: no entries until it falls
    # back. The shipped strategies stay near 1 on 2018-2026 history; a fault that trades at
    # every bar (a strategy flipping, a book bought back after each sale) does not.
    max_daily_turnover: float = Field(default=3.0, ge=0, allow_inf_nan=False)


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
    rule: str = ""  # halted, drawdown, stale, daily_loss, or turnover


class RiskGuard:
    def __init__(self, config: GuardConfig, state: GuardState | None = None) -> None:
        self.config = config
        self.state = state or GuardState()

    def check(
        self, now: datetime, equity: float, bar_time: datetime, traded: float = 0.0
    ) -> Decision:
        """Update peak and day-open equity, then decide what this event may do. `now` is
        when the event is handled, `bar_time` the close it brings; `traded` is the value of
        the fills of the last 24 hours, in the quote asset."""
        state, cfg = self.state, self.config
        # Days follow the bars, not the clock: a catch-up after a sleep handles yesterday's
        # last bars today. The first event of a day closes the last bar of the day before:
        # its loss counts against that day's open (else a 1d bar's loss is never seen).
        today = bar_time.date()
        day_open = state.day_open_equity
        if state.day is None or today > state.day:
            if state.day is None or (today - state.day).days > 1:
                day_open = equity  # no day before, or one long gone: nothing to test
            state.day = today
            state.day_open_equity = equity
        state.peak_equity = max(state.peak_equity, equity)

        if state.halted:
            return Decision(Mode.HALT, state.halt_reason, "halted")
        positive = state.peak_equity > 0  # a book emptied by a transfer has no drawdown
        if cfg.max_drawdown and positive and equity < state.peak_equity * (1 - cfg.max_drawdown):
            drawdown = equity / state.peak_equity - 1
            self.halt(f"drawdown {drawdown:.1%} beyond {_percent(cfg.max_drawdown)} limit")
            return Decision(Mode.HALT, state.halt_reason, "drawdown")
        if cfg.stale_seconds and (now - bar_time).total_seconds() > cfg.stale_seconds:
            age = (now - bar_time).total_seconds()
            return Decision(Mode.BLOCK, f"bar is {age:.0f}s old", "stale")
        if cfg.daily_loss_limit and day_open > 0 and equity < day_open * (1 - cfg.daily_loss_limit):
            loss = equity / day_open - 1
            return Decision(
                Mode.REDUCE_ONLY,
                f"day loss {loss:.1%} beyond {_percent(cfg.daily_loss_limit)}",
                "daily_loss",
            )
        if cfg.max_daily_turnover and equity > 0 and traded > equity * cfg.max_daily_turnover:
            return Decision(
                Mode.REDUCE_ONLY,
                f"traded {traded / equity:.1f}x equity in 24 hours, beyond the "
                f"{cfg.max_daily_turnover:g}x limit (guard.max_daily_turnover): a strategy "
                "that trades this much may be broken",
                "turnover",
            )
        return Decision(Mode.NORMAL)

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def shift(self, amount: float, equity: float) -> None:
        """Rescale the reference levels for money that entered or left the book from outside
        (deposits, withdrawals, manual trades), so it is no profit or loss: a drawdown stays
        the same in percent. Moving them by the amount would turn a withdrawal during a
        drawdown into a deeper drawdown, and trip the kill switch. `equity` is the book's
        value before the transfer."""
        state = self.state
        if equity > 0:
            factor = max(equity + amount, 0.0) / equity
            state.peak_equity *= factor
            state.day_open_equity *= factor
            return
        if state.peak_equity:  # nothing to scale from
            state.peak_equity = max(state.peak_equity + amount, 0.0)
        if state.day is not None:
            state.day_open_equity = max(state.day_open_equity + amount, 0.0)

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


def _percent(fraction: float) -> str:
    return f"{fraction * 100:g}%"
