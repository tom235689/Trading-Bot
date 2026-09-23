# Trading Bot

Multi-strategy crypto trading bot for Binance. See [docs/DESIGN.md](docs/DESIGN.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
git config core.hooksPath .githooks
```

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
