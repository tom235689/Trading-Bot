# Trading Bot

Multi-strategy crypto trading bot for Binance spot: data, backtests, a validation pipeline, paper trading, and live trading with a risk guard. See [docs/DESIGN.md](docs/DESIGN.md) for how it works and [CHANGELOG.md](CHANGELOG.md) for what changed.

**Risk**: trading crypto can lose all the money involved. No strategy here has passed the project's own validation gate (see Validation below). The software comes with no warranty; use real money only with an amount you can afford to lose.

## Quick start (Windows)

You need [uv](https://docs.astral.sh/uv/) (`winget install --id astral-sh.uv -e`) and [Git](https://git-scm.com/download/win). In PowerShell:

```powershell
git clone https://github.com/tom235689/Trading-Bot.git
cd Trading-Bot
.\tbot setup
```

Setup builds the Python environment, creates `.env`, downloads the market data (a few minutes), adds the folder to your PATH, and checks everything with `tbot doctor`. Then, in a new terminal:

```powershell
tbot notify      # Telegram alerts, guided (optional)
tbot paper       # paper trading on live prices; Ctrl+C stops it
tbot status      # from another terminal: every session at a glance
```

`tbot` is `tbot.cmd` in the repository folder. It always runs in that folder, where `.env`, `config\`, `data\`, and `logs\` live, so it works from any directory; relative paths you pass are relative to the repository. Before setup has changed PATH, or with `-NoPath`, type `.\tbot` in the folder. After Ctrl+C, cmd asks "Terminate batch job (Y/N)?": the bot has already stopped, so either answer is fine.

**Other systems**: from the repository root, `uv sync --frozen`, `cp .env.example .env`, `uv run tbot download`, then `uv run tbot ...` wherever this README says `tbot ...`.

## Everyday use

| Command | What it does |
|---|---|
| `tbot status` | every session (paper, testnet, live): running or not, equity, profit, 24-hour change, drawdown from the peak, positions, and anything halted |
| `tbot status paper` | one session in detail: fills, events, stored bars, backups |
| `tbot log -f` | the session's log as readable lines, following new ones (`-n 100` for more, `--level warning` for problems only) |
| `tbot dashboard` | an HTML report with equity and drawdown charts, opened in the browser |
| `tbot stop` | the running session finishes its event and stops |
| `tbot doctor` | checks strategies, the ledger, backups, alerts, the network, the clock, and the kill switch |
| `tbot compare` | replays the session's period through the backtest: does paper track it? |
| `tbot backup` | a copy of the ledger now, also while it runs |
| `tbot resume` | clears the kill switch of a halted session |
| `tbot autostart paper` | runs the session at every boot and restarts it after crashes (asks for administrator rights) |
| `tbot update` | pulls the new version and restarts scheduled sessions |

**Configs by name**: a config is a file or a name from `config\`: `tbot status testnet` reads `config\testnet.yaml`, `tbot backtest donchian_voltarget` reads `config\donchian_voltarget.yaml`. Leave it out and session commands pick the running session, else the only one that has run, else paper; with two candidates they ask you to name one, and they print which config they used. `tbot paper` defaults to `config\paper.yaml`, `tbot stop` to the running session, and `tbot resume` to the halted one; `tbot live` always needs the config named. `tbot -h` lists every command, `tbot <command> -h` explains one.

## Setup details

The bot runs from a clone of the repository: configs, scripts, and the trial log live there. `tbot setup` (`scripts\setup.ps1`) can run again at any time while no bot runs from this folder (it refuses otherwise): every step skips what is already done. `-SkipDownload` leaves the market data for later (sessions download what they need at start), `-NoPath` leaves your PATH alone. The PATH entry goes to your user PATH (Settings, System, About, Advanced system settings, Environment Variables, to remove it); a second clone does not replace the first one's entry.

With Smart App Control on (Windows 11), unsigned Python files can be blocked (`An Application Control policy has blocked this file`), even files that worked yesterday. Setup detects it and builds the environment on Python signed by the Python Software Foundation, which the policy allows: an existing python.org install, or the official `python` package from nuget.org, unpacked to `%LOCALAPPDATA%\tbot` after its signature is checked. `tbot` runs `uv run python -m tbot`, so the generated `tbot.exe` launcher, which the policy can block, is never needed.

## Market data

```sh
tbot download                    # BTCUSDT, ETHUSDT 1h/4h since 2017-08 into data/
tbot check                       # quality report; exit code 1 on errors
tbot download --symbols SOLUSDT --timeframes 4h --start 2021-01-01
```

Downloads extend forward from the last stored bar and back to `--start` when that is earlier than the first stored bar, so running `paper` first and `download` later still gives the full history. Months missing from the archives come from the REST API; a sync that fails part way leaves no hole, and the next one carries on. A symbol Binance no longer lists keeps the history its archives have. To re-download a stream from scratch, delete `data/binance/spot/klines/<SYMBOL>/<timeframe>/`.

## Backtest

```sh
tbot backtest donchian_voltarget
```

A config lists strategies with symbols, timeframe, allocation, and params, plus costs, risk limits (including volatility targeting), rebalance rules, and optionally the session `guard` (then the backtest stops trading where the kill switch would). Strategies are plugins registered by name in `src/tbot/strategies/` (`donchian_trend`, `rsi_reversion`).

```sh
tbot backtest multi --attribution                           # each strategy alone, the mix, correlation
tbot backtest donchian_voltarget --html reports/voltarget.html   # opened in the browser
```

## Validation

```sh
tbot validate donchian_voltarget_validation
```

Runs the in-sample baseline, cost stress, parameter sweep, walk-forward, Monte Carlo, deflated Sharpe, and a single holdout evaluation, then checks the promotion gate. Exit code 1 means the gate failed. Every backtest is appended to `data/trials.jsonl`, which is kept in git: it is the record of how many things were tried, and the deflated Sharpe depends on it. See [docs/reports/](docs/reports/).

**Status**: no configuration has passed the gate ([report](docs/reports/donchian_voltarget_2026-10-05.md)). Both Donchian candidates fail it out of sample:

| Candidate | OOS Sharpe | OOS max drawdown | Monte Carlo max drawdown, median / bad 5%: in-sample, out-of-sample | In-sample drawdown |
|---|---|---|---|---|
| volatility targeting 0.4 (configured) | 0.79 | -43.5% | -29% / -45%, -34% / -55% | -24.9% |
| full exposure | 0.95 | -28.5% | -48% / -69%, -41% / -63% | -40.1% |

The Monte Carlo resamples the returns of every 4h bar in 20-day blocks, as the kill switch sees them: the in-sample column covers 7 years of the chosen params, the out-of-sample column the 4 years of walk-forward test segments, which the params were not chosen on. The in-sample edge is significant after 115 logged trials. The paper, testnet, and live configs use the volatility-targeted settings, which carry far less risk in every in-sample measure; its weaker walk-forward result comes from one 2022 parameter choice that a near tie decided. Over four years, expect a worst drawdown around 34% and, in a bad stretch, about 55%. The design rule is that real money waits for a candidate that passes; trading this one anyway is the owner's decision.

## Paper trading

```sh
tbot notify          # Telegram alerts, guided (see Telegram below)
tbot doctor          # strategies, ledger, backups, alerts, network, kill switch
tbot paper           # runs until stopped; logs to logs/paper.jsonl
tbot status          # running or not, equity, profit, positions
tbot compare         # the same period through the backtest: does it track?
tbot dashboard       # reports/paper.html, opened in the browser
tbot backup          # a copy of the ledger now, also while it runs
tbot stop            # a running session finishes its event and stops
```

The bot syncs the bar store, rebuilds its book from the ledger (`data/paper.sqlite`) and its strategy state from stored bars plus the targets saved at the last event, then trades on closed 4h bars from the Binance WebSocket with simulated fills at the live book price. A bar that closed while the bot was down for a few minutes (a restart, a crash) is still traded when it comes back, as long as it is within the guard's `stale_seconds`. On start it prints the ledger, the log file, and how to stop it.

**Telegram**: run `tbot notify` and follow it. It asks for the token of a new bot (in Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`), checks it, asks you to send your bot any message, finds your chat, saves both in `.env` (`TBOT_TELEGRAM_TOKEN`, `TBOT_TELEGRAM_CHAT_ID`; the rest of the file stays as it was), and sends a test message. Run it again at any time for a test message. To edit `.env` by hand, save it as UTF-8 (Notepad's default; PowerShell 5.1's `Out-File` writes UTF-16, which the bot rejects with a clear message).

