# Trading Bot Design

Status: Draft v1 (2026-09-23)

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
 Binance   <--> |  Exchange Adapter  |  (ccxt REST + WebSocket)
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
| `testnet` | Binance testnet | Binance testnet adapter | Wall clock |
| `live` | Live Binance data | Binance adapter | Wall clock |

`paper` checks strategy behavior on real prices. `testnet` checks the real order path. Both are needed before `live`.

## 5. Components

### 5.1 Core Models

- `Bar`, `TargetExposure`, `OrderIntent`, `Order`, `Fill`, `Position`, `Balance`, `RiskEvent`.
- All timestamps are UTC. A bar is keyed by its open time and only used after it closes.
- Prices and quantities use `Decimal` at the exchange boundary; research code may use floats.

### 5.2 Data

- **Historical**: complete months come from `data.binance.vision` monthly archives, verified against their SHA-256 checksums. Everything after the last archive (including a month not yet published) comes from the public REST API (`data-api.binance.vision`). Only closed bars are stored.
- **Storage**: Parquet via polars, one file per `exchange/market/symbol/timeframe/year`. Writes merge by open time and replace files atomically. Sync only extends forward.
- **Timestamps**: spot archives use microseconds from 2025 and milliseconds before; both are normalized to UTC milliseconds.
- **Off-grid bars**: Binance has stretches of bars not aligned to the timeframe grid (1h bars at :28 after the February 2018 outage). They are dropped, not snapped: snapping would leak future prices into a bar.
- **Live** (`tbot/live/feed.py`): kline WebSocket for closed bars, REST catch-up on connect, after every reconnect, and from a watchdog that polls whenever a bar is overdue. Every emitted bar is first written to the same Parquet store, and the store's last bar is the memory of what was seen, so nothing is emitted twice or out of order. Bars from streams that close at the same instant are grouped into one event (short wait for stragglers), as in the backtest. A stream overdue by more than `stale_after_seconds` raises a stale alert once.
- **Quality checks**: errors are duplicates, off-grid bars, unclosed bars, and invalid prices. Gaps, zero-volume bars, and large moves are warnings, since they are usually real exchange events. Keep delisted symbols to avoid survivorship bias.
- **Perpetuals**: also store funding rate history.

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
- Deterministic: same input gives the same output. Internal state is allowed only if it derives from bars seen through `on_bar`, so replaying history rebuilds it.
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
- Volatility targeting: scale exposure down for more volatile symbols.
- No-trade band: skip rebalances smaller than a threshold or below the minimum notional, to limit churn.
- Tracks positions, cash, realized and unrealized PnL, and per-strategy attribution.

### 5.5 Risk Manager

Independent layer with veto power. Values below are the intended live defaults and live in config. Backtest defaults are permissive (weight and gross caps of 100%) so strategies can be studied unconstrained; implemented so far: per-symbol cap, gross cap, long-only.

| Rule | Default |
|---|---|
| Risk per trade | 0.5-1% of equity, sized from stop distance |
| Max weight per symbol | 30% |
| Gross exposure | 100% spot; max 2x leverage on perpetuals |
| Daily loss limit | -3%: block new entries until next UTC day |
| Max drawdown | -15%: flatten all and halt (kill switch) |
| Order sanity | Reject prices far from mid, below min notional, or off tick/step size |
| Stale data | Block trading if the latest bar is older than expected |
| Unknown state | Block trading if reconciliation fails |

The kill switch state is persisted. A restart does not resume trading; a human must reset it.

**Implementation** (`tbot/live/guard.py`, applied by `SessionTrader` in paper, testnet, and live): the `guard` config section holds `daily_loss_limit`, `max_drawdown`, and `stale_seconds` (zero disables a rule). Each event: equity below the day's opening equity by the daily limit puts the session in reduce-only mode (orders may only shrink positions); equity below the running peak by the drawdown limit flattens every position and halts; a bar older than `stale_seconds` when it arrives blocks that event. Guard state (peak, day open, halted) is saved in the ledger after every check, so a restart stays halted; `tbot resume <config>` clears it and resets the peak. Per-symbol and gross exposure caps are applied earlier by `RiskLimits`; order sanity (tick, step, minimum notional) by the executor; unknown state (reconciliation failure) by alerting and adopting the exchange.

### 5.6 Execution

