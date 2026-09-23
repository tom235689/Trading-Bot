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

- **Historical**: bulk download klines from `data.binance.vision`; fill recent gaps with the REST API. Store as Parquet, partitioned by `market/symbol/timeframe/year`. Query with DuckDB.
- **Live**: subscribe to kline streams over WebSocket. On reconnect, backfill missing bars via REST before emitting new ones.
- **Quality checks**: missing bars, duplicates, zero volume, outliers. Keep delisted symbols to avoid survivorship bias.
- **Perpetuals**: also store funding rate history.

### 5.3 Strategy Plugins

```python
class Strategy(ABC):
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]

    @abstractmethod
    def warmup(self) -> int:
        """Bars needed before the first signal."""

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        """Return target exposure per symbol in [-1, 1]."""
```

**Contract**

- Pure logic: no exchange calls, no file or network I/O, no wall clock. `StrategyContext` provides bar history and current positions.
- Deterministic: same input gives the same output.
- Output is target exposure as a fraction of the strategy's allocated capital, not order size. Negative values are ignored in spot mode.
- Registered with `@register_strategy`; parameters validated by the `Params` pydantic model.

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

Independent layer with veto power. Values below are initial defaults and live in config.

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

### 5.6 Execution

- **Order lifecycle**: `PENDING -> NEW -> PARTIALLY_FILLED -> FILLED | CANCELED | REJECTED | EXPIRED`.
- **Write-ahead**: persist the order intent before sending, so a crash can be recovered.
- **Idempotency**: every order carries a deterministic client order ID. On timeout, query by that ID before retrying.
- **Retries**: exponential backoff on network errors and rate limits; no retry on business rejects.
- **Order type**: post-only limit (`LIMIT_MAKER`) first for lower fees; fall back to market after a timeout.
- **Protective stops**: place exchange-side stop orders so losses stay bounded if the bot dies.
- **Reconciliation**: on startup and every few minutes, compare balances, positions, and open orders with the exchange. On mismatch, alert and adopt exchange state.

### 5.7 Binance Adapter

- Built on ccxt with `enableRateLimit`. Respect request weight and order count limits.
- Sync server time (`adjustForTimeDifference`) to avoid timestamp and `recvWindow` errors.
- Round prices and quantities to each symbol's `PRICE_FILTER`, `LOT_SIZE`, and `NOTIONAL` filters from exchange info.
- Order and fill updates from the user data stream over WebSocket, with REST polling as a fallback.
- Client order IDs: at most 36 characters, only characters Binance allows.
- Fees come from config and are checked against the account's actual fee rates (BNB discount, VIP tier).
- Binance blocks some regions, including the US. The host must run from an allowed region.

### 5.8 Backtest

Two tiers:

1. **Research (vectorized)**: pandas or polars over full arrays. Screens many ideas quickly. Never used for final decisions.
2. **Validation (event-driven)**: reuses the live engine with a simulated broker.

**Fill model**

- Signals use closed bars only. Market orders fill at the next bar open plus slippage.
- Limit orders fill only if the next bar trades through the price, not just touches it.
- Fees, slippage (bps, configurable), and funding (perpetuals, at each funding time) are always applied.

**Report**: CAGR, Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, turnover, exposure, average trade return vs cost, per-strategy attribution.

### 5.9 Persistence

- **Market data**: Parquet + DuckDB.
- **Ledger**: SQLite, moving to PostgreSQL if needed. Tables: `runs`, `signals`, `order_intents`, `orders`, `fills`, `positions`, `equity_snapshots`, `risk_events`.
- Every signal, order, and fill is recorded, so live results can be compared with a backtest over the same period.

### 5.10 Monitoring and Alerts

- Structured JSON logs (structlog).
- Telegram alerts: fills, errors, risk limit hits, kill switch, reconciliation mismatches.
- External heartbeat: the bot pings an outside monitor. If pings stop, the monitor alerts. A dead bot cannot alert on its own.
- Dashboard (later): equity curve, positions, per-strategy performance.

### 5.11 Security and Configuration

- API keys: trading permission only, withdrawals disabled, IP whitelist enabled.
- Secrets live in `.env`, never committed. Pre-commit runs a secret scan.
- Config: YAML for settings, environment variables for secrets, validated with pydantic.
- Default mode is `paper`. `live` requires both `mode: live` in config and an explicit `--live` CLI flag.

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

## 8. Tech Stack

| Area | Choice |
|---|---|
| Runtime | Python 3.12, uv |
| Exchange access | ccxt (REST + WebSocket) |
| Data | polars, pandas, numpy |
| Storage | Parquet + DuckDB, SQLite |
| Config and models | pydantic, pydantic-settings, YAML |
| Concurrency | asyncio |
| Quality | ruff, mypy, pytest, hypothesis, pre-commit |
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
| 0. Foundation | Project layout, tooling, pre-commit (Hangul check, secret scan) | Lint, type check, and tests pass |
| 1. Data | Historical download, storage, quality checks | Several years of BTC and ETH 4h/1h data stored and verified |
| 2. Core and backtester | Models, plugin interface, event-driven backtester, cost models, report, one sample strategy | Backtest matches hand-calculated results in tests |
| 3. Validation tools | Walk-forward, parameter sweep, Monte Carlo, trial log | Validation report for the sample strategy |
| 4. Paper trading | Live data, simulated broker, Telegram alerts, heartbeat | Two weeks of uninterrupted operation |
| 5. Live | Binance adapter, reconciliation, kill switch, exchange-side stops | Testnet run, then small live capital |
| 6. Expansion | Multi-strategy allocation, dashboard, perpetuals | Two or more strategies running together |

No live trading before a strategy passes phase 3 validation.

## 11. Open Questions

- Fee tier and BNB fee discount: confirm actual account rates before phase 5.
- Hosting provider and region.
- Starting capital and per-strategy allocations.
- Promotion gate thresholds: revisit after the first validation report.
