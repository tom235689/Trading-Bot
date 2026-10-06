# Trading Bot Design

Status: v1, phases 0-6 implemented (updated 2026-10-06)

## 1. Goals and Non-Goals

**Goals**

- Automated crypto trading on Binance.
- Multiple strategies as plugins, combined into one portfolio.
- One code path for backtest, paper, and live trading.
- An independent risk layer that can veto any strategy.
- A validation pipeline that every strategy must pass before going live.

**Non-goals (for now)**

- High-frequency trading or market making. Latency is not a design driver.
- Multiple exchanges. The adapter boundary allows adding them later.
- Machine learning strategies. They can be added later as plugins.

## 2. Design Principles

Most bots lose money for three reasons, not because of weak code:

1. **Overfitting**: the strategy fits past noise and fails live.
2. **Missing costs**: fees, slippage, and funding are ignored, so backtest profits are fake.
3. **Operational failures**: disconnects, duplicate orders, state drift between bot and exchange, or the bot dying with open positions.

The design answers each of them:

- **Same code everywhere**: backtest, paper, and live share strategy, portfolio, risk, and execution logic. Only the clock, data source, and broker change.
- **Strategies emit intent, not orders**: strategies return target exposure. Sizing and limits belong to the risk layer.
- **Validation gates**: no strategy trades real money without passing the pipeline in section 7.
- **Exchange is the source of truth**: bot state is reconciled against the exchange on startup and periodically.
- **Fail safe**: on unknown state, stale data, or breached limits, stop opening positions and alert.

## 3. Key Decisions

| Topic | Decision | Reason |
|---|---|---|
| Exchange | Binance | Deep liquidity, many pairs, spot and futures, testnet, bulk historical data |
| Market | Spot first, USD-M perpetuals later (max 2x leverage) | No liquidation risk while the system matures |
| Quote asset | USDT | Most liquid quote on Binance |
| Timeframe | 4h primary, 1h secondary | Expected move per trade is well above costs; latency does not matter; enough trades for statistics |
| Build vs framework | Build own core | Multi-strategy portfolio with an independent risk layer; Freqtrade runs one strategy per bot, NautilusTrader is heavy for this scope |
| Language | Python 3.12 | Best ecosystem for data, research, and exchange access |

The core stays timeframe-agnostic so faster strategies (e.g. 15m) can be added later.

## 4. Architecture

```
                +--------------------+
 Binance   <--> |  Exchange Adapter  |  (own REST adapter + kline WebSocket)
                +--------------------+
                   | market data           ^ orders / fills
                   v                       |
            +-------------+         +------------------+
            |  Data Feed  |         | Execution Engine |
            +-------------+         +------------------+
                   | bar events            ^ approved orders
                   v                       |
      +------------------------+    +------------------+
      | Strategy plugins (N)   | -> | Portfolio + Risk |
      | -> target exposure     |    +------------------+
      +------------------------+           |
                                           v
                        Ledger DB / Logs / Alerts / Dashboard
```

**Cycle** (runs on every bar close):

1. Data feed emits a closed bar.
2. Each strategy subscribed to that symbol and timeframe returns target exposure.
3. Portfolio combines targets using strategy allocations.
4. Risk manager clips or vetoes the combined targets.
5. Execution computes the difference from current positions and places orders.
6. Fills update positions and the ledger.

**Run modes**

| Mode | Data | Broker | Clock |
|---|---|---|---|
| `backtest` | Historical replay | Simulated broker | Simulated |
| `paper` | Live Binance data | Simulated broker | Wall clock |
| `testnet` | Live Binance data | Binance testnet adapter | Wall clock |
| `live` | Live Binance data | Binance adapter | Wall clock |

`paper` checks strategy behavior on real prices. `testnet` checks the real order path. Both are needed before `live`.

## 5. Components

### 5.1 Core Models

- Bars are polars frames with a fixed schema (`tbot/data/schema.py`); `Fill` and `Trade` are the shared records (`tbot/core/models.py`); exchange orders and balances live in the adapter (`tbot/exchange/binance.py`). Strategy targets are plain `symbol -> weight` mappings.
- All timestamps are UTC. A bar is keyed by its open time and only used after it closes.
- Prices and quantities use `Decimal` at the exchange boundary; research code may use floats.

### 5.2 Data

