# Trading Bot

Multi-strategy crypto trading bot for Binance. See [docs/DESIGN.md](docs/DESIGN.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
git config core.hooksPath .githooks
```

## Market data

```sh
uv run tbot download                    # BTCUSDT, ETHUSDT 1h/4h since 2017-08 into data/
uv run tbot check                       # quality report; exit code 1 on errors
uv run tbot download --symbols SOLUSDT --timeframes 4h --start 2021-01-01
```

Downloads only extend forward from the last stored bar. Delete the symbol's directory under `data/` to re-download from scratch.

## Backtest

```sh
uv run tbot backtest config/donchian_trend.yaml
```

A config lists strategies with symbols, timeframe, allocation, and params, plus costs, risk limits, and rebalance rules. Strategies are plugins registered by name in `src/tbot/strategies/`.

## Validation

```sh
uv run tbot validate config/donchian_validation.yaml
```

Runs the in-sample baseline, cost stress, parameter sweep, walk-forward, Monte Carlo, deflated Sharpe, and a single holdout evaluation, then checks the promotion gate. Exit code 1 means the gate failed. Every backtest is appended to `data/trials.jsonl`; keep that file, it is the record of how many things were tried. See [docs/reports/](docs/reports/) for past reports.

## Paper trading

```sh
cp .env.example .env                    # optional: Telegram token and chat id, heartbeat URL
uv run tbot paper config/paper.yaml     # runs until Ctrl+C; logs to logs/paper.jsonl
uv run tbot status config/paper.yaml    # equity, positions, recent fills and events
```

The bot syncs the bar store, rebuilds state from the ledger (`data/paper.sqlite`) and stored history, then trades on closed bars from the Binance WebSocket with simulated fills at the live book price. Restarting resumes from the ledger. Keep the machine awake and the network up; a supervisor (Task Scheduler, NSSM, systemd) that restarts the process on exit is recommended for long runs.

## Testnet and live trading

```sh
# .env: TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET (testnet keys from testnet.binance.vision)
uv run tbot account config/testnet.yaml          # balances and open orders: checks the keys
uv run tbot live config/testnet.yaml             # real order path, fake money
uv run tbot live config/live.yaml --live         # real money; the flag is mandatory
uv run tbot status config/live.yaml
uv run tbot resume config/live.yaml              # clear the kill switch after a halt
```

Before live: API key with spot trading only, withdrawals off, IP restricted; Windows time sync enabled (the bot warns when the clock is off by more than a second); a supervisor that restarts the process. Every position carries an exchange-side stop `protective_stop_pct` below the last close, so a dead bot still has bounded loss.

## Git hooks

| Hook | Checks |
|---|---|
| `pre-commit` | Hangul and secrets in staged changes, ruff, mypy |
| `commit-msg` | Hangul and secrets in the message |
| `pre-push` | Hangul and secrets in every outgoing commit and tag, tests |

Run a full scan of tracked files and history:

```sh
uv run python scripts/git_guard.py audit
```

Mark a known false-positive secret line with `guard: allow-secret`.

## Development

```sh
uv run ruff check .
uv run ruff format .
uv run mypy
uv run pytest
```
