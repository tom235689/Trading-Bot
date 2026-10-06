# Changelog

## 0.2.0 (2026-10-06)

Release hardening after three independent reviews. Upgrade note: exit codes changed (3 and 4 are new; the old 2 for a ledger in use is now 3), and the kill switch default is 45%.

**Live trading safety**
- A sell never exceeds the book's position, so a protective stop that fills just before an exit can no longer sell coins the owner holds; buys never spend negative book cash.
- Budget reconciliation sets a negative book position or cash back to zero and compares total quote; it reads balances between two settle passes and waits a round when something executed meanwhile.
- Fills, order rows, stop state, adjustments, and the guard shift are written atomically.
- A missing or failed protective stop is alerted once and retried at every reconciliation.
- Orders whose send status is unknown are looked up again before they count as failed; unexpected errors no longer crash an event or the reconciliation loop; a long Retry-After fails at once.
- A changed `initial_cash` and a `tbot resume` survive reconciliation; the feed never loses a bar when a catch-up fails.

**Validation**
- Monte Carlo resamples daily returns in blocks, so losses while a trade is open count.
- Trials include their setup and result; every backtest over the holdout counts as a look; the trial log is kept in git.
- Attribution pairs days by date; walk-forward windows no longer drift; streams stop at a common end.
- The backtest can run the session guard; the kill switch default moved from 15% (which halts the shipped strategy in 2018) to 45%.
- Re-validated: no configuration passes the gate.

**Operations**
- `tbot doctor`: strategies, ledger, alerts and heartbeat ping, clock, symbols, kill switch on history, API key permissions, budget.
- `tbot compare`: a session against a backtest of the same period.
- `tbot stop`: a graceful stop for a bot without a console.
- One-line errors and distinct exit codes (1 retry, 3 ledger unusable, 4 wrong config or command); `scripts/run_bot.ps1` restarts with backoff and logs to `logs/supervisor.log`; `scripts/install_task.ps1` checks the config and runs doctor before registering a startup task.
- Downloads backfill earlier history without leaving holes and use Binance's clock.

## 0.1.0

Data download and storage, event-driven backtester, Donchian and RSI strategies, volatility targeting, validation pipeline, paper trading, Binance live trading with reconciliation, risk guard, protective stops, Telegram alerts, heartbeat, and the HTML dashboard.
