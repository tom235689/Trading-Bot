"""Plain-text backtest report."""

from tbot.backtest.metrics import Metrics


def format_metrics(m: Metrics) -> str:
    return "\n".join(
        [
            f"Period        {m.start:%Y-%m-%d} -> {m.end:%Y-%m-%d} ({m.years:.2f} years)",
            f"Equity        {m.initial_equity:,.2f} -> {m.final_equity:,.2f} "
            f"({m.total_return:+.1%})",
            f"CAGR          {m.cagr:.1%}",
            f"Sharpe        {m.sharpe:.2f}    Sortino {m.sortino:.2f}",
            f"Max drawdown  {m.max_drawdown:.1%}   Calmar {m.calmar:.2f}",
            f"Exposure      {m.avg_exposure:.1%}   Turnover {m.turnover:.1f}x/year   "
            f"Fees {m.fees:,.2f}",
            f"Trades        {m.trades}   Win rate {m.win_rate:.1%}   "
            f"Profit factor {m.profit_factor:.2f}   Avg trade {m.avg_trade_return:+.2%}",
        ]
    )
