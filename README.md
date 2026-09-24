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
