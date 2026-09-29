from datetime import UTC, datetime

import numpy as np
import pydantic
import pytest

from tbot.strategies.base import BarWindow, StrategyContext
from tbot.strategies.registry import create_strategy
from tbot.strategies.rsi import RsiReversion, rsi

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_rsi_hand_calculation() -> None:
    # Period 2 over closes 10, 11, 10, 12, 11: changes +1, -1, +2, -1.
    # Seed: avg gain 0.5, avg loss 0.5. Then Wilder: gain (0.5 + 2) / 2 = 1.25, loss 0.25;
    # then gain 0.625, loss (0.25 + 1) / 2 = 0.625 -> RS 1 -> RSI 50.
    assert rsi(np.array([10.0, 11.0, 10.0, 12.0, 11.0]), 2) == pytest.approx(50.0)
    assert rsi(np.array([1.0, 2.0, 3.0, 4.0]), 2) == 100.0  # no losses
    assert rsi(np.array([4.0, 3.0, 2.0, 1.0]), 2) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="not enough"):
        rsi(np.array([1.0, 2.0]), 2)


def window(closes: list[float]) -> BarWindow:
    values = np.array(closes, dtype=np.float64)
    times = np.arange(len(values)).astype("datetime64[h]").astype("datetime64[ms]")
    return BarWindow(times, values, values, values, values, np.ones(len(values)))


def targets_for(strategy: RsiReversion, closes: list[float]) -> list[float]:
    out = []
    for n in range(1, len(closes) + 1):
        ctx = StrategyContext(T0, {"BTC": window(closes[:n])}, {})
        out.append(strategy.on_bar(ctx)["BTC"])
    return out


def test_enters_oversold_and_exits_overbought() -> None:
    strategy = RsiReversion(["BTC"], {"period": 2, "low": 30, "high": 70})
    assert strategy.warmup == 21
    flat = [100.0] * 21
    falling = [100.0 - 2 * i for i in range(1, 6)]  # RSI heads to 0: entry
    rising = [90.0 + 3 * i for i in range(1, 6)]  # RSI heads to 100: exit
    targets = targets_for(strategy, flat + falling + rising)
    assert targets[:21] == [0.0] * 21  # warming up
    assert targets[21 + 4] == 1.0  # long after the fall
    assert targets[-1] == 0.0  # flat after the rise


def test_trend_filter_blocks_entries_below_the_average() -> None:
    strategy = RsiReversion(["BTC"], {"period": 2, "low": 30, "high": 70, "trend_period": 30})
    closes = [100.0] * 30 + [100.0 - 2 * i for i in range(1, 8)]  # falling below the SMA
    assert targets_for(strategy, closes)[-1] == 0.0


def test_registry_and_params() -> None:
    assert isinstance(create_strategy("rsi_reversion", ["BTC"], {"period": 5}), RsiReversion)
    with pytest.raises(pydantic.ValidationError):
        RsiReversion(["BTC"], {"low": 80, "high": 20})
