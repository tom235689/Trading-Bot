"""Compare the bot's book with the exchange account.

Two ownership modes:
- "budget": the bot owns `initial_cash` and what it bought with it; the rest of the
  account belongs to the owner. The exchange can only shrink the book (a manual
  sale, a withdrawal, fees paid elsewhere); balances beyond the book are ignored.
- "account": the bot owns the whole account (a dedicated account); the exchange wins
  both ways.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Literal

import structlog

from tbot.exchange.binance import Balance, BinanceSpot
from tbot.live.ledger import Adjustment, Ledger
from tbot.monitoring.telegram import Notifier
from tbot.portfolio.portfolio import Portfolio

log = structlog.get_logger(__name__)
CASH_TOLERANCE = 1.0  # quote units; fee assets like BNB make small cash drift normal
Ownership = Literal["budget", "account"]


async def reconcile(
    spot: BinanceSpot,
    portfolio: Portfolio,
    symbols: Sequence[str],
    ledger: Ledger,
    notifier: Notifier,
    now: datetime,
    *,
    tolerance: float,
    ownership: Ownership = "account",
    label: str = "live",
    balances: Mapping[str, Balance] | None = None,
    on_adjust: Callable[[list[Adjustment]], None] | None = None,
    cash_floor: float = 0.0,
) -> list[Adjustment]:
    """Move the book to the exchange where they differ beyond tolerance; record why.

    In budget mode the book only moves down to the exchange, never below zero: a book
    that went negative (a fill booked twice) is set back to zero. Cash may stay down to
    cash_floor: a budget lowered by more than the cash held leaves the book owing the rest
    until sales repay it. on_adjust runs in the same ledger transaction as the adjustments
    (the guard shift), so a crash splits neither.
    """
    if balances is None:
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
        target = _target(booked, held, ownership)  # coins beyond the book are the owner's
        step = float(spot.rules[symbol].step_size)
        if abs(target - booked) <= max(step, tolerance * max(abs(target), abs(booked))):
            continue
        note = f"{symbol} position: book {booked:.6f}, exchange {held:.6f}"
        adjustments.append(Adjustment(now, symbol, target - booked, 0.0, note))

    # Locked quote counts too: an open buy order of the owner's does not shrink the book.
    cash = balances[quote].total if quote in balances else 0.0
    target = _target(portfolio.cash, cash, ownership, min(cash_floor, 0.0))
    if abs(target - portfolio.cash) > max(
        CASH_TOLERANCE, tolerance * max(abs(target), abs(portfolio.cash))
    ):
        note = f"cash: book {portfolio.cash:.2f}, exchange {cash:.2f}"
        adjustments.append(Adjustment(now, "", 0.0, target - portfolio.cash, note))

    if not adjustments:
        return adjustments
    with ledger.atomic():
        for adjustment in adjustments:
            ledger.add_adjustment(adjustment)
            ledger.add_event(now, "warning", f"reconciled {adjustment.note}")
        if on_adjust is not None:
            on_adjust(adjustments)
    for adjustment in adjustments:
        portfolio.adjust(adjustment.symbol, adjustment.quantity, adjustment.cash)
        log.warning("reconciled", note=adjustment.note)
    if adjustments:
        lines = "\n".join(a.note for a in adjustments)
        await notifier.send(f"[{label}] book adjusted to the exchange:\n{lines}")
    return adjustments


def _target(booked: float, held: float, ownership: Ownership, floor: float = 0.0) -> float:
    if ownership == "account":
        return held
    return max(floor, min(booked, held))
