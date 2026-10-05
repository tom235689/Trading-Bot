# Trading Bot

Multi-strategy crypto trading bot for Binance spot. See [docs/DESIGN.md](docs/DESIGN.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and, on Windows, [Git for Windows](https://git-scm.com/download/win) (the git hooks are shell scripts).

```sh
uv sync
git config core.hooksPath .githooks
uv run tbot --help                      # or: uv run python -m tbot --help
```

### Windows Smart App Control

Smart App Control can block unsigned Python files at any time, even ones that worked yesterday. The symptom is `DLL load failed ... An Application Control policy has blocked this file`, most often for `_sqlite3`, which every session command needs. The fix is a Python signed by the Python Software Foundation (the python.org build), which the policy allows:

```powershell
# Install Python 3.12 from python.org (per user is enough), then point the project at it:
uv venv --python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" --clear
uv sync
```

mypy is installed as pure Python (`no-binary-package` in `pyproject.toml`) because its compiled build is unsigned; an environment that still has the compiled one needs `uv sync --reinstall-package mypy`. If a generated launcher such as `tbot.exe` is blocked, use `uv run python -m tbot ...` instead. The hooks already run their tools that way. `backtest`, `download`, `check`, and `validate` do not need SQLite; the session commands print this advice when it cannot load.

## Market data

```sh
uv run tbot download                    # BTCUSDT, ETHUSDT 1h/4h since 2017-08 into data/
uv run tbot check                       # quality report; exit code 1 on errors
uv run tbot download --symbols SOLUSDT --timeframes 4h --start 2021-01-01
```

Downloads extend forward from the last stored bar and back to `--start` when that is earlier than the first stored bar, so running `paper` first and `download` later still gives the full history. Months missing from the archives come from the REST API. To re-download a stream from scratch, delete `data/binance/spot/klines/<SYMBOL>/<timeframe>/`.

## Backtest

```sh
uv run tbot backtest config/donchian_voltarget.yaml
```

A config lists strategies with symbols, timeframe, allocation, and params, plus costs, risk limits (including volatility targeting), and rebalance rules. Strategies are plugins registered by name in `src/tbot/strategies/` (`donchian_trend`, `rsi_reversion`).

```sh
uv run tbot backtest config/multi.yaml --attribution      # each strategy alone, the mix, correlation
uv run tbot backtest config/donchian_voltarget.yaml --html reports/voltarget.html
```

## Validation

```sh
uv run tbot validate config/donchian_voltarget_validation.yaml
```

Runs the in-sample baseline, cost stress, parameter sweep, walk-forward, Monte Carlo, deflated Sharpe, and a single holdout evaluation, then checks the promotion gate. Exit code 1 means the gate failed. Every backtest is appended to `data/trials.jsonl`; keep that file, it is the record of how many things were tried. See [docs/reports/](docs/reports/).

**Status**: no configuration has passed the gate yet. The best candidate, Donchian trend with volatility targeting 0.4, misses the out-of-sample drawdown limit by two points ([report](docs/reports/donchian_voltarget_2026-09-29.md)). The paper, testnet, and live configs all use exactly its trading settings, so paper rehearses what would go live.

## Paper trading

```sh
cp .env.example .env                    # optional: Telegram and heartbeat settings
uv run tbot notify                      # sends a test Telegram message
uv run tbot paper config/paper.yaml     # runs until Ctrl+C; logs to logs/paper.jsonl
uv run tbot status config/paper.yaml    # equity, positions, recent fills and events
uv run tbot dashboard config/paper.yaml # reports/paper.html
```

The bot syncs the bar store, rebuilds its book from the ledger (`data/paper.sqlite`) and its strategy state from stored bars plus the targets saved at the last event, then trades on closed 4h bars from the Binance WebSocket with simulated fills at the live book price.

**Telegram**: create a bot with [@BotFather](https://t.me/BotFather) and put its token in `TBOT_TELEGRAM_TOKEN`. Send the bot any message, open `https://api.telegram.org/bot<token>/getUpdates`, and put the number under `"chat":{"id":...}` in `TBOT_TELEGRAM_CHAT_ID`. Check with `tbot notify`.

**Heartbeat**: set `TBOT_HEARTBEAT_URL` to a ping URL from a monitor such as healthchecks.io. It is the only way to hear about a bot that was killed or a PC that went down. It means "alive", not "trading": a halted bot keeps pinging, and Telegram tells you why it halted.

## Testnet and live trading

```sh
# .env: TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET (testnet keys from testnet.binance.vision)
uv run tbot account config/testnet.yaml          # balances and open orders: checks the keys
uv run tbot live config/testnet.yaml             # real order path, fake money
uv run tbot live config/live.yaml --live         # real money; the flag is mandatory
uv run tbot status config/live.yaml
uv run tbot resume config/live.yaml              # clear the kill switch; a running bot picks it up
```

**What the bot owns**: with `ownership: budget` (the default) the bot manages `initial_cash` USDT and what it buys with it. Everything else in the account, including BTC or ETH you bought yourself, is left alone. `initial_cash` is therefore the most the bot can lose; keep at least that much free USDT in the account. `ownership: account` hands the bot the whole spot account and is only for a dedicated account.

**Before real money**:

- API key with spot trading only and withdrawals off. Restrict it to your IP only if the IP is static; otherwise every signed call fails after your provider changes it.
- Windows time sync on. The bot measures the offset to Binance and warns above one second.
- A supervisor that restarts the bot (below), Telegram, and the heartbeat.
- Two weeks of paper and a testnet run without surprises.

Every position carries an exchange-side stop `protective_stop_pct` below the last close, so a dead bot still has bounded loss. The stop's limit sits 0.5% under the trigger; in a gap through both it may not fill.

## Running unattended

- **Working directory**: `.env`, `data/`, `logs/`, and the ledger paths are relative to it. Always start the bot from the repository root, for example `uv --directory D:\Project\Trading-Bot run python -m tbot paper config/paper.yaml`.
- **Supervisor**: in Task Scheduler, create a task that starts at log on or at startup, runs that command, runs whether the user is logged on or not, and under Settings restarts every minute if the task fails. The bot exits with code 1 on a crash or a failed start and sends a Telegram alert either way.
- **One process per ledger**: a second session on the same config refuses to start. Paper and testnet can run side by side; they share `data/` safely.
- **Stopping**: Ctrl+C lets an event in progress finish its orders. Stopping never sells anything; exchange stops stay in place. After a hard kill or a reboot, the next start books any order whose result was not recorded and reconciles with the exchange.
- **Kill switch**: a 15% drawdown from the peak sells everything and halts. It stays halted across restarts until `tbot resume`, which also restarts the drawdown count from the current equity.
- **Deposits and withdrawals**: in budget mode extra money in the account is ignored. Taking out more than the bot's cash shrinks its book, and the guard treats that as a transfer, not a loss.
- **Logs**: `logs/<mode>.jsonl` rotates at 20 MB with 5 backups. If the supervisor also captures the console, rotate that file too.

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
uv run python -m ruff check .
uv run python -m ruff format .
uv run python -m mypy
uv run python -m pytest
```
