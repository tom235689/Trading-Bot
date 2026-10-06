"""Compare a session ledger with a backtest of the same settings over the same period.

This is validation step 8: paper (or live) results must track the backtest. Differences
point at costs the backtest underestimates, downtime, or behaviour that differs from it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tbot.backtest.engine import BacktestResult
from tbot.backtest.runner import run_period
from tbot.core.models import Fill
from tbot.data.store import BarStore
from tbot.live.config import SessionConfig
from tbot.live.ledger import Ledger

MAX_EQUITY_GAP = 0.02  # larger gaps between session and backtest equity need a look
SHOWN_FILLS = 10


@dataclass(frozen=True)
class FillMatch:
    session: Fill
    backtest: Fill

    @property
    def extra_cost_bps(self) -> float:
        """How much worse the session price was, in basis points; negative is better."""
        side = 1.0 if self.session.quantity > 0 else -1.0
        return side * (self.session.price / self.backtest.price - 1) * 1e4


@dataclass(frozen=True)
class Comparison:
    start: datetime
    end: datetime  # last bar close compared
    bar_events: int
    missed: list[datetime]  # backtest bar events without a session snapshot
    session_equity: float
    backtest_equity: float
    initial_cash: float
    matched: list[FillMatch]
    session_only: list[Fill]
    backtest_only: list[Fill]
    pending: int  # session fills after the last bar the backtest can fill
    session_fees: float
    backtest_fees: float
    max_gap: float  # largest relative equity gap at shared bar closes
    adjustments: int
    slippage_bps: float
    flows: float = 0.0  # money reconciliation moved into the book, taken out of its equity
    stop_fills: int = 0  # protective stops that executed; the backtest has none

    @property
    def mean_extra_cost_bps(self) -> float | None:
        if not self.matched:
            return None
        return sum(m.extra_cost_bps for m in self.matched) / len(self.matched)

    def checks(self) -> list[str]:
        found = []
        if self.stop_fills:
            found.append(
                f"{self.stop_fills} protective stop fills: the backtest has no exchange stop, "
                "so the fills after them differ"
            )
        if self.missed:
            found.append(
                f"{len(self.missed)} bar events without a session snapshot, first "
                f"{self.missed[0]:%Y-%m-%d %H:%M}: the bot was down"
            )
        if self.session_only or self.backtest_only:
            found.append(
                f"fills differ: {len(self.session_only)} only in the session, "
                f"{len(self.backtest_only)} only in the backtest"
            )
        extra = self.mean_extra_cost_bps
        if extra is not None and extra > self.slippage_bps:
            found.append(
                f"fills cost {extra:.1f} bps more than the backtest on average; raise "
                "costs.slippage_bps and validate again"
            )
        if self.max_gap > MAX_EQUITY_GAP:
            found.append(f"equity drifted up to {self.max_gap:.1%} from the backtest")
        return found


def compare_session(config: SessionConfig, ledger: Ledger, store: BarStore) -> Comparison:
    points = ledger.equity_points()
    if not points:
        raise ValueError("the ledger has no bar events yet")
    start = points[0].time
    result = run_period(config, store, start, None, config.guard)

    # The backtest fills a decision at the next bar's open, so decisions at the last stored
    # close have no fill yet; compare up to the last decision both sides could execute.
    step = min(c.timeframe.delta for c in config.strategies)
    end = min(points[-1].time, _last_open(config, store))
    backtest_times = [t for t in result.equity["time"].to_list() if start <= t <= end]
    session_by_time = {p.time: p for p in points if p.time <= end}
    shared = [t for t in backtest_times if t in session_by_time]
    if not shared:
        raise ValueError(
            "no bar close is in both the session and the stored bars yet; run `tbot download`"
            " or let the session run longer"
        )

    # Deposits, withdrawals, and the startup reconcile of an account move money in or out;
    # valued when they happened and taken out, the rest is what trading did.
    flows = [
        (a.time, a.cash + a.quantity * _price_at(config, store, a.symbol, a.time))
        for a in ledger.adjustments()
    ]

    def traded(t: datetime) -> float:
        return session_by_time[t].equity - sum(value for at, value in flows if at <= t)

    backtest_equity = dict(zip(result.equity["time"], result.equity["equity"], strict=True))
    gaps = [abs(traded(t) / backtest_equity[t] - 1) for t in shared]
    fills = ledger.fills()
    stops = ledger.stop_fill_keys()
    in_period = [f for f in fills if start <= f.time < end + step]
    session_fills = [f for f in in_period if (_iso(f.time), f.symbol) not in stops]
    matched, session_only, backtest_only = match_fills(
        session_fills, [f for f in _fills(result) if f.time <= end], step
    )
    last = shared[-1]
    return Comparison(
        start=start,
        end=last,
        bar_events=len(backtest_times),
        missed=[t for t in backtest_times if t not in session_by_time],
        session_equity=traded(last),
        backtest_equity=backtest_equity[last],
        initial_cash=config.initial_cash,
        matched=matched,
        session_only=session_only,
        backtest_only=backtest_only,
        pending=sum(1 for f in fills if f.time >= end + step),
        session_fees=sum(f.fee for f in in_period),
        backtest_fees=sum(m.backtest.fee for m in matched) + sum(f.fee for f in backtest_only),
        max_gap=max(gaps),
        adjustments=len(flows),
        slippage_bps=config.costs.slippage_bps,
        flows=sum(value for at, value in flows if at <= last),
        stop_fills=len(in_period) - len(session_fills),
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _price_at(config: SessionConfig, store: BarStore, symbol: str, moment: datetime) -> float:
    """Close of the symbol's finest stream at or before moment (the next one if none)."""
    if not symbol:
        return 0.0  # a cash-only adjustment
    timeframe = min(
        (c.timeframe for c in config.strategies if symbol in c.symbols), key=lambda t: t.millis
    )
    bars = store.read(symbol, timeframe, moment - timeframe.delta * 50, moment + timeframe.delta)
    closed = bars.filter(bars["open_time"] + timeframe.delta <= moment)
    chosen = closed if not closed.is_empty() else bars
    return float(chosen["close"][-1 if not closed.is_empty() else 0]) if chosen.height else 0.0


