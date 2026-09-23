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
