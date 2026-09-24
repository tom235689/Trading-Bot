"""Performance metrics from a backtest result."""

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

from tbot.backtest.engine import BacktestResult

DAYS_PER_YEAR = 365  # crypto trades every day
SECONDS_PER_YEAR = 365.25 * 86400


@dataclass(frozen=True)
class Metrics:
    start: datetime
    end: datetime
    years: float
    initial_equity: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float  # from daily returns, annualized, zero risk-free rate
    sortino: float
    max_drawdown: float  # negative fraction
    calmar: float
    avg_exposure: float
    turnover: float  # traded notional per year / average equity
    fees: float
    trades: int
    win_rate: float
    profit_factor: float
    avg_trade_return: float


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else math.nan


def _profit_factor(gains: float, losses: float) -> float:
    if losses > 0:
        return gains / losses
    return math.inf if gains > 0 else math.nan


def compute_metrics(result: BacktestResult) -> Metrics:
    equity = result.equity
    if equity.is_empty():
        raise ValueError("backtest produced no equity records; check the period and data")
    initial = result.initial_cash
    values = equity["equity"].to_numpy()
    start, end = equity["time"][0], equity["time"][-1]
    years = (end - start).total_seconds() / SECONDS_PER_YEAR
    final = float(values[-1])

    daily = equity.group_by_dynamic("time", every="1d").agg(pl.col("equity").last())
    closes = np.concatenate([[initial], daily["equity"].to_numpy()])
    returns = np.diff(closes) / closes[:-1]
    annual = math.sqrt(DAYS_PER_YEAR)
    sharpe = sortino = math.nan
    if len(returns) >= 2:
        mean = float(returns.mean())
        sharpe = _ratio(mean, float(returns.std(ddof=1))) * annual
        sortino = _ratio(mean, math.sqrt(float(np.mean(np.minimum(returns, 0) ** 2)))) * annual

    path = np.concatenate([[initial], values])
    max_drawdown = float((path / np.maximum.accumulate(path) - 1).min())
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else math.nan

    pnl = result.trades["pnl"].to_numpy()
    trade_returns = result.trades["return"].to_numpy()
    notional = float((result.fills["quantity"].abs() * result.fills["price"]).sum())

    return Metrics(
        start=start,
        end=end,
        years=years,
        initial_equity=initial,
        final_equity=final,
        total_return=final / initial - 1,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        calmar=_ratio(cagr, -max_drawdown),
        avg_exposure=float(equity["exposure"].to_numpy().mean()),
        turnover=_ratio(notional / float(values.mean()), years),
        fees=float(result.fills["fee"].sum()),
        trades=len(pnl),
        win_rate=float((pnl > 0).mean()) if len(pnl) else math.nan,
        profit_factor=_profit_factor(float(pnl[pnl > 0].sum()), float(-pnl[pnl < 0].sum())),
        avg_trade_return=float(trade_returns.mean()) if len(pnl) else math.nan,
    )