Alerts cover starts and stops, fills, the kill switch, paused trading, stops and orders that need attention, and a daily summary (equity, change over 24 hours, drawdown from the peak, positions, the last bar event, and anything halted or paused). They are sent in the background, so a slow Telegram never holds up an order.

**Telegram commands**: with `telegram_commands: true` (set in `config/paper.yaml`) the session answers `/status` (the summary, now; `/start` too) and `/fills` (the last fills) in your chat, and any other message with the list of commands. Nothing it answers trades or stops the bot, other chats are ignored, and an odd reply from Telegram only pauses the polling for a minute. Only one process per bot token can read its messages, so turn it on for the session you watch and off in the others (`config/testnet.yaml` and `config/live.yaml` ship with it off).

**Heartbeat**: set `TBOT_HEARTBEAT_URL` in `.env` to a full `https://` ping URL from a monitor such as healthchecks.io; `tbot doctor` pings it once. It is the only way to hear about a bot that was killed or a PC that went down. It means "alive", not "trading": a halted bot keeps pinging, and Telegram tells you why it halted.

## Testnet and live trading

```sh
# .env: TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET (testnet keys from testnet.binance.vision)
tbot doctor testnet             # keys, trading permission, budget, alerts
tbot account testnet            # balances and open orders
tbot live testnet               # real order path, fake money
tbot live live --live           # real money; the flag is mandatory
tbot status live
tbot resume live                # clear the kill switch; a running bot picks it up
```

