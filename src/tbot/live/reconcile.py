"""Compare the bot's book with the exchange account; the exchange wins."""

from collections.abc import Sequence
from datetime import datetime

import structlog

from tbot.exchange.binance import BinanceSpot
from tbot.live.ledger import Adjustment, Ledger
from tbot.monitoring.telegram import Notifier
from tbot.portfolio.portfolio import Portfolio

log = structlog.get_logger(__name__)
CASH_TOLERANCE = 1.0  # quote units; fee assets like BNB make small cash drift normal


async def reconcile(
    spot: BinanceSpot,
    portfolio: Portfolio,
    symbols: Sequence[str],
    ledger: Ledger,
    notifier: Notifier,
    now: datetime,
    *,
    tolerance: float,
    label: str = "live",
) -> list[Adjustment]:
    """Adopt exchange balances that differ from the book beyond tolerance; record why."""
    balances = await spot.balances()
    quotes = {spot.rules[s].quote for s in symbols}
    if len(quotes) != 1:
        raise ValueError(f"all symbols must share one quote asset, got {sorted(quotes)}")
    quote = quotes.pop()
    adjustments = []

    for symbol in symbols:
        base = spot.rules[symbol].base
        held = balances[base].total if base in balances else 0.0
        booked = portfolio.position(symbol)
        step = float(spot.rules[symbol].step_size)
        if abs(held - booked) > max(step, tolerance * max(abs(held), abs(booked))):
            note = f"{symbol} position: book {booked:.6f}, exchange {held:.6f}"
            adjustments.append(Adjustment(now, symbol, held - booked, 0.0, note))

    cash = balances[quote].free if quote in balances else 0.0
    if abs(cash - portfolio.cash) > max(
        CASH_TOLERANCE, tolerance * max(abs(cash), abs(portfolio.cash))
    ):
        note = f"cash: book {portfolio.cash:.2f}, exchange {cash:.2f}"
        adjustments.append(Adjustment(now, "", 0.0, cash - portfolio.cash, note))

    for adjustment in adjustments:
        portfolio.adjust(adjustment.symbol, adjustment.quantity, adjustment.cash)
        ledger.add_adjustment(adjustment)
        ledger.add_event(now, "warning", f"reconciled {adjustment.note}")
        log.warning("reconciled", note=adjustment.note)
    if adjustments:
        lines = "\n".join(a.note for a in adjustments)
        await notifier.send(f"[{label}] book adjusted to the exchange:\n{lines}")
    return adjustments