- **Historical**: complete months come from `data.binance.vision` monthly archives, verified against their SHA-256 checksums. Everything after the last archive (including a month not yet published) comes from the public REST API (`data-api.binance.vision`). Only closed bars are stored.
- **Storage**: Parquet via polars, one file per `exchange/market/symbol/timeframe/year`. Writes merge by open time and replace files atomically. Sync leaves no holes: it extends forward from the last stored bar, backfills when the start is earlier than the first stored bar (one probe request first, so a start before the listing costs nothing more), and fills months missing from the archives from REST. Sessions sync as many bars as their strategies need to warm up.
- **Timestamps**: spot archives use microseconds from 2025 and milliseconds before; both are normalized to UTC milliseconds.
- **Off-grid bars**: Binance has stretches of bars not aligned to the timeframe grid (1h bars at :28 after the February 2018 outage). They are dropped, not snapped: snapping would leak future prices into a bar.
- **Live** (`tbot/live/feed.py`): kline WebSocket for closed bars, REST catch-up on connect, after every reconnect, and from a watchdog that polls whenever a bar is overdue. Every emitted bar is first written to the same Parquet store, and the store's last bar is the memory of what was seen, so nothing is emitted twice or out of order. Bars from streams that close at the same instant are grouped into one event (short wait for stragglers), as in the backtest; events are consumed in close-time order, so a catch-up of several bars keeps every stream in sequence. A socket bar that does not follow the last emitted one triggers a REST catch-up first, so a missed close never leaves a hole. A failed catch-up is logged and retried at the next poll. A stream whose next bar is overdue by more than `stale_after_seconds` raises a stale alert once.
- **Quality checks**: errors are duplicates, off-grid bars, unclosed bars, and invalid prices. Gaps, zero-volume bars, and large moves are warnings, since they are usually real exchange events. Keep delisted symbols to avoid survivorship bias.
- **Perpetuals** (deferred): would also need funding rate history.

### 5.3 Strategy Plugins

```python
class Strategy(ABC):
    name: ClassVar[str]

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None: ...

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Bars needed before the first signal."""

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Mapping[str, float]:
        """Target exposure per symbol in [-1, 1], as a fraction of this strategy's capital."""
```

**Contract**

- Pure logic: no exchange calls, no file or network I/O, no wall clock. `StrategyContext` provides read-only arrays of closed bars and current portfolio exposures.
- Deterministic: same input gives the same output. Internal state is allowed only if it derives from bars seen through `on_bar`. A restart replays only recent bars, which may not reach the event that set path-dependent state (an entry weeks ago), so such strategies also implement `restore(targets)`: the session saves every slot's targets after each event and hands them back once the replay reaches that time.
- Output is target exposure as a fraction of the strategy's allocated capital, not order size. Weights across symbols should sum to at most 1. Negative values are clipped to zero in spot mode.
- Registered with `@register_strategy`; each strategy validates its params with its own pydantic model (unknown keys are rejected).

**Configuration**

```yaml
strategies:
  - name: donchian_trend
    symbols: [BTC/USDT, ETH/USDT]
    timeframe: 4h
    allocation: 0.5
    params: {entry: 55, exit: 20}
  - name: rsi_reversion
    symbols: [BTC/USDT]
    timeframe: 1h
    allocation: 0.3
    params: {period: 14, low: 25, high: 75}
```

### 5.4 Portfolio

- Combines strategy targets by allocation. Opposite targets on the same symbol net out, saving fees.
- Volatility targeting (`risk.target_volatility`, `risk.volatility_lookback_days`; `tbot/risk/volatility.py`): each symbol's combined weight is multiplied by `min(1, target / realized)` where realized is the annualized standard deviation of log returns over the lookback on the symbol's finest stream. Spot never scales up. Applied before the per-symbol and gross caps, identically in backtest and session. On the Donchian strategy a 0.4 target cut the 2018-2024 drawdown from -40% to -25% while raising the Sharpe from 1.03 to 1.29 (`docs/reports/donchian_voltarget_2026-09-29.md`).
- No-trade band: skip rebalances smaller than a threshold or below the minimum notional, to limit churn.
- Tracks positions, cash, realized and unrealized PnL, and per-strategy attribution.

### 5.5 Risk Manager

Independent layer with veto power. Values below are the intended live defaults and live in config. Backtest defaults are permissive (weight and gross caps of 100%) so strategies can be studied unconstrained. Not implemented yet: risk per trade sized from a stop distance, and rejecting prices far from mid (market orders use the book price at the moment).

