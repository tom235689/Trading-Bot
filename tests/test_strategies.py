from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
import pydantic
import pytest

import tbot.strategies  # noqa: F401  # registers built-in strategies
from tbot.strategies.base import BarWindow, StrategyContext
from tbot.strategies.donchian import DonchianTrend
from tbot.strategies.registry import available_strategies, create_strategy, register_strategy

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def window(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> BarWindow:
    times = np.arange(len(closes)).astype("datetime64[h]").astype("datetime64[ms]")
    close = np.array(closes, dtype=np.float64)
    return BarWindow(
        open_time=times,
        open=close,
        high=np.array(highs, dtype=np.float64),
        low=np.array(lows, dtype=np.float64),
        close=close,
        volume=np.ones(len(closes)),
    )


def test_registry_builds_from_name() -> None:
    assert "donchian_trend" in available_strategies()
    strategy = create_strategy("donchian_trend", ["BTC"], {"entry": 10, "exit": 5})
    assert isinstance(strategy, DonchianTrend)
    assert strategy.warmup == 11


def test_registry_rejects_unknown_and_duplicate() -> None:
    with pytest.raises(ValueError, match="available"):
        create_strategy("nope", ["BTC"])
    with pytest.raises(ValueError, match="already registered"):
        register_strategy(DonchianTrend)


@pytest.mark.parametrize("params", [{"entry": 1}, {"unknown": 3}])
def test_donchian_rejects_bad_params(params: dict[str, int]) -> None:
    with pytest.raises(pydantic.ValidationError):
        DonchianTrend(["BTC"], params)


def test_donchian_enters_on_breakout_and_exits_on_breakdown() -> None:
    strategy = DonchianTrend(["BTC", "ETH"], {"entry": 3, "exit": 2})
    highs = [10, 11, 12, 11, 13, 13, 12, 11]
    lows = [9, 10, 11, 10, 12, 11, 11, 9]
    closes = [9.5, 10.5, 11.5, 10.5, 12.5, 12.2, 11.5, 9.5]
    flat_eth = window([1] * 8, [1] * 8, [1] * 8)
    targets = []
    for n in range(1, 9):
        btc = window(highs[:n], lows[:n], closes[:n])
        ctx = StrategyContext(T0, {"BTC": btc, "ETH": flat_eth}, {})
        targets.append(strategy.on_bar(ctx)["BTC"])
    # Bar 4 closes at 12.5, above the prior 3-bar high of 12.
    # Bar 7 closes at 9.5, below the prior 2-bar low of 11.
    assert targets == [0, 0, 0, 0, 0.5, 0.5, 0.5, 0]