- **Executor** (`tbot/live/executor.py`): the session hands signed order quantities to an executor. `PaperExecutor` fills through the simulated broker; `LiveExecutor` trades on Binance spot. The same `SessionTrader` drives both.
- **Write-ahead**: the order is written to the ledger as `pending` before it is sent, then updated to `filled`, `skipped`, `unfilled`, or `failed`.
- **Idempotency**: the client order id is derived from the bar close time, symbol, and side (`tb<close ms><symbol><B|S>`). Before sending, the executor asks the exchange for that id; an order that already exists (crash after send, lost response) is adopted, never re-sent. A transport failure while sending is followed by the same lookup.
- **Sizing at the exchange**: buys are capped by free quote balance (with fee and a small margin), sells by free base balance; quantities are rounded down to the lot step and orders below the exchange minimum are skipped.
- **Fees**: commission paid in the base asset reduces the filled quantity; every commission is converted into quote and booked as the fill's fee (BNB and others via the ticker), so the book stays equal to the exchange balances.
- **Order type**: market orders for now. Post-only limit with a market fallback is a later improvement.
- **Protective stops**: after every event a `STOP_LOSS_LIMIT` sell sits on the exchange for each held position at `protective_stop_pct` below the last close (limit 0.5% under the stop). Stops are cancelled before a sell (they lock the balance) and re-placed afterwards. A stop that fires while the bot is down shows up as a reconciliation adjustment.
- **Reconciliation** (`tbot/live/reconcile.py`): on startup and every `reconcile_seconds`, base balances (free plus locked) are compared with booked positions and free quote with booked cash. Differences beyond the lot step or `reconcile_tolerance` (cash: at least one quote unit) are adopted from the exchange, written to the ledger's `adjustments` table, and alerted. Restoring a book replays fills and adjustments in time order.

### 5.7 Binance Adapter

- `tbot/exchange/binance.py` talks to the spot REST API directly (no ccxt): the bot needs about ten endpoints, and a small adapter is easier to test against a fake exchange. Signed requests use HMAC-SHA256 with a timestamp from the server-synced clock and `recv_window`.
- GET and DELETE retry on rate limits (418, 429) and server errors with backoff; POST never retries, the executor's client-id lookup handles uncertainty.
- Symbol rules from `exchangeInfo` (`PRICE_FILTER`, `LOT_SIZE`, `NOTIONAL`) round prices and quantities with `Decimal`; symbols that are not `TRADING` refuse to start.
- Order and fill updates come from the order response (`newOrderRespType=FULL`) and periodic reconciliation; the user data stream is a later improvement.
- Client order ids: at most 36 characters, only characters Binance allows.
- Fees come from config and are checked against the account's actual fee rates (BNB discount, VIP tier).
- Testnet (`https://testnet.binance.vision`) has the same API with fake balances; signals still use production market data, so testnet fills happen at testnet prices and only prove the mechanics.
- Binance blocks some regions, including the US. The host must run from an allowed region.

### 5.8 Backtest

Two tiers:

1. **Research (vectorized)**: pandas or polars over full arrays. Screens many ideas quickly. Never used for final decisions.
2. **Validation (event-driven)**: reuses the live engine with a simulated broker.

**Event loop** (validation tier): events are bar close times across all loaded streams. At each event the engine reveals bars closing now, fills pending orders, marks positions at the latest close and records equity, runs strategies whose bars closed, then combines targets, applies risk limits, and queues orders. Before `start`, strategies run to build state but nothing trades.

**Fill model**

- Signals use closed bars only. Market orders fill at the open of the next bar of the symbol's finest loaded timeframe, plus slippage. After a data gap, the order waits for the next available bar.
- Spot broker rules: buys are capped by cash (including the fee), sells by the position.
- Rebalance rules: a zero target closes the exact position; other changes below `rebalance_threshold` of equity or below `min_notional` are skipped.
- Limit orders fill only if the next bar trades through the price, not just touches it (not implemented yet).
- Fees, slippage (bps, configurable), and funding (perpetuals, at each funding time) are always applied.

**Config** (YAML): `start`, optional `end` (exclusive), `initial_cash`, `costs`, `risk`, `rebalance`, and `strategies` as in 5.3. Allocations must sum to at most 1. See `config/donchian_trend.yaml`.

**Report**: CAGR, Sharpe and Sortino (daily returns, 365-day year, zero risk-free rate), Calmar, max drawdown, win rate, profit factor, turnover, exposure, fees, average trade return. Per-strategy attribution comes with multi-strategy support.

### 5.9 Persistence

- **Market data**: Parquet, read and written with polars.
- **Ledger**: SQLite, moving to PostgreSQL if needed. Tables: `runs`, `signals`, `order_intents`, `orders`, `fills`, `positions`, `equity_snapshots`, `risk_events`.
- Every signal, order, and fill is recorded, so live results can be compared with a backtest over the same period.

### 5.10 Monitoring and Alerts

- Structured logs (structlog): readable console output plus rotating JSON lines in `logs/`.
- Telegram alerts (`TBOT_TELEGRAM_TOKEN`, `TBOT_TELEGRAM_CHAT_ID`): start and stop, every fill, failed price lookups, stale streams, task crashes, and a daily summary at `summary_hour_utc`. Without a token, alerts go to the log. A failed send is logged and never stops trading. Risk limit hits, kill switch, and reconciliation mismatches are added with the live phase.
- External heartbeat (`TBOT_HEARTBEAT_URL`): the bot pings an outside monitor every `heartbeat_seconds`. If pings stop, the monitor alerts. A dead bot cannot alert on its own.
- `tbot status <config>` prints the ledger: equity, positions, recent fills and events, and the last stored bar per stream.
- Dashboard (later): equity curve, positions, per-strategy performance.