| Rule | Default |
|---|---|
| Risk per trade | 0.5-1% of equity, sized from stop distance |
| Max weight per symbol | 50% (two symbols share the allocation) |
| Gross exposure | 100% spot; max 2x leverage on perpetuals |
| Daily loss limit | -3% below the UTC day's opening equity: reduce-only while it lasts |
| Max drawdown | -45%: flatten all and halt (kill switch); set beyond the strategy's 5th-percentile Monte Carlo drawdown so it trips on a failure, not a normal drawdown |
| Order sanity | Reject prices far from mid, below min notional, or off tick/step size |
| Stale data | Block trading if the latest bar is older than expected |
| Unknown state | Block trading if reconciliation fails |

The kill switch state is persisted. A restart does not resume trading; a human must reset it. The backtest runs the same guard when the config has a `guard` section (a halt lasts to the end of the run), so a level that would stop the strategy during ordinary drawdowns shows up before it goes live: with the shipped settings, 15% halts the backtest in November 2018.

**Implementation** (`tbot/risk/guard.py`, applied by `SessionTrader` in paper, testnet, and live): the `guard` config section holds `daily_loss_limit`, `max_drawdown`, and `stale_seconds` (zero disables a rule). Each event: equity below the day's opening equity by the daily limit puts the session in reduce-only mode (orders may only shrink positions); equity below the running peak by the drawdown limit flattens every position and halts; a bar older than `stale_seconds` when it arrives blocks that event (announced once per streak, then logged). Money that enters or leaves the book from outside (reconciliation adjustments: deposits, withdrawals, manual trades) shifts the peak and day-open levels, so it never counts as a loss or a gain. Guard state (peak, day open, halted) is saved in the ledger after every check, so a restart stays halted; `tbot resume <config>` clears it and resets the peak, and a running session re-reads the state each event, so the resume takes effect at the next bar. The kill switch is announced when it trips and whenever flattening fills something, not on every bar, since dust below the exchange minimum can remain. Per-symbol and gross exposure caps are applied earlier by `RiskLimits`; order sanity (tick, step, minimum notional) by the executor; unknown state by pausing new orders after three failed reconciliations in a row, with an alert, until one succeeds.

### 5.6 Execution

