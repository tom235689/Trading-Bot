"""Plain-text validation report."""

from tbot.backtest.metrics import Metrics
from tbot.backtest.report import MIN_RATIO_DAYS
from tbot.research.montecarlo import MonteCarloSummary
from tbot.research.sweep import sweep_table
from tbot.research.validate import GateCheck, ValidationReport

TOP_ROWS = 10
MANY_LOOKS = 3  # a holdout seen more often than this is warned about


def _line(label: str, m: Metrics) -> str:
    short = m.years * 365.25 < MIN_RATIO_DAYS  # annualized ratios say nothing, as in backtest
    cagr = f"{'n/a':>7}" if short else f"{m.cagr:7.1%}"
    sharpe = f"{'n/a':>5}" if short else f"{m.sharpe:5.2f}"
    return (
        f"{label:<14}CAGR {cagr}  Sharpe {sharpe}  MaxDD {m.max_drawdown:7.1%}  "
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
    lines.append(
        f"4. Walk-forward: train {wf.train_months}m, test {wf.test_months}m, "
        f"pick {r.config.selection} {objective}"
    )
    for w in r.walk_forward.windows:
        m = w.test_metrics
        lines.append(
            f"   test {w.window.train_end} -> {w.window.test_end}: {_params(w.params)}"
            f"  train {objective} {w.train_objective:.2f}  test Sharpe {m.sharpe:5.2f}  "
            f"return {m.total_return:+7.1%}  trades {m.trades}"
        )
    lines.append("   " + _line("stitched OOS", r.oos))
    if r.holding is not None:  # the same symbols and risk settings, held all along
        lines.append("   " + _line("holding them", r.holding))
    lines.append(
        "   (segments start flat: what one holds at its end counts at the last close, unsold)"
    )
    lines.append("")

    mc = r.monte_carlo
    lines.extend(
        [
            f"5. Monte Carlo: {mc.runs} runs on blocks of bar returns",
            *_monte_carlo_lines("in-sample", mc),
            *_monte_carlo_lines("out-of-sample", r.monte_carlo_oos),
            "",
            f"6. Deflated Sharpe: {r.deflated.trials} logged trials; luck alone would reach "
            f"Sharpe {r.deflated.expected_max_sharpe:.2f}; P(true Sharpe > 0) = "
            f"{r.deflated.probability:.1%}",
            "",
            *_holdout_lines(r),
            "",
            "8. Gate",
        ]
    )
    for check in r.gate:
        lines.append(f"   {'PASS' if check.passed else 'FAIL'}  {check.name}: {_values(check)}")
    if r.holdout is None:
        lines.append("   SKIP  holdout return: not evaluated (see 7)")
    failed = [check.name for check in r.gate if not check.passed]
    if r.passed:
        lines.append("   PASSED: it held up out of sample; paper trade it before real money")
    elif failed == ["holdout length"]:
        lines.append(
            "   FAILED: the holdout is too short to confirm it; do not trade it with money you need"
        )
    else:
        lines.append(
            "   FAILED: it did not hold up out of sample; do not trade it with money you need"
        )
    return "\n".join(lines)


def _holdout_lines(r: ValidationReport) -> list[str]:
    looks = r.holdout_evaluations
    seen = f"seen {looks} time(s) so far, counting every backtest over it"
    if r.holdout is None:
        lines = [
            "7. Holdout (baseline params): not evaluated, since the checks before it failed",
            f"   It was {seen}; every look weakens it.",
            "   `tbot validate <config> --holdout` evaluates it anyway.",
        ]
    else:
        lines = [f"7. Holdout (baseline params), {seen}", "   " + _line("", r.holdout)]
    if looks > MANY_LOOKS:
        lines.append(
            f"   Warning: {looks} looks; it is no longer unseen data, since what changed "
            "after a look was fitted to it too"
        )
    return lines


def _values(check: GateCheck) -> str:
    if check.name == "deflated Sharpe":  # a probability
        return f"{check.value:.1%} vs {check.threshold:.0%}"
    if check.name == "holdout length":
        short = "" if check.passed else ": too short"
        return f"{int(check.value)} days vs {check.threshold:g}{short}"
    if "return" in check.name or "drawdown" in check.name:  # fractions, shown as percent
        return f"{check.value:+.1%} vs {check.threshold:+.1%}"
    return f"{check.value:.3g} vs {check.threshold:.3g}"


def _monte_carlo_lines(label: str, mc: MonteCarloSummary) -> list[str]:
    return [
        f"   {label} ({mc.days} days, {mc.block_days}-day blocks): "
        f"max drawdown p5 {mc.drawdown_p5:.1%}  "
        f"p50 {mc.drawdown_p50:.1%}  p95 {mc.drawdown_p95:.1%}",
        f"      P(drawdown beyond {mc.drawdown_limit:.0%}) = {mc.prob_drawdown_beyond:.1%}, "
        f"beyond the {mc.kill_switch:.0%} kill switch = {mc.prob_kill:.1%}; final return "
        f"p5 {mc.return_p5:+.1%}  p50 {mc.return_p50:+.1%}  p95 {mc.return_p95:+.1%}",
    ]


def _grid_lines(r: ValidationReport) -> list[str]:
    """Objective over the grid: a table for two parameters, top rows otherwise."""
    grid = r.config.grid
    objective = r.config.objective
    table = sweep_table(r.sweep_runs, objective)
    if len(grid) != 2:
        columns = [*grid, objective, "max_drawdown", "trades"]
        rows = table.head(TOP_ROWS).select(*columns).rows()
        cells = [columns] + [
            [f"{v:.2f}" if isinstance(v, float) else str(v) for v in row] for row in rows
        ]
        widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
        return [
            "   " + "  ".join(cell.rjust(width) for cell, width in zip(row, widths, strict=True))
            for row in cells
        ]
    (row_name, row_values), (col_name, col_values) = grid.items()
    lookup = {(row[row_name], row[col_name]): row[objective] for row in table.rows(named=True)}
    header = f"   {row_name + chr(92) + col_name:>10}" + "".join(f"{c:>8}" for c in col_values)
    body = [
        f"   {rv!s:>10}" + "".join(f"{lookup[(rv, cv)]:8.2f}" for cv in col_values)
        for rv in row_values
    ]
    return [header, *body]