**Paper session** (`tbot paper <config>`, `tbot/live/paper.py`): the session (`tbot/live/session.py`) runs the backtest's decision path one event at a time: append the bars that closed, mark, run the strategies whose bars closed, combine targets, apply risk limits, plan orders. Paper fills happen at once at the live book price (ask for buys, bid for sells) through the same simulated broker with fees and slippage. Every signal, order, fill, and equity snapshot goes to a SQLite ledger. On start the bot syncs the store, rebuilds the portfolio by replaying the ledger's fills, rebuilds strategy state by replaying stored history through the strategies, and warns if the trading config changed since the ledger was created. Fills are timestamped with the wall clock; equity snapshots with the bar close. A test proves the session reproduces the backtest engine bar for bar when fills use the next bar's open.

### 5.11 Security and Configuration

- API keys: trading permission only, withdrawals disabled, IP whitelist enabled.
- Secrets live in `.env`, never committed. Git hooks scan for secrets.
- Config: YAML for settings, environment variables for secrets, validated with pydantic.
- Secrets come from the environment or a `.env` file (`TBOT_` prefix, see `.env.example`), validated by pydantic-settings.
- Paper and live are separate commands with separate configs. `live` will require both `mode: live` in config and an explicit `--live` CLI flag.

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
8. **Paper trading**: 2-4 weeks; results must track a backtest over the same period.
9. **Small live**: start with small capital and scale gradually.

**Initial promotion gate (tunable)**: out-of-sample Sharpe above 0.8 after costs, max drawdown under 25%, at least 100 trades, profitable under 2x costs.

**Implementation** (`tbot validate <validation.yaml>`, code in `src/tbot/research/`):

- A validation config points at a base backtest config and sets `holdout_start`, the parameter `grid`, the `objective` (Sharpe by default), walk-forward window lengths, Monte Carlo settings, the cost multiplier, and gate thresholds. Steps 1-3 and 5-7 run in one command; breadth (step 4) is covered by running the same config on other symbols.
- Steps run on the in-sample period (base start to `holdout_start`): baseline, cost stress, the full sweep, walk-forward (grid search on each train window, best objective applied to the next test window), and Monte Carlo on the baseline's trades. The holdout runs last, once, with the base params. The report says how many times the holdout has been evaluated, because every look at it weakens it.
- Parameter stability is read from the sweep: the objective over the whole grid, the baseline's rank, and the mean objective of a point and its grid neighbors (a plateau scores close to its peak).
- Out-of-sample metrics come from the walk-forward test segments stitched into one equity curve; each segment starts flat.
- Monte Carlo shuffles trade order (returns on equity at entry) to get the drawdown distribution. Shuffling leaves the compounded return unchanged, so return percentiles are only reported for bootstrap resampling.
- Every run appends to `data/trials.jsonl`. The deflated Sharpe counts distinct parameter sets tried on the same sample (strategy, symbols, timeframe, period); re-running identical params is not a new trial.
- The gate applies to the stitched out-of-sample result (Sharpe, drawdown, trades), the cost stress (still profitable), and the holdout (profitable). Sweeps run in parallel processes.

## 8. Tech Stack

| Area | Choice |
|---|---|
| Runtime | Python 3.12, uv |
| Exchange access | ccxt (REST + WebSocket) |
| Data | polars, numpy |
| Storage | Parquet (polars), SQLite |
| HTTP | httpx |
| Config and models | pydantic, pydantic-settings, YAML |
| Concurrency | asyncio |
| Quality | ruff, mypy, pytest, hypothesis, git hooks (`.githooks/`) |
| Alerts | Telegram Bot API |
| Deployment | Docker on a VPS in an allowed region (e.g. Tokyo) |

## 9. Repository Layout

```
Trading-Bot/
  pyproject.toml
  config/                # yaml configs
  docs/
  src/tbot/
    core/                # models, events, clock
    data/                # download, storage, live feed
    strategies/          # base, registry, plugins
    portfolio/           # positions, pnl, allocation
    risk/                # rules, kill switch
    execution/           # order manager, sim broker
    exchange/            # binance adapter
    backtest/            # engine, cost models, reports
    research/            # vectorized tests, walk-forward, optimization
    monitoring/          # logging, alerts, heartbeat
    cli.py               # download | backtest | paper | testnet | live
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
| 6. Expansion | Multi-strategy allocation, dashboard, perpetuals | Two or more strategies running together |

No live trading before a strategy passes phase 3 validation.

## 11. Open Questions

- Fee tier and BNB fee discount: confirm actual account rates before phase 5.
- Hosting provider and region.
- Starting capital and per-strategy allocations.
- Promotion gate thresholds: revisit after the first validation report.
