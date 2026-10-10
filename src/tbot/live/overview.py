"""Every session config on one screen: what `tbot status` shows without a config."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from tbot.data.store import BarStore
from tbot.live.config import SessionConfig
from tbot.live.ledger import Ledger
from tbot.live.runner import GUARD_META, base_cash, is_running, price_at, restore_portfolio
from tbot.risk.guard import GuardState


@dataclass(frozen=True)
class SessionRow:
    name: str
    state: str  # running, stopped, not started
    equity: float | None = None
    profit: float | None = None  # equity less the money put in
    invested: float = 0.0
    day_change: float | None = None
    from_peak: float | None = None
    last_event: datetime | None = None
    symbols: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def session_row(name: str, config: SessionConfig, store: BarStore) -> SessionRow:
    if not config.ledger.is_file():
        return SessionRow(name, "not started")
    state = "running" if is_running(config.ledger) else "stopped"
    try:
        ledger = Ledger(config.ledger)
    except Exception as exc:  # damaged, or no SQLite: say so and show the others
        return SessionRow(name, state, notes=(f"ledger unusable: {exc}",))
    try:
        return _row(name, state, config, ledger, store)
    except Exception as exc:  # one session that cannot be read never hides the others
        return SessionRow(name, state, notes=(f"cannot read the ledger: {exc!r}",))
    finally:
        ledger.close()


def broken_row(name: str, problem: str, ledger: Path | None) -> SessionRow:
    """A config that does not load; a session may still run on the ledger it names."""
    running = ledger is not None and ledger.is_file() and is_running(ledger)
    return SessionRow(name, "running" if running else "invalid", notes=(problem,))


def _row(
    name: str, state: str, config: SessionConfig, ledger: Ledger, store: BarStore
) -> SessionRow:
    raw = ledger.get_meta(GUARD_META)
    guard = GuardState.model_validate_json(raw) if raw else GuardState()
    notes = []
    if guard.halted:
        notes.append(f"HALTED: {guard.halt_reason} (tbot resume {name})")
    doubtful = len(ledger.unresolved_orders())
    if doubtful:
        notes.append(f"{doubtful} order(s) in doubt, settled when it runs")
    point = ledger.latest_equity()
    if point is None:
        return SessionRow(name, state, notes=(*notes, "no bar event yet"))
    # Deposits, withdrawals, and budget changes are money put in, not profit.
    flows = [
        (a.time, a.cash + a.quantity * price_at(config, store, a.symbol, a.time))
        for a in ledger.adjustments()
        if a.time <= point.time
    ]
    invested = base_cash(config, ledger) + sum(value for _, value in flows)
    earlier = ledger.equity_before(point.time - timedelta(days=1))
    day_change = None
    if earlier and earlier.equity > 0:
        moved = sum(value for at, value in flows if at > earlier.time)
        day_change = (point.equity - moved) / earlier.equity - 1
    positions = restore_portfolio(config, ledger).positions
    return SessionRow(
        name,
        state,
        equity=point.equity,
        profit=point.equity - invested,
        invested=invested,
        day_change=day_change,
        from_peak=(
            min(point.equity / guard.peak_equity - 1, 0.0) if guard.peak_equity > 0 else None
        ),
        last_event=point.time,
        symbols=tuple(sorted(positions)),
        notes=tuple(notes),
    )


def overview_text(rows: list[SessionRow]) -> str:
    if not rows:
        return "no session configs in config/ (paper.yaml, testnet.yaml, live.yaml)"
    width = max(7, *(len(row.name) for row in rows))
    head = f"{'config':<{width}}  state"
    if any(row.equity is not None for row in rows):
        head = (
            f"{'config':<{width}}  {'state':<11}  {'equity':>11}  {'profit':>18}  {'24h':>7}  "
            f"{'peak':>7}  {'last bar (UTC)':<16}  positions"
        )
    lines = [head]
    for row in rows:
        line = f"{row.name:<{width}}  {row.state:<11}"
        if row.equity is not None and row.profit is not None:
            share = f" ({row.profit / row.invested:+.1%})" if row.invested > 0 else ""
            line += (
                f"  {row.equity:>11,.2f}  {f'{row.profit:+,.2f}{share}':>18}  "
                f"{_percent(row.day_change):>7}  {_percent(row.from_peak):>7}  "
                f"{_time(row.last_event):<16}  {' '.join(row.symbols) or 'none'}"
            )
        lines.append(line.rstrip())
        lines.extend(f"{'':<{width}}  {note}" for note in row.notes)
    if all(row.state == "not started" for row in rows):
        lines.append("nothing has run yet: `tbot doctor` checks the setup, `tbot paper` starts")
    else:
        lines.append("details: tbot status <config>; what it does: tbot log <config> -f")
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1%}"


def _time(moment: datetime | None) -> str:
    return "-" if moment is None else f"{moment:%Y-%m-%d %H:%M}"