- **Executor** (`tbot/live/executor.py`): the session hands signed order quantities to an executor. `PaperExecutor` fills through the simulated broker; `LiveExecutor` trades on Binance spot. The same `SessionTrader` drives both.
- **Write-ahead**: every order gets a row with its client order id before it is sent (`pending`), then a row that ends it: `filled`, `unfilled`, or `failed` (`skipped` orders are never sent).
- **Client order ids**: `tb<close ms><symbol><B|S>`, plus `r<n>` when a second decision at the same close needs a new id (a stream that arrived late), so an id is never reused.
- **Outcome in doubt**: Binance says a 5xx or -1007 answer to an order leaves its execution unknown, and a lost response says nothing. The executor then looks the id up a few times; found means booked, never seen means failed. If the exchange cannot be asked, the row becomes `unknown`. Before every reconciliation, and at startup, `settle` looks up every `pending` or `unknown` order (including ones left by a crash) and books or fails it; commissions of an order read back this way come from its trades (`myTrades`).
- **Sizing at the exchange**: buys are capped by free quote balance and by the book's own cash (with fee and a small margin), sells by free base balance; quantities are rounded down to the lot step and orders below the exchange minimum are skipped.
- **Fees**: commission paid in the base asset reduces the filled quantity; every commission is converted into quote and booked as the fill's fee (BNB and others via the ticker), so the book stays equal to the exchange balances.
- **Order type**: market orders for now. Post-only limit with a market fallback is a later improvement.
- **Protective stops**: after every event a `STOP_LOSS_LIMIT` sell sits on the exchange for each held position at `protective_stop_pct` below the last close (limit 0.5% under the stop; in a gap through the limit it may not fill). Stops are cancelled before a sell (they lock the balance) and re-placed afterwards. The stop in force is remembered in the ledger; whatever part of it executed, between events or while the bot was down, is booked as a fill (fee at the configured rate) before the next reconciliation.
- **Ownership** (`ownership` in testnet and live configs): `budget` (default) means the bot owns `initial_cash` and what it buys with it; every other balance in the account, including coins of the traded symbols bought by hand, is the owner's and is never adopted or sold. `account` means the bot owns the whole spot account; use it only for a dedicated account.
- **Reconciliation** (`tbot/live/reconcile.py`): on startup and every `reconcile_seconds`, after `settle`, base balances (free plus locked) are compared with booked positions and quote (free plus locked, so an owner's open order is no loss) with booked cash. Differences beyond the lot step or `reconcile_tolerance` (cash: at least one quote unit) move the book to the exchange, in `budget` mode only downward; they are written to the ledger's `adjustments` table and alerted. Restoring a book replays fills and adjustments in time order. Reconciliation and event handling share a lock, so the book is never compared with the exchange while an order is in flight. Testnet and live default to separate ledgers and log files (`data/<mode>.sqlite`, `logs/<mode>.jsonl`).

### 5.7 Binance Adapter

- `tbot/exchange/binance.py` talks to the spot REST API directly (no ccxt): the bot needs about ten endpoints, and a small adapter is easier to test against a fake exchange. Signed requests use HMAC-SHA256 with a timestamp from the server-synced clock and `recv_window`.
- GET and DELETE retry on rate limits (418, 429) and server errors with backoff; POST never retries, the executor's client-id lookup handles uncertainty. A request rejected for its timestamp (-1021) was not executed, so it is signed again once after a clock resync.
- Symbol rules from `exchangeInfo` (`PRICE_FILTER`, `LOT_SIZE`, `NOTIONAL`) round prices and quantities with `Decimal`; symbols that are not `TRADING` refuse to start.
- Order and fill updates come from the order response (`newOrderRespType=FULL`) and periodic reconciliation; the user data stream is a later improvement.
- Client order ids: at most 36 characters, only characters Binance allows.
- Fees: `costs.fee_rate` sizes buys and estimates the fee of stop fills; market order fees are the real commissions. Commissions paid in BNB lower the book's cash while the exchange takes BNB instead, so in `account` mode reconciliation hands that cash back; turn BNB payment off or accept the drift.
- Testnet (`https://testnet.binance.vision`) has the same API with fake balances; signals still use production market data, so testnet fills happen at testnet prices and only prove the mechanics.
- Binance blocks some regions, including the US. The host must run from an allowed region.

### 5.8 Backtest

Two tiers:

1. **Research (vectorized)**: polars over full arrays, to screen many ideas quickly; never used for final decisions. Not built yet: every result so far comes from the event-driven engine.
2. **Validation (event-driven)**: reuses the live engine with a simulated broker.

**Event loop** (validation tier): events are bar close times across all loaded streams. At each event the engine reveals bars closing now, fills pending orders, marks positions at the latest close and records equity, runs strategies whose bars closed, then combines targets, applies risk limits, and queues orders for the symbols whose execution stream (finest loaded timeframe) closed now; other symbols keep their pending orders, so a faster stream of another symbol never postpones a fill. Paper and live plan orders the same way. Before `start`, strategies run to build state but nothing trades. With a `guard` in the config the engine applies it after marking: reduce-only filters orders, a halt flattens at the next open and ends trading. Without an `end`, every stream stops at the earliest last close among them.

**Fill model**

- Signals use closed bars only. Market orders fill at the open of the next bar of the symbol's finest loaded timeframe, plus slippage. After a data gap, the order waits for the next available bar.
- Spot broker rules: buys are capped by cash (including the fee), sells by the position.
- Rebalance rules: a zero target closes the exact position, and so does a reduction that would leave less than `min_notional`, which could never be sold; other changes below `rebalance_threshold` of equity or below `min_notional` are skipped. A remainder worth less than `min_notional` after a sale (lot rounding, commissions) ends the round trip.
- Limit orders fill only if the next bar trades through the price, not just touches it (not implemented yet).
- Fees and slippage (bps, configurable) are always applied; funding would be for perpetuals, which are deferred.

**Config** (YAML): `start`, optional `end` (exclusive), `initial_cash`, `costs`, `risk`, `rebalance`, and `strategies` as in 5.3. Allocations must sum to at most 1. See `config/donchian_trend.yaml`.

**Report**: CAGR, Sharpe and Sortino (daily returns, 365-day year, zero risk-free rate), Calmar, max drawdown, win rate, profit factor, turnover, exposure, fees, average trade return. `--html` writes the dashboard for the run.

**Attribution** (`tbot backtest <config> --attribution`, `tbot/backtest/attribution.py`): runs every strategy alone with full capital and then the configured combination, and prints the metrics side by side with the correlation of the solo daily returns. This is how a candidate second strategy is judged: an uncorrelated strategy only helps if it has positive expectancy on its own. First result (`config/multi.yaml`): the RSI mean-reversion leg loses money alone (Sharpe -0.37) and lowers the mix below Donchian alone despite a low correlation (0.18, days paired by date), so it stays an example, not a recommendation.

### 5.9 Persistence

- **Market data**: Parquet, read and written with polars.
- **Ledger** (`tbot/live/ledger.py`): SQLite in WAL mode with full sync. Tables: `meta` (config hash, guard state, strategy checkpoint, protective stop in force), `signals`, `orders` (with client ids), `fills`, `adjustments`, `equity`, `events`. Positions are not stored: they are rebuilt from fills and adjustments.
- One process per ledger: a lock file next to it (released by the operating system when the process dies) refuses a second session on the same book.
- Every signal, order, and fill is recorded. To compare paper with a backtest, run the same trading settings as a backtest from the paper start; paper fills at the book price at the close, the backtest at the next bar's open, so expect small differences per trade.

### 5.10 Monitoring and Alerts

- Structured logs (structlog): readable console output plus rotating JSON lines in `logs/`. Error entries never include request URLs, which carry the Telegram token and the heartbeat URL.
- Telegram alerts (`TBOT_TELEGRAM_TOKEN`, `TBOT_TELEGRAM_CHAT_ID`): start and stop, every fill, failed price lookups, stale streams, task crashes, and a daily summary at `summary_hour_utc`. Without a token, alerts go to the log. A failed send is logged and never stops trading. Also sent: kill switch, entries blocked, paused and resumed trading, reconciliation adjustments and failures, orders in doubt and their resolution, protective stops that executed, and a failed start. `tbot notify` sends a test message.
- External heartbeat (`TBOT_HEARTBEAT_URL`): the bot pings an outside monitor every `heartbeat_seconds`. If pings stop, the monitor alerts. A dead bot cannot alert on its own, so this is the only way to learn about a hard kill; it means alive, not trading (a halted bot keeps pinging).
- `tbot status <config>` prints the ledger: equity, positions, recent fills and events, and the last stored bar per stream.
- `tbot doctor <config>` checks a session before it runs: the strategies and their params (a config that cannot start fails first), the ledger (a damaged file, the kill switch, orders in doubt, a running process), Telegram and the heartbeat URL (pinged once), Binance reachability and clock offset, tradable symbols, whether the configured kill switch would have halted a backtest on the stored history, and for testnet and live the API key, the account's trading permission, and in budget mode whether free USDT covers the bot's cash. On production it reads the key's restrictions and fails if the key can withdraw. Exit code 1 on any failure.
- `tbot stop <config>` leaves a request file next to the ledger; the running session finishes the event in progress and stops with exit code 0, which also ends the supervisor.
- `tbot compare <config>` replays the session's period through the backtest engine with the same settings, guard included, and compares bar events, fills (matched by symbol, side, and time within one bar), prices against the backtest's, and equity (step 8 of the validation pipeline). Missed bars point at downtime; a mean price gap above the modeled slippage means the backtest underestimates costs. Protective stop fills are named, not priced, since the backtest has no exchange stop; reconciliation adjustments are valued when they happened and taken out of the session's equity. Exit code 1 when it does not track.
- Dashboard (`tbot dashboard <config>`, `tbot backtest --html`; `tbot/monitoring/dashboard.py`): one self-contained HTML file with stat tiles, equity and drawdown charts (inline SVG, crosshair tooltip, light and dark mode, table view), open positions, recent fills and events. Static on purpose: nothing listens on the trading machine; generate it on demand or on a schedule. Per-strategy performance is the attribution report.

**Paper session** (`tbot paper <config>`, `tbot/live/runner.py`): the session (`tbot/live/session.py`) runs the backtest's decision path one event at a time: append the bars that closed, mark, run the strategies whose bars closed, combine targets, apply risk limits, plan orders. Paper fills happen at once at the live book price (ask for buys, bid for sells) through the same simulated broker with fees and slippage. Every signal, order, fill, and equity snapshot goes to a SQLite ledger. On start the bot syncs the store, rebuilds the portfolio by replaying the ledger's fills and adjustments, rebuilds strategy state by replaying stored history and restoring the saved targets (ignored if the strategies changed), and warns if the trading config changed since the ledger was created. A start that fails is logged and alerted with exit code 1. Exit codes 3 (the ledger is in use, damaged, or SQLite cannot load) and 4 (a wrong config or command, including unknown strategies, checked before the start) tell the supervisor not to retry; every command prints one line for an error instead of a traceback (`TBOT_DEBUG=1` shows it). On a graceful stop, an event in progress finishes its orders first (up to a minute). Fills are timestamped with the wall clock; equity snapshots with the bar close. A test proves the session reproduces the backtest engine bar for bar when fills use the next bar's open.

### 5.11 Security and Configuration

- API keys: spot trading permission only, withdrawals disabled, IP whitelist enabled when the host has a static IP (with a changing home IP, every signed call fails after a change).
- Secrets live in `.env`, never committed. Git hooks scan for secrets.
- Config: YAML for settings, environment variables for secrets, validated with pydantic.
- Secrets come from the environment or a `.env` file (`TBOT_` prefix, see `.env.example`), validated by pydantic-settings.
- Paper and live are separate commands with separate configs. Real money requires both `mode: live` in the config and the `--live` flag; `--live` is refused for testnet so the flag keeps its meaning.
- Paths in configs and defaults (`.env`, `data/`, `logs/`, ledgers) are relative to the working directory: run the bot from the repository root (`uv --directory <repo> run ...` under a scheduler).

## 6. Initial Strategy Candidates

These are candidates to validate, not proven edges. They are chosen to be weakly correlated.

| Strategy | Timeframe | Idea |
|---|---|---|
| Donchian trend | 4h | Enter on N-bar high breakout, exit on M-bar low |
| Cross-sectional momentum | 1d, weekly rebalance | Hold the strongest of the top N coins by recent return |
| RSI / Bollinger mean reversion | 1h | Buy oversold, sell overbought, only in ranging regimes |
| Funding carry | 8h | Perpetuals only; add after futures support |

Trend and mean reversion earn in different regimes. Running both smooths the equity curve, which is the main payoff of the plugin design.

## 7. Validation Pipeline

A strategy is promoted to live only after passing every step:

1. **Holdout**: the most recent 20-30% of data is untouched until the final check.
2. **Walk-forward**: optimize on a window, test on the next, roll forward.
3. **Parameter stability**: performance must hold on a broad plateau, not a single peak.
4. **Breadth**: test across several symbols and bull, bear, and sideways regimes.
5. **Cost stress**: still profitable with 2x fees and slippage.
6. **Monte Carlo**: resample trade order to estimate the drawdown distribution.
7. **Trial log**: record every tested variant and adjust Sharpe for the number of trials (deflated Sharpe).
8. **Paper trading**: 2-4 weeks; results must track a backtest over the same period (`tbot compare`).
9. **Small live**: start with small capital and scale gradually.

**Initial promotion gate (tunable)**: out-of-sample Sharpe above 0.8 after costs, max drawdown under 25%, at least 100 trades, profitable under 2x costs.

**Implementation** (`tbot validate <validation.yaml>`, code in `src/tbot/research/`):

- A validation config points at a base backtest config and sets `holdout_start`, the parameter `grid`, the `objective` (Sharpe by default), walk-forward window lengths, Monte Carlo settings, the cost multiplier, and gate thresholds. Steps 1-3 and 5-7 run in one command; breadth (step 4) is covered by running the same config on other symbols.
- Steps run on the in-sample period (base start to `holdout_start`): baseline, cost stress, the full sweep, walk-forward (grid search on each train window, best objective applied to the next test window; a train window in which no grid point reaches `min_trades` is an error, not an arbitrary pick; `selection: neighborhood` picks the qualifying point whose grid neighbors do best on average instead of the single best one), and Monte Carlo on the baseline's daily returns. Grid points vary only the listed parameters; the others keep the base config's values. `step_months`, if set, must equal `test_months`, since the test segments are stitched. The holdout runs last, once, with the base params. The report says how many runs have seen the holdout period, counting every backtest that reached into it, because every look weakens it.
- Parameter stability is read from the sweep: the objective over the whole grid, the baseline's rank, and the mean objective of a point and its grid neighbors (a plateau scores close to its peak).
- Out-of-sample metrics come from the walk-forward test segments stitched into one equity curve; each segment starts flat.
- History before a backtest's start covers the strategies' warmup and, when volatility targeting is on, the volatility window on each symbol's finest stream, so the first weeks are sized like the rest. Paper and live sessions replay the same amount from the store.
- Monte Carlo resamples the baseline's daily returns in blocks of `block_days` (default 20) to get the drawdown and return distribution. Daily returns include losses while trades are open, and blocks keep streaks of bad days together; shuffling closed trades hid both and understated drawdowns.
- Every run appends to `data/trials.jsonl`, with a hash of its setup (risk, costs, rebalance, other strategies). The deflated Sharpe counts distinct trials on the same sample (strategy, symbols, timeframe, period): a trial is params and their result, so re-running a setup is not a new trial, while the same params under other risk settings are. Runs without a Sharpe count as trials.
- The gate applies to the stitched out-of-sample result (Sharpe, drawdown, trades), the cost stress (still profitable), and the holdout (profitable). Sweeps run in parallel processes.

## 8. Tech Stack

| Area | Choice |
|---|---|
| Runtime | Python 3.12, uv |
| Exchange access | own Binance spot REST adapter (httpx), websockets |
| Data | polars, numpy |
| Storage | Parquet (polars), SQLite |
| HTTP | httpx |
| Config and models | pydantic, pydantic-settings, YAML |
| Concurrency | asyncio |
| Quality | ruff, mypy, pytest, git hooks (`.githooks/`) |
| Alerts | Telegram Bot API |
| Deployment | a Windows or Linux host under a supervisor that restarts on exit (`scripts/install_task.ps1` and `scripts/run_bot.ps1` on Windows); a VPS in an allowed region later |

## 9. Repository Layout

```
Trading-Bot/
  pyproject.toml
  config/                # yaml configs
  docs/
  scripts/               # git guard, Windows task and restart loop
  src/tbot/
    core/                # config, models, timeframes
    data/                # download, storage, quality checks
    live/                # feed, session, executor, ledger, reconciliation, doctor, compare
    strategies/          # base, registry, plugins
    portfolio/           # positions, pnl, allocation
    risk/                # exposure limits, volatility targeting, session guard
    execution/           # rebalance planning, sim broker
    exchange/            # binance adapter
    backtest/            # engine, runner, metrics, attribution, reports
    research/            # sweep, walk-forward, Monte Carlo, trial log, validation
    monitoring/          # logging, alerts, heartbeat, dashboard
    cli.py               # download check backtest validate paper live doctor stop
                         # status compare resume account dashboard notify
  tests/
```

## 10. Roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. Foundation | Project layout, tooling, git hooks (Hangul check, secret scan) | Lint, type check, and tests pass |
| 1. Data | Historical download, storage, quality checks | Several years of BTC and ETH 4h/1h data stored and verified |
| 2. Core and backtester | Models, plugin interface, event-driven backtester, cost models, report, one sample strategy | Backtest matches hand-calculated results in tests |
| 3. Validation tools | Walk-forward, parameter sweep, Monte Carlo, trial log | Validation report for the sample strategy |
| 4. Paper trading | Live data, simulated broker, ledger, Telegram alerts, heartbeat | Two weeks of uninterrupted operation |
| 5. Live | Binance adapter, live executor, reconciliation, risk guard with kill switch, exchange-side stops, `tbot live` | Testnet run, then small live capital |
| 6. Expansion | Volatility targeting, second strategy plugin and attribution, dashboard; perpetuals deferred | Two or more strategies running together |

Perpetuals are deferred: they add a second API surface (USD-M futures), margin and liquidation handling, funding accrual, and shorting to every layer, which deserves its own design pass. The validation results say exposure control on spot was the more valuable step.

No live trading before a strategy passes phase 3 validation. As of 2026-10-05 no configuration has passed the gate. With the corrected validation tools the best candidate (Donchian with volatility targeting 0.4, used by the paper, testnet, and live configs) reaches an out-of-sample Sharpe of 0.79 and a drawdown of -43.5%; the walk-forward result hinges on a near tie in the 2022 parameter choice, and plateau selection does not help. Further variants on the same data would only add trials. Going live anyway is the owner's decision, with a small budget.

## 11. Open Questions

- Fee tier and BNB fee discount: confirm actual account rates before phase 5.
- Hosting provider and region.
- Starting capital and per-strategy allocations.
- Promotion gate thresholds: revisit after the first validation report.
