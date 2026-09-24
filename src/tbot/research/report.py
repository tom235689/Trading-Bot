"""Plain-text validation report."""

from tbot.backtest.metrics import Metrics
from tbot.research.sweep import sweep_table
from tbot.research.validate import ValidationReport

TOP_ROWS = 10


def _line(label: str, m: Metrics) -> str:
    return (
        f"{label:<14}CAGR {m.cagr:7.1%}  Sharpe {m.sharpe:5.2f}  MaxDD {m.max_drawdown:7.1%}  "
        f"Return {m.total_return:+8.1%}  Trades {m.trades:4d}"
    )


def _params(params: dict[str, object]) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def format_report(r: ValidationReport) -> str:
    strategy = r.base.strategies[0]
    objective = r.config.objective
    lines = [
        f"Validation of {strategy.name} on {', '.join(strategy.symbols)} {strategy.timeframe}",
        f"In-sample {r.base.start} -> {r.config.holdout_start}, holdout from "
        f"{r.config.holdout_start} to {r.base.end or 'latest'}",
        "",
        f"1. Baseline ({_params(strategy.params)}), in-sample",
        "   " + _line("", r.baseline),
        "",
        f"2. Cost stress x{r.config.cost_multiplier:g}",
        "   " + _line("", r.stress),
        "",
        f"3. Parameter sweep: {len(r.sweep_runs)} points, objective {objective}",
        f"   best {_params(r.best.params)}: {getattr(r.best.metrics, objective):.2f}, "
        f"neighborhood mean {r.plateau.best_neighborhood:.2f}",
    ]
    if r.plateau.rank:
        lines.append(
            f"   baseline rank {r.plateau.rank} of {len(r.sweep_runs)}, "
            f"neighborhood mean {r.plateau.neighborhood:.2f}"
        )
    lines.extend(_grid_lines(r))
    lines.append("")

    wf = r.config.walk_forward
    lines.append(f"4. Walk-forward: train {wf.train_months}m, test {wf.test_months}m")
    for w in r.walk_forward.windows:
        m = w.test_metrics
        lines.append(
            f"   test {w.window.train_end} -> {w.window.test_end}: {_params(w.params)}"
            f"  train {objective} {w.train_objective:.2f}  test Sharpe {m.sharpe:5.2f}  "
            f"return {m.total_return:+7.1%}  trades {m.trades}"
        )
    lines.append("   " + _line("stitched OOS", r.oos))
    lines.append("")

    mc = r.monte_carlo
    lines.extend(
        [
            f"5. Monte Carlo: {mc.runs} runs over {mc.trades} trades",
            f"   max drawdown p5 {mc.drawdown_p5:.1%}  p50 {mc.drawdown_p50:.1%}  "
            f"p95 {mc.drawdown_p95:.1%}; P(drawdown beyond {mc.drawdown_limit:.0%}) = "
            f"{mc.prob_drawdown_beyond:.1%}",
            *_return_lines(r),
            "",
            f"6. Deflated Sharpe: {r.deflated.trials} logged trials; luck alone would reach "
            f"Sharpe {r.deflated.expected_max_sharpe:.2f}; P(true Sharpe > 0) = "
            f"{r.deflated.probability:.1%}",
            "",
            f"7. Holdout (baseline params), evaluated {r.holdout_evaluations} time(s) so far",
            "   " + _line("", r.holdout),
            "",
            "8. Gate",
        ]
    )
    for check in r.gate:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"   {status}  {check.name}: {check.value:.3g} vs {check.threshold:.3g}")
    lines.append(f"   {'PASSED' if r.passed else 'FAILED'}")
    return "\n".join(lines)


def _return_lines(r: ValidationReport) -> list[str]:
    """Shuffling keeps the compounded return fixed; only bootstrapping spreads it."""
    mc = r.monte_carlo
    if not r.config.monte_carlo.replace:
        return []
    return [
        f"   final return p5 {mc.return_p5:+.1%}  p50 {mc.return_p50:+.1%}  "
        f"p95 {mc.return_p95:+.1%}"
    ]


def _grid_lines(r: ValidationReport) -> list[str]:
    """Objective over the grid: a table for two parameters, top rows otherwise."""
    grid = r.config.grid
    objective = r.config.objective
    table = sweep_table(r.sweep_runs, objective)
    if len(grid) != 2:
        rows = table.head(TOP_ROWS).select(*grid, objective, "max_drawdown", "trades").rows()
        return [
            "   " + "  ".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in row)
            for row in rows
        ]
    (row_name, row_values), (col_name, col_values) = grid.items()
    lookup = {(row[row_name], row[col_name]): row[objective] for row in table.rows(named=True)}
    header = f"   {row_name + chr(92) + col_name:>10}" + "".join(f"{c:>8}" for c in col_values)
    body = [
        f"   {rv!s:>10}" + "".join(f"{lookup[(rv, cv)]:8.2f}" for cv in col_values)
        for rv in row_values
    ]
    return [header, *body]
