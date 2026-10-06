# Trading Bot

Multi-strategy crypto trading bot for Binance spot: data, backtests, a validation pipeline, paper trading, and live trading with a risk guard. See [docs/DESIGN.md](docs/DESIGN.md) for how it works and [CHANGELOG.md](CHANGELOG.md) for what changed.

**Risk**: trading crypto can lose all the money involved. No strategy here has passed the project's own validation gate (see Validation below). The software comes with no warranty; use real money only with an amount you can afford to lose.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and, on Windows, [Git for Windows](https://git-scm.com/download/win) (the git hooks are shell scripts). The bot runs from a clone of the repository: configs, scripts, and the trial log live there.

```sh
git clone https://github.com/tom235689/Trading-Bot.git
cd Trading-Bot
uv sync
git config core.hooksPath .githooks
uv run tbot --help                      # or: uv run python -m tbot --help
```

### Windows: Smart App Control

On Windows 11 with Smart App Control on, `uv sync` can fail with `An Application Control policy has blocked this file` (for the venv's `python.exe`, `_sqlite3`, or any other unsigned file), even for files that worked yesterday. Use a Python signed by the Python Software Foundation, which the policy allows, before the first `uv sync`:

```powershell
# Install Python 3.12 from python.org (per user is enough), then:
uv venv --python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" --clear
uv sync --frozen
```

mypy is installed as pure Python (`no-binary-package` in `pyproject.toml`) because its compiled build is unsigned; an environment that still has the compiled one needs `uv sync --reinstall-package mypy`. If a generated launcher such as `tbot.exe` is blocked, use `uv run python -m tbot ...`; the hooks and scripts already do.

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

A config lists strategies with symbols, timeframe, allocation, and params, plus costs, risk limits (including volatility targeting), rebalance rules, and optionally the session `guard` (then the backtest stops trading where the kill switch would). Strategies are plugins registered by name in `src/tbot/strategies/` (`donchian_trend`, `rsi_reversion`).

```sh
uv run tbot backtest config/multi.yaml --attribution      # each strategy alone, the mix, correlation
uv run tbot backtest config/donchian_voltarget.yaml --html reports/voltarget.html
```

## Validation

```sh
uv run tbot validate config/donchian_voltarget_validation.yaml
```

Runs the in-sample baseline, cost stress, parameter sweep, walk-forward, Monte Carlo, deflated Sharpe, and a single holdout evaluation, then checks the promotion gate. Exit code 1 means the gate failed. Every backtest is appended to `data/trials.jsonl`, which is kept in git: it is the record of how many things were tried, and the deflated Sharpe depends on it. See [docs/reports/](docs/reports/).

**Status**: no configuration has passed the gate ([report](docs/reports/donchian_voltarget_2026-10-05.md)). Both Donchian candidates fail it out of sample:

| Candidate | OOS Sharpe | OOS max drawdown | Monte Carlo drawdown, median / bad 5% | In-sample drawdown |
|---|---|---|---|---|
| volatility targeting 0.4 (configured) | 0.79 | -43.5% | -28% / -44% | -24.9% |
| full exposure | 0.95 | -28.5% | -47% / -69% | -40.1% |

The in-sample edge is significant after 115 logged trials. The paper, testnet, and live configs use the volatility-targeted settings, which carry far less risk in every in-sample measure; its weaker walk-forward result comes from one 2022 parameter choice that a near tie decided. Expect drawdowns around 28% in a typical stretch and over 40% in a bad one. The design rule is that real money waits for a candidate that passes; trading this one anyway is the owner's decision.

## Paper trading

```sh
cp .env.example .env                    # optional: Telegram and heartbeat settings
uv run tbot notify                      # sends a test Telegram message
uv run tbot doctor config/paper.yaml    # strategies, ledger, alerts, network, kill switch on history
uv run tbot paper config/paper.yaml     # runs until stopped; logs to logs/paper.jsonl
uv run tbot status config/paper.yaml    # equity, positions, recent fills and events
uv run tbot compare config/paper.yaml   # the same period through the backtest: does it track?
uv run tbot dashboard config/paper.yaml # reports/paper.html
uv run tbot stop config/paper.yaml      # a running session finishes its event and stops
```

The bot syncs the bar store, rebuilds its book from the ledger (`data/paper.sqlite`) and its strategy state from stored bars plus the targets saved at the last event, then trades on closed 4h bars from the Binance WebSocket with simulated fills at the live book price.

**Telegram**: create a bot with [@BotFather](https://t.me/BotFather) and put its token in `TBOT_TELEGRAM_TOKEN`. Send the bot any message, open `https://api.telegram.org/bot<token>/getUpdates`, and put the number under `"chat":{"id":...}` in `TBOT_TELEGRAM_CHAT_ID`. Check with `tbot notify`. Save `.env` as UTF-8 (Notepad's default; PowerShell 5.1's `Out-File` writes UTF-16, which the bot rejects with a clear message).

**Heartbeat**: set `TBOT_HEARTBEAT_URL` to a full `https://` ping URL from a monitor such as healthchecks.io; `tbot doctor` pings it once. It is the only way to hear about a bot that was killed or a PC that went down. It means "alive", not "trading": a halted bot keeps pinging, and Telegram tells you why it halted.

## Testnet and live trading

```sh
# .env: TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET (testnet keys from testnet.binance.vision)
uv run tbot doctor config/testnet.yaml           # keys, trading permission, budget, alerts
uv run tbot account config/testnet.yaml          # balances and open orders
uv run tbot live config/testnet.yaml             # real order path, fake money
uv run tbot live config/live.yaml --live         # real money; the flag is mandatory
uv run tbot status config/live.yaml
uv run tbot resume config/live.yaml              # clear the kill switch; a running bot picks it up
```

**What the bot owns**: with `ownership: budget` (the default) the bot manages `initial_cash` USDT and what it buys with it. Everything else in the account, including BTC or ETH you bought yourself, is left alone. `initial_cash` is therefore the most the bot can lose; keep at least that much free USDT in the account. `ownership: account` hands the bot the whole spot account and is only for a dedicated account.

**Before real money**:

- You have read the validation status above and accept that the strategy has not passed the gate.
- API key with spot trading only and withdrawals off. Restrict it to your IP only if the IP is static; otherwise every signed call fails after your provider changes it. `tbot doctor config/live.yaml` reads the key's permissions and fails if it can withdraw.
- Windows time sync on (Settings, Time & language, Sync now). The bot uses Binance's clock either way and warns above one second.
- Sleep and hibernation off, so the PC keeps running: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
- The supervisor (below), Telegram, and the heartbeat.
- Two weeks of paper and a testnet run without surprises: `tbot compare` says the paper run tracks the backtest. A gap in fills or prices means the backtest, and so the validation, is too optimistic.
- `tbot doctor config/live.yaml` reports 0 problems.

Every position carries an exchange-side stop `protective_stop_pct` below the last close, so a dead bot still has bounded loss. The stop's limit sits 0.5% under the trigger; in a gap through both it may not fill. A stop that cannot be placed is alerted and retried at every reconciliation. Paper and the backtest have no such stop. At 20% it would have fired twice on 2018-2026 history (ETH, January 2018 and August 2020), each time selling well under the bar's close before the strategy bought back.

## Running unattended

- **Working directory**: `.env`, `data/`, `logs/`, and the ledger paths are relative to it. Always start the bot from the repository root, for example `uv --directory D:\Project\Trading-Bot run python -m tbot paper config/paper.yaml`.
- **Supervisor**: from an elevated PowerShell, `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Config config\paper.yaml` (add `-Live` for real money, `-DryRun` to only show the task) runs `tbot doctor`, refuses while it reports a problem, and registers a task that starts at boot, whether anyone is logged on or not. Start it the first time with `Start-ScheduledTask -TaskName "tbot paper"`. The task runs `scripts\run_bot.ps1`, which starts the bot again after a crash (Task Scheduler's own restart option only covers a task that fails to launch).
- **Exit codes**: `1` is a crash, a failed start, or no network yet: the supervisor starts the bot again after a minute, then after twice the previous wait, up to 15 minutes, and every failed start is alerted on Telegram. `0` (stopped on purpose), `3` (the ledger is in use by another process, damaged, or SQLite cannot load), and `4` (the config or the command is wrong) end the supervisor, since retrying cannot help; the reason goes to Telegram when it can be sent. `logs/supervisor.log` records every start and exit, `logs/console.txt` the last run's console.
- **Stopping and updating**: `tbot stop config/paper.yaml` lets the event in progress finish its orders and stops the session, which also ends the supervisor. To update: `tbot stop ...`, `git pull`, `uv sync --frozen`, then `Start-ScheduledTask -TaskName "tbot paper"`. `Stop-ScheduledTask` is a hard kill. Stopping never sells anything; exchange stops stay in place. After a hard kill or a reboot, the next start books any order whose result was not recorded and reconciles with the exchange.
- **One process per ledger**: a second session on the same config refuses to start. Paper and testnet can run side by side; they share `data/` safely.
- **Kill switch**: a 45% drawdown from the peak sells everything and halts (`guard.max_drawdown`, also the default). It sits beyond the strategy's normal drawdowns so that it catches a broken strategy, not a bad month; at 15% the backtest halts in November 2018 and never trades again, and `tbot doctor` warns when the configured level would have halted on the stored history. It stays halted across restarts until `tbot resume`, which also restarts the drawdown count from the current equity. `initial_cash` is still the most the bot can lose.
- **Deposits and withdrawals**: in budget mode extra money in the account is ignored. Taking out more than the bot's cash shrinks its book, and the guard treats that as a transfer, not a loss.
- **Logs**: `logs/<mode>.jsonl` rotates at 20 MB with 5 backups. Set `TBOT_DEBUG=1` to see a full traceback instead of the one-line error a command prints.

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

## License

No license has been chosen yet, so all rights are reserved by the author. Choose one before inviting others to use or change the code.
