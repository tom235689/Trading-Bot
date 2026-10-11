"""Plain-text backtest report."""

from tbot.backtest.metrics import Metrics

MIN_RATIO_DAYS = 30  # annualized ratios of a shorter period say nothing


def format_metrics(m: Metrics) -> str:
    short = m.years * 365.25 < MIN_RATIO_DAYS
    na = f"n/a (under {MIN_RATIO_DAYS} days)"
    cagr = na if short else f"{m.cagr:.1%}"
    ratios = na if short else f"{m.sharpe:.2f}    Sortino {m.sortino:.2f}"
    calmar = "" if short else f"   Calmar {m.calmar:.2f}"
    win_rate = f"{m.win_rate:.1%}" if m.trades else "-"
    return "\n".join(
        [
            f"Period        {m.start:%Y-%m-%d} -> {m.end:%Y-%m-%d} ({m.years:.2f} years)",
            f"Equity        {m.initial_equity:,.2f} -> {m.final_equity:,.2f} "
            f"({m.total_return:+.1%})",
            f"CAGR          {cagr}",
            f"Sharpe        {ratios}",
            f"Max drawdown  {m.max_drawdown:.1%}{calmar}",
            f"Exposure      {m.avg_exposure:.1%}   Turnover {m.turnover:.1f}x/year   "
            f"Fees {m.fees:,.2f}",
            f"Trades        {m.trades}   Win rate {win_rate}   "
            f"Profit factor {m.profit_factor:.2f}   Avg trade {m.avg_trade_return:+.2%}",
        ]
    )