def match_fills(
    session: Sequence[Fill], backtest: Sequence[Fill], tolerance: timedelta
) -> tuple[list[FillMatch], list[Fill], list[Fill]]:
    """Pair fills of the same symbol and side placed within tolerance of each other."""
    left = sorted(backtest, key=lambda f: f.time)
    matched, session_only = [], []
    for fill in sorted(session, key=lambda f: f.time):
        partner = next(
            (
                b
                for b in left
                if b.symbol == fill.symbol
                and (b.quantity > 0) == (fill.quantity > 0)
                and abs(b.time - fill.time) <= tolerance
            ),
            None,
        )
        if partner is None:
            session_only.append(fill)
        else:
            left.remove(partner)
            matched.append(FillMatch(fill, partner))
    return matched, session_only, left


def _fills(result: BacktestResult) -> list[Fill]:
    rows = result.fills.select("time", "symbol", "quantity", "price", "fee").iter_rows()
    return [Fill(t, s, q, p, f) for t, s, q, p, f in rows]


def _last_open(config: SessionConfig, store: BarStore) -> datetime:
    """Open of the last stored bar on every symbol's finest stream."""
    finest: dict[str, timedelta] = {}
    for c in config.strategies:
        for symbol in c.symbols:
            finest[symbol] = min(finest.get(symbol, c.timeframe.delta), c.timeframe.delta)
    times = []
    for c in config.strategies:
        for symbol in c.symbols:
            if c.timeframe.delta == finest[symbol]:
                last = store.last_open_time(symbol, c.timeframe)
                if last is None:
                    raise ValueError(f"no stored bars for {symbol} {c.timeframe}")
                times.append(last)
    return min(times)


def comparison_text(c: Comparison, ledger_path: str) -> str:
    session_return = c.session_equity / c.initial_cash - 1
    backtest_return = c.backtest_equity / c.initial_cash - 1
    lines = [
        f"Session {ledger_path} against a backtest of the same settings",
        f"period {c.start:%Y-%m-%d %H:%M} -> {c.end:%Y-%m-%d %H:%M} UTC, "
        f"{c.bar_events} bar events, {len(c.missed)} missed",
        f"{'':10}{'equity':>12}{'return':>10}{'fills':>7}{'fees':>10}",
        f"{'session':10}{c.session_equity:>12,.2f}{session_return:>+10.2%}"
        f"{len(c.matched) + len(c.session_only):>7}{c.session_fees:>10.2f}",
        f"{'backtest':10}{c.backtest_equity:>12,.2f}{backtest_return:>+10.2%}"
        f"{len(c.matched) + len(c.backtest_only):>7}{c.backtest_fees:>10.2f}",
        f"fills: {len(c.matched)} matched, {len(c.session_only)} only in the session, "
        f"{len(c.backtest_only)} only in the backtest",
    ]
    if c.matched:
        worst = max(m.extra_cost_bps for m in c.matched)
        lines.append(
            f"price against the backtest: mean {c.mean_extra_cost_bps:+.1f} bps, worst "
            f"{worst:+.1f} bps (modeled slippage {c.slippage_bps:g} bps; positive is worse)"
        )
    lines.append(f"largest equity gap: {c.max_gap:.2%}")
    if c.adjustments:
        lines.append(
            f"{c.adjustments} reconciliation adjustments moved {c.flows:+,.2f}; "
            "taken out of the session's equity"
        )
    if c.pending:
        lines.append(f"not compared yet: {c.pending} session fills after the last complete bar")
    for label, fills in (("session only", c.session_only), ("backtest only", c.backtest_only)):
        for f in fills[:SHOWN_FILLS]:
            lines.append(
                f"  {label:<14}{f.time:%Y-%m-%d %H:%M} {f.symbol} "
                f"{f.quantity:+.6f} @ {f.price:,.2f}"
            )
    checks = c.checks()
    lines.extend(f"check: {text}" for text in checks)
    if checks:
        lines.append("verdict: does not track the backtest yet; see the checks")
    elif not c.matched:
        lines.append("verdict: no trades to compare yet; let it run longer")
    else:
        lines.append("verdict: tracks the backtest")
    return "\n".join(lines)