**What the bot owns**: with `ownership: budget` (the default) the bot manages `initial_cash` USDT and what it buys with it. Everything else in the account, including BTC or ETH you bought yourself, is left alone. `initial_cash` is therefore the most the bot can lose; keep at least that much free USDT in the account. Changing `initial_cash` later moves money into or out of the bot's book at the next start, like a deposit or a withdrawal: the guard, the dashboard, `tbot status`, and `tbot compare` do not count it as a profit or a loss. `ownership: account` hands the bot the whole spot account and is only for a dedicated account.

**Before real money**:

- You have read the validation status above and accept that the strategy has not passed the gate.
- API key with spot trading only and withdrawals off. Restrict it to your IP only if the IP is static; otherwise every signed call fails after your provider changes it. `tbot doctor live` reads the key's permissions and fails if it can withdraw.
- Windows time sync on (Settings, Time & language, Sync now). The bot uses Binance's clock either way and warns above one second.
- Sleep and hibernation off, so the PC keeps running: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
- Autostart (below), Telegram, and the heartbeat.
- Two weeks of paper and a testnet run without surprises: `tbot compare` says the paper run tracks the backtest. A gap in fills or prices means the backtest, and so the validation, is too optimistic.
- `tbot doctor live` reports 0 problems.

Every position carries an exchange-side stop `protective_stop_pct` below the last close, so a dead bot still has bounded loss. Stops are replaced after every bar and whenever reconciliation books something that changed the position (an order whose result came late, a stop that executed). The stop's limit sits 0.5% under the trigger; in a gap through both it may not fill. A stop that cannot be placed is alerted and retried at every reconciliation. Paper and the backtest have no such stop. At 20% it would have fired twice on 2018-2026 history (ETH, January 2018 and August 2020), each time selling well under the bar's close before the strategy bought back.

## Running unattended

