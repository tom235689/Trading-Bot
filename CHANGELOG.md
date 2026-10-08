# Changelog

## 0.3.1 (2026-10-08)

Fixes from four independent reviews of 0.3.0 (live path, research, operations, and a mutation test of the suite). Upgrade notes: a paper session now logs to `logs\<config name>.jsonl`, like live (`paper.jsonl` for `config\paper.yaml`, as before); `.gitattributes` merges the trial log by union.

**Live trading safety**
- An order whose result is in doubt now holds its symbol back: the next event looks it up first and, if it executed or is still unknown, plans that symbol again at the next bar instead of ordering twice on a stale book. In budget mode that second order could sell the owner's coins or spend the owner's USDT.
- No protective stop is sized while a sell is in doubt (its free coins may be the owner's; 0.3.0 still did this until the next reconciliation), and a stop that triggered and partly filled is left to finish instead of being replaced above the market.
- An odd reply from Telegram (a proxy page, a malformed update) no longer ends the session; the command poller pauses and tries again.
- A close whose streams arrive in two events (a late 4h bar after the 1h one) is checkpointed only when every stream is in, so a restart in between still trades the late bar.

**Operations**
- `scripts\update.ps1` keeps local edits in every failure case (0.3.0 lost them when `uv sync` failed after the pull), restarts every task it stopped whatever happens, ends a supervisor waiting to restart, clears leftover stop requests, refuses to run with a leftover stash or a bot started by hand, checks configs with the new `tbot doctor --offline`, and exits with 1 when the update was not applied.
- `scripts\setup.ps1` refuses while a bot runs from `.venv`; `scripts\remove_task.ps1` ends a waiting supervisor and leaves everything as it was when the bot does not stop.
- `tbot stop --cancel` withdraws a stop request; `tbot backup --out` accepts a folder and never leaves a partial file; every session logs to its own file.

**Data and research**
- The Monte Carlo resamples every bar instead of daily closes, as the kill switch checks every bar. For the configured candidate out of sample: median drawdown -34%, 5th percentile -55%, 17% of four-year paths beyond 45% (0.3.0 said -33%, -54%, 16%).
- Delisted symbols are filled from their archives, partial months included; a 400 that is not "invalid symbol" stays an error; a first download of a symbol listed after `--start` stores its archives as they come.
- After a rate limit the live feed stays away from REST until the exchange allows it, and `tbot download` stops instead of hammering on.
- A leftover sold on its own belongs to the round trip that left it, so it is no longer a separate winning trade.

**Tests**
- 32 new tests (341 in all), among them a whole paper session from start to stop with the network replaced, the reconciliation pause and recovery, the guard shift through a reconciliation round, atomic ledger writes, the exchange-side order caps, volatility sizing without look-ahead, and kill-switch timing in the backtest. Each was checked to fail without the code it guards. Tests no longer depend on the folder pytest starts in.

## 0.3.0 (2026-10-08)

Product round after three more independent reviews (live path, research, operations). Upgrade notes: the supervisor's logs are per config now (`logs\<config>.supervisor.log`, `logs\<config>.console.txt`); a wrong command line or a `.env` that is not UTF-8 exits with 4 instead of 2 or 1; a changed `initial_cash` is booked as an adjustment at the next start.

**Use it as a product**
- `scripts\setup.ps1`: one command from clone to doctor, including a Python signed by the Python Software Foundation when Smart App Control is on.
- `scripts\update.ps1`: stops the scheduled sessions gracefully, pulls, syncs, runs doctor, starts them again; local edits are kept, or the update is undone when it collides with them. `scripts\remove_task.ps1` removes a task after a graceful stop.
- Ledger backups every 6 hours, 14 days kept (`backup_days`), `tbot backup` on demand, a tested restore; doctor warns when backups fall behind.
- Telegram: `/status` and `/fills` from the chat (`telegram_commands`, read only); `tbot notify` finds the chat id; the daily summary adds drawdown from the peak, fills, the last bar event, and any halt, pause, or position without a stop. Alerts are sent in the background, so a slow Telegram never delays an order.
- `tbot status` says whether a session is running; the dashboard shows when it was made and spaces points by time, so downtime shows.

**Live trading safety**
- An order in doubt, or a stop, that reconciliation books between bars now gets its protective stop at once instead of up to 4 hours later; an in-doubt sell no longer leaves a stop on the owner's coins.
- A restart no longer skips the bar that closed while the bot was down: bars within the guard's stale window are traded, and a crash in the middle of an event hands that bar over again.
- `tbot stop` also stops a bot the supervisor is about to restart, and no longer reports "stopped" for one that crashed.
- REST bars count as closed only 5 seconds after the close, and a slow clock resync keeps the previous offset, so a fast clock cannot store a bar that is still open.
- Orders in doubt that turn out never executed or unfilled are alerted.
- A changed `initial_cash` is a transfer everywhere: the dashboard's drawdown and `tbot compare` no longer read it as a loss.

**Data and research**
- The validation's Monte Carlo also resamples the out-of-sample returns and reports the chance of passing the kill switch. For the configured candidate: median drawdown -33% and 5th percentile -54% over four years, 16% of paths beyond 45% (report addendum).
- Syncs leave no hole when they fail part way (backfills write newest first; nothing after a month missing from the archives is stored before REST fills it); delisted symbols keep their archives; writers to one stream take turns; a long `Retry-After` fails at once instead of stalling the feed for hours; one failed stream no longer stops `tbot download`.
- A trade with no opening cost no longer breaks the backtest result.

**Operations**
- The supervisor keeps retrying with a clear log line when uv is missing, rotates its log, and keeps sessions' logs apart; the installer reads `mode: live` with quotes or a comment and asks for administrator rights up front.
- doctor no longer passes the kill switch on less than a year of history.

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