- **Autostart**: `tbot autostart paper` (add `-Live` for a config with `mode: live`, `-DryRun` to only show the task, `-NoStart` to wait for the next boot) runs `tbot doctor`, refuses while it reports a problem, registers a scheduled task that starts the session at boot, whether anyone is logged on or not, and starts it now. Tasks that start at boot need administrator rights: from a normal terminal it asks for them, goes on in a new window, and keeps that window open until you press Enter. If a session started by hand already runs on the config, the task waits for the next boot. The task runs `scripts\run_bot.ps1`, which starts the bot again after a crash (Task Scheduler's own restart option only covers a task that fails to launch). `tbot autostart paper -Remove` stops the bot gracefully and removes the task; the ledger, data, and logs stay. The scripts behind these commands are `scripts\install_task.ps1` and `scripts\remove_task.ps1`.
- **Working directory**: `.env`, `data/`, `logs/`, and the ledger paths are relative to the repository folder. `tbot` and the scheduled task always run there; with `uv`, start the bot from the repository root, for example `uv --directory D:\Project\Trading-Bot run python -m tbot paper`.
- **Exit codes**: `1` is a crash, a failed start, or no network yet: the supervisor starts the bot again after a minute, then after twice the previous wait, up to 15 minutes, and every failed start is alerted on Telegram. `0` (stopped on purpose), `3` (the ledger is in use by another process, damaged, or SQLite cannot load), and `4` (the config, the command line, or `.env` is wrong) end the supervisor, since retrying cannot help; the reason goes to Telegram when it can be sent. If uv cannot be found (moved or reinstalled), the supervisor says so in its log and keeps retrying; run `tbot autostart <config>` again.
- **Logs**: `tbot log <config>` shows the bot's own log, `logs/<config>.jsonl` (`--log-file` to change it), rotated at 20 MB with 5 backups (`tbot log --file logs/paper.jsonl.1` reads one). Each config also has supervisor files, `logs\<config>.supervisor.log` (every start and exit, moved to `.1` at 5 MB) and `logs\<config>.console.txt` (the last run's console). Set `TBOT_DEBUG=1` to see a full traceback instead of the one-line error a command prints.
- **Stopping**: `tbot stop` lets the event in progress finish its orders and stops the session, which also ends the supervisor. A bot the supervisor is about to restart (after a crash) stops as it starts, if that is within 20 minutes of the request; `tbot stop --cancel` withdraws a request no session has taken. `Stop-ScheduledTask` is a hard kill. Stopping never sells anything; exchange stops stay in place. After a hard kill or a reboot, the next start books any order whose result was not recorded and reconciles with the exchange.
- **Updating**: `tbot update` (`-DryRun` shows what would change) stops the scheduled sessions gracefully, pulls, syncs the packages, and starts every task again that `tbot doctor --offline` passes, whatever happened in between (the network checks are the bot's to retry); with scheduled tasks it asks for administrator rights like autostart. It ends with the old and new version. Local changes to tracked files, such as an edited config, are set aside for the pull and put back; lines appended to the trial log on both sides are both kept. If the update changes the same lines as yours, or any step fails, everything stays at the old version with your changes, and the script says "update NOT applied" and exits with 1. A bot started by hand in a console must be stopped by hand first. Read [CHANGELOG.md](CHANGELOG.md) for what changed.
- **Backups**: a running session copies its ledger every 6 hours to `data/backups/<ledger>-<date>.sqlite` and keeps `backup_days` days (14). `tbot backup` makes an extra copy at any time (`--out` to put it elsewhere: a file name, or a folder such as a synced one); those are never deleted. `tbot doctor` warns when the newest copy is far behind the ledger. To restore: stop the bot, copy the backup over the ledger (`data/paper.sqlite`; delete any `-wal` and `-shm` files next to it), start. In live mode a restored ledger misses what happened after the copy: budget reconciliation lowers the book to the exchange but never raises it, so coins bought after the copy become yours, not the bot's.
- **One process per ledger**: a second session on the same config refuses to start. Paper and testnet can run side by side; they share `data/` safely.
- **Kill switch**: a 45% drawdown from the peak sells everything and halts (`guard.max_drawdown`, also the default). It is meant to catch a broken strategy, not a bad month: in the Monte Carlo the configured strategy goes past 45% in 5% of in-sample paths and in 17% of four-year out-of-sample paths (at 55%: 0.5% and 5.5%), so a level closer to the strategy's normal drawdowns stops it for good in an ordinary bad stretch; at 15% the backtest halts in November 2018 and never trades again. `tbot doctor` warns when the configured level would have halted on the stored history. It stays halted across restarts until `tbot resume`, which also restarts the drawdown count from the current equity. `initial_cash` is still the most the bot can lose. The daily rule (`daily_loss_limit`, 3%) only blocks new entries for the rest of a bad day; it changes no fill of the 2018-2026 backtest.
- **Deposits and withdrawals**: in budget mode extra money in the account is ignored. Taking out more than the bot's cash shrinks its book, and the guard treats that as a transfer, not a loss.

## Development

```sh
git config core.hooksPath .githooks     # once per clone (setup does it)
uv run python -m ruff check .
uv run python -m ruff format .
uv run python -m mypy
uv run python -m pytest
```

The hooks are shell scripts (Git for Windows runs them):

| Hook | Checks |
|---|---|
| `pre-commit` | Hangul and secrets in staged changes, ruff, mypy |
| `commit-msg` | Hangul and secrets in the message |
| `pre-push` | Hangul and secrets in every outgoing commit and tag, tests |

Run a full scan of tracked files and history with `uv run python scripts/git_guard.py audit`. Mark a known false-positive secret line with `guard: allow-secret`.

## License

No license has been chosen yet, so all rights are reserved by the author. Choose one before inviting others to use or change the code.
