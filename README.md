# Trading Bot

Multi-strategy crypto trading bot for Binance spot: data, backtests, a validation pipeline, paper trading, and live trading with a risk guard. See [docs/DESIGN.md](docs/DESIGN.md) for how it works, [docs/CONFIG.md](docs/CONFIG.md) for every setting, and [CHANGELOG.md](CHANGELOG.md) for what changed.

**Risk**: trading crypto can lose all the money involved. No strategy here has passed the project's own validation gate (see Validation below). The software comes with no warranty and is no financial advice; use real money only with an amount you can afford to lose.

## Quick start (Windows)

You need [uv](https://docs.astral.sh/uv/) (`winget install --id astral-sh.uv -e`) and [Git](https://git-scm.com/download/win); open a new terminal after installing them, so it finds them. In PowerShell:

```powershell
git clone https://github.com/tom235689/Trading-Bot.git
cd Trading-Bot
.\tbot setup
```

Setup builds the Python environment, creates `.env`, downloads the market data (a few minutes), adds the folder to your PATH, and checks everything with `tbot doctor`. The folder may have any name, also one with `[` or `]`. The data library (polars) needs a processor with AVX2, FMA, and BMI (most PCs made since 2015); on an older one setup stops and says so: run the bot on a newer PC. Then, in a new terminal:

```powershell
tbot notify      # Telegram alerts, guided (optional)
tbot paper       # paper trading on live prices; Ctrl+C stops it
tbot status      # from another terminal: every session at a glance
```

Paper trading uses simulated money (`initial_cash`, 10,000 USDT in `config\paper.yaml`) at live prices. The configured strategy trades about once every two weeks on 4h bars, so days or weeks in cash, with `orders=0` in the log at every close, are normal: it waits for a breakout.

`tbot` is `tbot.cmd` in the repository folder. It always runs in that folder, where `.env`, `config\`, `data\`, and `logs\` live, so it works from any directory; relative paths you pass are relative to the repository. `python -m tbot` from a clone (any system) also runs in the repository, but a file you name, such as `./my.yaml` or `--out x.html`, is taken from the folder you typed the command in. Before setup has changed PATH, or with `-NoPath`, type `.\tbot` in the folder. After Ctrl+C, cmd asks "Terminate batch job (Y/N)?": the bot has already stopped, so either answer is fine.

Binance must serve your region: binance.com refuses some countries, including the US, and `tbot doctor` then fails its network check. Do not reach it through a VPN from such a region: that breaks Binance's terms, and accounts can be frozen.

**Behind a proxy**: set `HTTPS_PROXY` as a user environment variable (not in `.env`, which holds only `TBOT_` settings); with a proxy that inspects TLS, also `SSL_CERT_FILE` (its certificate) and `UV_NATIVE_TLS=1` for uv.

**Linux and macOS**: from the repository root, `uv sync --frozen`, `cp .env.example .env && chmod 600 .env`, `uv run tbot download`, then `uv run tbot ...` wherever this README says `tbot ...`. An alias makes it work from any folder: `alias tbot='uv run --project ~/Trading-Bot --frozen python -m tbot'`. To run a session unattended, see [Linux and macOS](#linux-and-macos) below.

## Everyday use

| Command | What it does |
|---|---|
| `tbot status` | every session (paper, testnet, live): running or not, equity, profit, 24-hour change, drawdown from the peak, positions, and anything halted |
| `tbot status paper` | one session in detail: fills, events, stored bars, backups |
| `tbot log -f` | the session's log as readable lines, following new ones (`-n 100` for more, `--level warning` for problems only) |
| `tbot dashboard` | an HTML report with equity and drawdown charts, opened in the browser |
| `tbot stop` | the running session finishes its event and stops; it sells nothing |
| `tbot halt live` | **the panic button**: the session sells what the bot holds at once and trades no more until `tbot resume` (Telegram: `/halt`; `tbot halt --all`: every session at once) |
| `tbot doctor` | checks strategies, the ledger, backups, alerts, the network, the clock, and the kill switch |
| `tbot compare` | replays the session's period through the backtest: does paper track it? |
| `tbot backup` | a copy of the ledger now, also while it runs |
| `tbot export paper` | every trade and transfer of the session in a CSV file (`reports\paper-trades.csv`; `--year 2026` for one year), for a spreadsheet or a tax return |
| `tbot resume` | clears the kill switch of a halted session, after saying where the book stands |
| `tbot keys testnet` | asks for Binance API keys, checks them with Binance, and saves them in `.env` (`tbot keys live` for real money) |
| `tbot autostart paper` | runs the session at every boot and restarts it after crashes (asks for administrator rights) |
| `tbot update` | installs the newest release and restarts scheduled sessions |
| `tbot --version` | the version; `tbot status`, `tbot doctor`, and the start alert name it too, and doctor and the daily summary say when a newer release (a published GitHub release) is out |
| `tbot support` | a report to share when asking for help: versions, each session's state and offline checks, recent warnings; no keys or tokens (attach an edited config yourself) |

**Configs by name**: a config is a file or a name from `config\`: `tbot status testnet` reads `config\testnet.yaml`, `tbot backtest donchian_voltarget` reads `config\donchian_voltarget.yaml`. Leave it out and session commands pick the running session, else the only one that has run, else paper; with two candidates they ask you to name one, and they print which config they used. `tbot paper` defaults to `config\paper.yaml`, `tbot stop` and `tbot halt` to the running session (with none running, the only one that has run), and `tbot resume` to the halted one (or the one a `tbot halt` request waits for); a session that trades real money must be named for `tbot halt` and `tbot resume`, and `tbot live` always needs the config named. A config that no longer loads (a typo while its session runs) shows in `tbot status` with its error under the session, which runs on with the settings it started with; its next start fails (exit 4), so fix the file. `tbot stop`, `tbot halt`, and `/halt` still reach the session, also after its `ledger:` was changed: every session notes its config and ledger in `data\sessions\` when it starts. Configs may end in `.yaml` or `.yml`, and a key given twice in one is an error, not a silent override. A config with `mode:` is a testnet or live config (a stray `start:` in it is an unknown key); any other that is no backtest (`start:`) or validation (`backtest:`) config is a paper config. A copied session config needs its own `ledger:`, or both keep one book, and `tbot status` and `tbot doctor` point that out. Edit a shipped config in place for its own session: `tbot update` keeps your edits, and stops only when a release changes the same lines, saying how to go on. A mistyped key gets a suggestion (`initial_cahs: unknown key (did you mean initial_cash?)`), and a config given to the wrong command names the right one. [docs/CONFIG.md](docs/CONFIG.md) explains every setting. `tbot -h` or `tbot help` lists every command, `tbot <command> -h` or `tbot help <command>` explains one (`tbot setup -h`, `tbot update -h`, and `tbot autostart -h` too).

## Setup details

The bot runs from a clone of the repository: configs, scripts, and the trial log live there. `tbot setup` (`scripts\setup.ps1`) can run again at any time: every step skips what is already done, and while a bot runs from this folder it leaves the Python environment and packages alone (stop the bot to have them rebuilt). `-SkipDownload` leaves the market data for later (sessions download what they need at start), `-NoPath` leaves your PATH alone. The PATH entry goes to your user PATH (Settings, System, About, Advanced system settings, Environment Variables, to remove it); a second clone does not replace the first one's entry.

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

`tbot backtest` and `tbot validate` say so before they run when a stream ends earlier than the others (every stream stops there) or when symbols trade on different bars (see DESIGN, Fill model). A config lists strategies with symbols, timeframe, allocation, and params, plus costs, risk limits (including volatility targeting), rebalance rules, and optionally the session `guard` (then the backtest stops trading where the kill switch would). Strategies are plugins registered by name in `src/tbot/strategies/` (`donchian_trend`, `rsi_reversion`).

```sh
tbot backtest multi --attribution                           # each strategy alone, the mix, correlation
tbot backtest donchian_voltarget --html reports/voltarget.html   # opened in the browser
```

## Validation

```sh
tbot validate donchian_voltarget_validation
```

Runs the in-sample baseline, cost stress, parameter sweep, walk-forward, Monte Carlo, and deflated Sharpe, then checks the promotion gate; only when every other check passes does it evaluate the holdout, once (`--holdout` evaluates it anyway, and every look weakens it: the report warns after three). A holdout shorter than `gate.min_holdout_days` (180) fails as too short. Exit code 1 means the gate failed. A backtest config can set `protective_stop_pct: 0.2` to simulate live's exchange stop (off by default; on `donchian_voltarget` it fires twice and lowers the return from +655% to +616%). Every `tbot backtest` and `tbot validate` run is appended to `data/trials.jsonl`, which is kept in git: it is the record of how many things were tried, and the deflated Sharpe depends on it. So `git status` shows it changed after a run; that is expected, and `tbot update` keeps the lines of both sides. A backtest whose stored bars start long after its period's start (only what a session fetched) is refused: download the history first. See [docs/reports/](docs/reports/).

**Status**: no configuration has passed the gate ([report](docs/reports/donchian_voltarget_2026-10-05.md)). Both Donchian candidates fail it out of sample:

| Candidate | OOS Sharpe | OOS max drawdown | Monte Carlo max drawdown, median / bad 5%: in-sample, out-of-sample | In-sample drawdown |
|---|---|---|---|---|
| volatility targeting 0.4 (configured) | 0.79 | -43.5% | -29% / -45%, -34% / -55% | -24.9% |
| full exposure | 0.95 | -28.5% | -48% / -69%, -41% / -63% | -40.1% |

The Monte Carlo resamples the returns of every 4h bar in 20-day blocks, as the kill switch sees them: the in-sample column covers 7 years of the chosen params, the out-of-sample column the 4 years of walk-forward test segments, which the params were not chosen on. The in-sample edge is significant after 115 logged trials. The paper, testnet, and live configs use the volatility-targeted settings, which carry far less risk in every in-sample measure; its weaker walk-forward result comes from one 2022 parameter choice that a near tie decided. Over four years, the simulation puts the worst drawdown around 34% and, in a bad stretch, about 55%: history, not a bound. The design rule is that real money waits for a candidate that passes; trading this one anyway is the owner's decision.

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

The bot syncs the bar store, rebuilds its book from the ledger (`data/paper.sqlite`) and its strategy state from stored bars plus the targets saved at the last event, then trades on closed 4h bars from the Binance WebSocket with simulated fills at the live book price. A bar that closed while the bot was down for a few minutes (a restart, a crash) is still traded when it comes back, as long as it is within the guard's `stale_seconds`. On start it prints the ledger, the log file, when its first decision comes (in UTC and your local time), and how to stop it. Keep the PC awake for the whole paper run (see Before real money): bars missed while it sleeps make `tbot compare` fail.

**Telegram**: run `tbot notify` in a terminal and follow it. It asks for the token of a new bot (in Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`), checks it, shows a one-time code to send your bot, takes the chat that sends it (bot names are public: a stranger who wrote to your bot first never gets your alerts), saves both in `.env` (`TBOT_TELEGRAM_TOKEN`, `TBOT_TELEGRAM_CHAT_ID`; the rest of the file stays as it was), and sends a test message. For alerts in a group, add the bot to the group and send the code there as a command, `/<code>@<bot name>` (`tbot notify` shows it): bots in groups receive commands only. In a group every member can send the bot's commands, `/halt` included: use a private chat, or a group of people you trust. Each clone needs a bot of its own: Telegram lets one program read a bot's messages, and a session says so when another one does. Run it again at any time for a test message, or with `--chat` to pick the chat again (a group, another phone); running sessions use a new token or chat once restarted. In a group, the bot answers only commands for it (`/status`, or `/status@<bot name>`, not another bot's) and asks for `/halt@<bot name> <code>`, which reaches it whatever other bots post. To edit `.env` by hand, save it as UTF-8 (Notepad's default; PowerShell 5.1's `Out-File`, `>`, and `>>` write UTF-16, which the bot refuses with a clear message), and `tbot doctor` names a line that sets nothing: a misspelled `TBOT_` name, or another shell's syntax such as `set X=`.

Alerts cover starts and stops, fills, the kill switch, paused trading, stops and orders that need attention, and a daily summary (equity, change over 24 hours, drawdown from the peak, positions, the last bar event, and anything halted or paused). They are sent in the background, so a slow Telegram never holds up an order. An alert Telegram cannot take (no network, Telegram down or busy) is sent again until it goes through, for up to a day, so the alert about an outage arrives when the outage ends; a stream reported stale is reported back once its bars arrive.

**Telegram commands**: with `telegram_commands: true` (the default) the session answers `/status` (the summary, now: equity, profit, positions; `/start` too), `/fills` (the last fills), and `/halt` in your chat, and any other message with the list of commands. `/halt` is the panic button: it answers with a four-digit code, and only `/halt <code>` within two minutes sells what the bot holds and halts it, so a tap on the command never does. It halts every session of the clone that runs, that its supervisor starts again, or whose stopped book holds coins (those at their next start), not only the one that answers; it names them, and any it could not reach, with the `tbot halt` to run. A `/halt` sent while no session read the chat (during a restart) is not carried out: the next session says so, and you send it again. Its answers, like alerts, are sent again until Telegram takes them. Nothing else trades, nothing resumes (`tbot resume` stays on the computer), other chats are ignored, and an odd reply from Telegram only pauses the polling for a minute. Only one process per bot token can read its messages, so the sessions take turns: a real-money session first, else the one that started first. Another program reading the same bot is reported once a day. The summary values the book at the last bar event's prices, which it names.

**Heartbeat**: set `TBOT_HEARTBEAT_URL` in `.env` to a full ping URL from a monitor such as healthchecks.io (`https://`; `http://` only for a monitor on your own network); `tbot doctor` pings it once (the monitor counts that ping as the bot alive). It is the only way to hear about a bot that was killed or a PC that went down. A ping means the bot runs, receives market data, and can alert you: pings pause while a stream is stale, new buys wait for reconciliation (Binance unreachable, an IP ban, a revoked key), Telegram refuses the alerts (a revoked token, a blocked bot), or an alert has not got through for 15 minutes (Telegram unreachable), so the monitor reports the bot down; they come back once alerts get through again. A halted bot keeps pinging, and Telegram tells you why it halted. Keep the URL secret: anyone who has it can report the bot alive.

## Testnet and live trading

```sh
tbot keys testnet               # asks for the keys, checks them, saves them in .env
tbot doctor testnet             # keys, trading permission, budget, alerts
tbot account testnet            # balances and open orders
tbot live testnet               # real order path, fake money
tbot keys live                  # the real-money keys; testnet keeps its own
tbot live live --live           # real money; the flag is mandatory
tbot status live
tbot halt live                  # sell what the bot holds and stop trading, now
tbot resume live                # clear the kill switch; a running bot picks it up
```

**Testnet keys**: sign in at [testnet.binance.vision](https://testnet.binance.vision) with GitHub, choose "Generate HMAC_SHA256 Key", and give the key and secret to `tbot keys testnet`; the testnet account comes with test funds. `tbot keys` hides the secret as you paste it, checks the pair with Binance, and saves it only when it works: testnet keys as `TBOT_BINANCE_TESTNET_API_KEY` and `_SECRET`, live keys as `TBOT_BINANCE_API_KEY` and `_SECRET` (a testnet key there moves to the testnet pair, so testnet keeps running). It refuses a live key that can withdraw or cannot trade spot.

**What the bot owns**: with `ownership: budget` (the default) the bot manages `initial_cash` USDT and what it buys with it. Everything else in the account, including BTC or ETH you bought yourself, is left alone. `initial_cash` is therefore the most the bot can lose, and a real-money config must set it. A new book, or a raised budget, starts only when the account's free USDT covers it, so a mistyped `initial_cash: 10000` stops at the start instead of taking all the USDT there is; the first real-money start, and one after a raise, started by hand asks you to type the amount; and every real-money start alert names the budget. Changing `initial_cash` later moves money into or out of the bot's book at the next start, like a deposit or a withdrawal: the guard, the dashboard, `tbot status`, and `tbot compare` do not count it as a profit or a loss. `ownership: account` hands the bot the whole spot account and is only for a dedicated account. Amounts in a config (`initial_cash`, `rebalance.min_notional`) count in the symbols' quote asset: the shipped configs trade USDT pairs; for pairs quoted in BTC, give them in BTC (a start refuses an order minimum far above Binance's, which would send no order).

**Before real money**:

- You have read the validation status above and accept that the strategy has not passed the gate.
- API key with spot trading only and withdrawals off, created as a system-generated (HMAC) key: tbot does not sign with Ed25519 or RSA keys; `tbot keys live` checks all of that before it saves the key. Leave margin, futures, options, and transfers off too (doctor warns about them). Restrict it to your IP only if the IP is static; otherwise every signed call fails after your provider changes it. `tbot doctor live` reads the key's permissions and fails if it can withdraw, and a live session refuses to start with such a key. Best of all, trade from a Binance sub-account that holds only the bot's budget: a stolen trading key can still lose an account's money by trading it.
- A budget (`initial_cash`) of at least about 300 USDT: below that, Binance's 10 USDT order minimum skips small rebalances and results drift from the backtest. Fees are assumed at 0.1% per fill (spot taker, VIP 0); with a BNB discount or a higher VIP level, lower `costs.fee_rate`.
- Windows time sync on (Settings, Time & language, Sync now). The bot uses Binance's clock either way; `tbot doctor` warns when this computer is more than three seconds ahead of or behind it.
- Sleep and hibernation off, so the PC keeps running: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0` (on a laptop also the `-dc` ones, for battery). A running session keeps Windows from going to sleep when idle, but not when the lid is closed or the sleep button is pressed.
- Autostart (below), Telegram, and the heartbeat. `tbot doctor` fails a live config without Telegram, and `tbot live` refuses to start one, or one whose token or chat Telegram refuses: a real-money bot must be able to say that something went wrong. Online, doctor also asks Telegram whether it knows the token and the chat (nothing is sent).
- You know the way out: `tbot halt live` on the computer, or `/halt` in the Telegram chat, sells what the bot holds and stops its trading.
- Paper and a testnet run without surprises, until `tbot compare` says the paper run tracks the backtest over at least 10 matched fills (with about one trade every two weeks, months). A gap in fills or prices means the backtest, and so the validation, is too optimistic.
- `tbot doctor live` reports 0 problems.
- You can check the bot against Binance: its orders carry client ids starting with `tb`, its stops with `tbs`; `tbot account live` shows the exchange's balances and open orders, `tbot status live` the book, and reconciliation's adjustments (in `tbot status live` and `tbot export live`) are money that moved from outside.

Every position carries an exchange-side stop `protective_stop_pct` below the last close, so a dead bot still has bounded loss. The stop follows the last close down as well as up: it bounds the fall from the last close, not from the price paid. Stops are replaced after every bar and whenever reconciliation books something that changed the position (an order whose result came late, a stop that executed). The stop is a stop-market order: once triggered it sells at the market, so a gap cannot leave it unfilled, though in a crash it can fill well under the trigger (Binance refuses a stop-limit priced that far under the market). A stop that cannot be placed is alerted and retried at every reconciliation; one that covers only part of a position (other orders lock the rest) is alerted too. When the price has already fallen through the new level as a stop is refitted (after a restart, or at a reconciliation between bars), the stop in place stays, since Binance refuses one that would trigger at once; a position without one gets it `protective_stop_pct` under the current price. `protective_stop_pct: 0` cancels a stop placed while stops were on. After a stop has sold, the strategy decides again at the next close and may buy back; `tbot halt` keeps it out. A stop is never larger than the largest order Binance takes: beyond it, a position is covered in part, which is alerted. Paper has no such stop; a backtest simulates it when its config sets `protective_stop_pct` (off by default). At 20% it would have fired twice on 2018-2026 history (ETH, January 2018 and August 2020), each time selling well under the bar's close before the strategy bought back.

## Running unattended

- **Autostart**: `tbot autostart paper` (add `-Live` for a config with `mode: live`, `-DryRun` to only show the task, `-NoStart` to wait for the next boot) runs `tbot doctor`, refuses while it reports a problem (when only its network checks fail, it says so: try again later, or pass `-SkipDoctor`), registers a scheduled task that starts the session at boot, whether anyone is logged on or not, at normal priority, starts it now, and checks ten seconds later that it still runs and that the bot started. The task is named after the config's file name; a second config with the same file name in another folder is refused: pass `-Name`. Tasks that start at boot need administrator rights: from a normal terminal it checks everything first, then asks for them, goes on in a new window, and keeps that window open until you press Enter. If a session started by hand already runs on the config, the task waits for the next boot. The task runs `scripts\run_bot.ps1`, which starts the bot again after a crash (Task Scheduler's own restart option only covers a task that fails to launch). `tbot autostart paper -Remove` stops the bot gracefully and removes the task (`-Remove -DryRun` only says so); the ledger, data, and logs stay. The scripts behind these commands are `scripts\install_task.ps1` and `scripts\remove_task.ps1`.
- **Working directory**: `.env`, `data/`, `logs/`, and the ledger paths are relative to the repository folder. `tbot`, `python -m tbot` from a clone, and the scheduled task always run there, wherever the command is typed (`uv run --project <repo> python -m tbot paper` works from any folder).
- **Exit codes**: `1` is a crash, a failed start, or no network yet: the supervisor starts the bot again after a minute, then after twice the previous wait, up to 15 minutes, and every failed start is alerted on Telegram. `0` (stopped on purpose), `3` (the ledger is in use by another process, damaged, or SQLite cannot load), and `4` (the config, the command line, or `.env` is wrong, including missing API keys, a key whose signature Binance rejects (a wrong secret, or an Ed25519 or RSA key), a symbol Binance does not list, or a ledger kept for another mode) end the supervisor, since retrying cannot help; the reason goes to Telegram when it can be sent (a supervised start tries for about two minutes, as the network may come up after the task), except for exit 3 because another process already runs on the ledger, which only the supervisor log records. If uv cannot be found (moved or reinstalled) or cannot be started, the supervisor says so in its log, tries twice more, and ends (exit 4): fix that (install uv again), then run `tbot autostart <config>` again.
- **Logs**: `tbot log <config>` shows the bot's own log, `logs/<config>.jsonl` (a config outside `config\` adds a short hash of its path to the name), rotated at 20 MB with 5 backups (`tbot log --file logs/paper.jsonl.1` reads one; a session started with `--log-file` is read with `tbot log --file <that file>`). On Windows each scheduled config also has supervisor files, `logs\<config>.supervisor.log` (every start and exit, moved to `.1` at 5 MB) and `logs\<config>.console.txt` (the last run's warnings and errors, the run before in `.1`; the full log is the JSON file). For a full traceback instead of the one-line error a command prints, set `TBOT_DEBUG` (PowerShell: `$env:TBOT_DEBUG = 1`, cmd: `set TBOT_DEBUG=1`).
- **Getting out**: `tbot halt <config>` (Telegram: `/halt`, then the code it answers) is the panic button. The running session books any stop that executed, cancels its exchange stops, sells what the bot holds at the market price within seconds, and stays halted until `tbot resume`: it keeps running, reconciling, and alerting, but buys nothing. A sale that fails (a rate limit, an exchange outage) is tried again a minute later, then less often, up to every 15 minutes, until it goes through, and `tbot halt` says what is not sold yet and exits 1. A session that is not running is marked halted, and its next start sells at once (until then its exchange stops stay in place). Telegram's `/halt`, and `tbot halt --all` on the computer, halt every session of the clone: those that run, and at their next start those a supervisor starts again (between its restarts, or at the next boot) and stopped books that hold coins; they name them. Dust below the exchange minimum cannot be sold, so no message counts it as held. `tbot resume` also withdraws a halt request no session took. Everything else in the account is left alone, as always in budget mode. Selling by hand on Binance while the bot runs is no way out: reconciliation books it as a withdrawal, and the next bar buys back to the strategy's target with the cash the book still holds.
- **If the bot cannot sell** (a revoked key, a changed IP, an API outage while the Binance app works, a dead PC), sell by hand in this order. First keep the bot from buying back: `tbot halt <config>` (a stopped book is marked halted). Then cancel the bot's open stop orders in Binance (client ids starting with `tbs`): they lock the coins. Then sell. The next start books the sale as a withdrawal and stays halted until `tbot resume`. A dead PC cannot be marked: when it comes back, its scheduled task starts the bot, which may buy back at the next close, so run `tbot halt <config>` (or `/halt`) as soon as it is up.
- **Stopping**: `tbot stop` lets the event in progress finish its orders and stops the session, which also ends the supervisor. It sells nothing: it says which positions stay (with their exchange stops), and for a scheduled session that its task starts it again at the next boot (`tbot autostart <config> -Remove` prevents that). A bot the supervisor is about to restart (after a crash) stops as it starts, if that is within 20 minutes of the request; `tbot stop` with no session running leaves such a request when only one session has run (and mentions the supervisor only when one started it last), and `tbot stop --cancel` withdraws it. A session you start by hand drops a waiting request instead of stopping. To start a scheduled session again, run `tbot autostart <config>` (it withdraws the request), or reboot. `Stop-ScheduledTask` is a hard kill. Stopping never sells anything; exchange stops stay in place. A stop the end of an event cut short (cancelled, not yet replaced) is placed before the session ends; one still missing is named in the stopped alert (`NO EXCHANGE STOP`) and placed at the next start. After a hard kill or a reboot, the next start books any order whose result was not recorded and reconciles with the exchange.
- **Updating**: `tbot update` (`-DryRun` shows what would change) installs the newest release: the newest `vX.Y.Z` tag with a published GitHub release (a version whose checks failed gets a tag but no release, and is skipped; when GitHub cannot be asked it takes the newest tag and says so). First it checks that the update can apply: the clone is on its branch, has no commits of its own, and has no untracked file where the release adds one; if not, it refuses before anything is stopped. A clone already at or past the newest release is up to date, and nothing is stopped. Then it stops the scheduled sessions gracefully, moves to the release (`git merge --ff-only <tag>`, so the clone stays on its branch), syncs the packages, and checks the new version: it must load on this PC, and `tbot doctor --offline` must still pass on every config whose task it stopped. Otherwise it goes back to the old version, syncs it, starts it again, says so, and exits with 1. Every task that was running is started again, unless the session could no longer start: `tbot doctor --offline` fails only on that (a config the new version rejects, a ledger of another mode, missing API keys, a real-money config without Telegram) and shows everything else, such as a halted kill switch, as a warning (the network checks are the bot's to retry). Ten seconds after each start it checks that the bot runs. With scheduled tasks it asks for administrator rights like autostart. It ends with the old and new version. Local changes to tracked files, such as an edited config, are set aside for the update and put back; lines appended to the trial log on both sides are both kept. If the update changes the same lines as yours, or any step fails, everything stays at the old version with your changes, and the script says "update NOT applied", how to go on, and exits with 1. A bot started by hand in a console must be stopped by hand first. Read [CHANGELOG.md](CHANGELOG.md) for what changed.
- **Backups**: a running session copies its ledger every 6 hours to `backups/<ledger>-<date>.sqlite` next to the ledger (`data/backups/` for the shipped configs) and keeps `backup_days` days (14). `tbot backup` makes an extra copy at any time (`--out` to put it elsewhere: a new file name, or a folder such as a synced one; an existing file is never written over); those are never deleted. `tbot doctor` warns when the newest copy is far behind the ledger. To restore: stop the bot, copy the backup over the ledger (`data/paper.sqlite`; delete any `-wal` and `-shm` files next to it), start. In live mode a restored ledger misses what happened after the copy: budget reconciliation lowers the book to the exchange but never raises it, so coins bought after the copy become yours, not the bot's.
- **One process per ledger**: a second session on the same config refuses to start. Paper and testnet can run side by side; they share `data/` safely.
- **Kill switch**: a 45% drawdown from the peak sells everything and halts (`guard.max_drawdown`, also the default). It is meant to catch a broken strategy, not a bad month: in the Monte Carlo the configured strategy goes past 45% in 5% of in-sample paths and in 17% of four-year out-of-sample paths (at 55%: 0.5% and 5.5%), so a level closer to the strategy's normal drawdowns stops it for good in an ordinary bad stretch; at 15% the backtest halts in November 2018 and never trades again. `tbot doctor` warns when the configured level would have halted on the stored history. It stays halted across restarts until `tbot resume`, which also restarts the drawdown count from the current equity: so it first says where the book stands, and for real money asks you to type `resume`; a running session announces the resume. `initial_cash` is still the most the bot can lose. The daily rule (`daily_loss_limit`, 3%) only blocks new entries while equity is that far below the day's open (UTC; the bar that closes at midnight counts for the day it ends); on the 2018-2026 backtest it changes no trade and the final equity by less than 0.01%.
- **Deposits and withdrawals**: in budget mode extra money in the account is ignored. Coins of the bot's book sold by hand count as a withdrawal from its book, and its strategy buys back at the next bar (use `tbot halt` to get out). Taking out more than the bot's cash shrinks its book, and the guard treats that as a transfer, not a loss. Lowering `initial_cash` by more than the cash the bot holds leaves its book owing the rest: it buys nothing until sales have repaid it. A cut larger than everything the book is worth at the latest closes is refused at start, with the lowest value `initial_cash` can take.
- **One ledger per mode**: a ledger remembers whether it keeps a paper, testnet, or live book, and a session of another mode refuses it (exit 4), so a testnet book never trades real money. Promote a config by giving it a new `ledger:`. Two sessions may share one account in budget mode: each tells its orders and stops apart from the other's.
- **Binance pauses a pair** (testnet and live): while Binance does not trade a symbol (status `BREAK`, during maintenance or before a delisting), its orders and stops wait, the rest trades on, and Telegram says when it stops and when it trades again. Symbol rules (price and lot steps, minimums) are read again every hour and after Binance refuses an order for them. A pair Binance stops listing is treated the same way (status `UNLISTED`), and the others go on.
- **Network outages**: a lost connection is retried for every read; an order is never sent twice (it is looked up by its id instead). A rate limit or IP ban Binance asks the bot to wait out is waited out without further requests. While reconciliation fails three times in a row, or a bar arrives later than `guard.stale_seconds`, the bot buys nothing, but its sales still go out. The session reconnects and catches up on its own, however long the outage; a socket that stays open but goes silent for two minutes is replaced. A socket that cannot connect tries port 443 after 9443, and 15 minutes without one is alerted once (bars still come by REST, up to 35 s late). An order whose answer was lost counts as never sent only once Binance still does not know it two minutes after it went out (one a crash left behind: after its event); meanwhile reconciliation does not compare balances, which could take its trade for a transfer.

### Linux and macOS

The Windows scripts (setup, update, autostart) have no Linux counterparts; systemd does the supervisor's job. CI tests Windows and Linux; macOS is untested. A unit for the paper session, `/etc/systemd/system/tbot-paper.service` (replace `you` and the paths):

```ini
[Unit]
Description=tbot paper session
Wants=network-online.target
After=network-online.target

[Service]
User=you
WorkingDirectory=/home/you/Trading-Bot
Environment=TBOT_SUPERVISED=1
ExecStart=/home/you/.local/bin/uv run --frozen python -m tbot paper paper
Restart=on-failure
RestartSec=60
RestartSteps=4
RestartMaxDelaySec=900
RestartPreventExitStatus=3 4
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now tbot-paper` starts it now and at every boot; `journalctl -u tbot-paper -f` shows its warnings and errors (a supervised session prints no more), and `tbot log paper -f` everything. `Restart=on-failure` with `RestartPreventExitStatus=3 4` is the exit code policy above (`RestartSteps` and `RestartMaxDelaySec` need systemd 254 or later; older versions ignore them and wait a minute each time). For testnet the command is `tbot live testnet`, for real money `tbot live live --live`. `TBOT_SUPERVISED=1` makes a restart obey a `tbot stop`. `systemctl stop` lets the event in progress finish, like `tbot stop`. To update: `sudo systemctl stop tbot-paper`, `git fetch --tags`, `git merge --ff-only --autostash vX.Y.Z` (the newest release on GitHub; a backtest's lines in the trial log are kept), `uv sync --frozen`, `uv run tbot doctor --offline paper`, `sudo systemctl start tbot-paper`. Keep the machine from sleeping. On macOS, a launchd agent with the same command and `WorkingDirectory` can keep it running, but `KeepAlive` restarts it after every exit, 3 and 4 too, and only `ThrottleInterval` (set it to 60 or more) spaces the tries: watch its log.

### Moving to another PC

1. Stop the session on the old PC and remove its autostart (`tbot autostart <config> -Remove`). Never run the same ledger on two machines: both would trade the same book and cancel each other's stops.
2. On the new PC, clone and run `tbot setup`.
3. Copy `.env`, the ledgers with the files next to them (`data/*.sqlite*`: a ledger's `-wal` and `-shm` files hold its latest writes), or a fresh copy from `tbot backup --out <folder>` made after the session stopped, and `data/backups/` from the old PC; edited configs too. Market data downloads again by itself.
4. `tbot doctor <config>`, then start it or `tbot autostart <config>`. A live session settles anything that happened in between with the exchange at its first reconciliation. With an IP-restricted API key, add the new IP in Binance first.

### Uninstalling

To get out of the market first, run `tbot halt <config>` for each session that holds coins while it runs: it sells them and cancels their exchange stops (a stop left on Binance keeps the coins locked). For a session that is not running, `tbot halt` only marks it: start it (`tbot paper <config>`, or `tbot live <config>`, with `--live` for real money), and it sells at once and halts; then stop it. Then `tbot autostart <config> -Remove` for every scheduled session, and delete the repository folder. Setup also added the folder to your user PATH (Settings, System, About, Advanced system settings, Environment Variables) and may have unpacked Python to `%LOCALAPPDATA%\tbot`; remove both by hand. Delete the API key in Binance's API Management and the bot in @BotFather (`/deletebot`) if you no longer need them.

### Troubleshooting

| Symptom | What to do |
|---|---|
| `An Application Control policy has blocked this file` | Smart App Control: run `tbot setup` again; it rebuilds the environment on signed Python. |
| `tbot doctor` fails `binance: unreachable` | The network, a firewall, or a region Binance does not serve. |
| `api key: rejected: -2015` | Wrong key, a key for the other network (testnet keys only work on testnet), a missing spot trading permission, or an IP restriction that no longer matches your IP. |
| `api key: rejected: -1022` | The secret is wrong, or the key is Ed25519 or RSA: create a system-generated (HMAC) key. |
| `clock: this computer is 4.20 s behind Binance; turn on time sync` (a warning, beyond 3 s) | Turn on Windows time sync (Settings, Time & language, Sync now). The bot corrects for it either way. |
| `another tbot process is using ...` or `a session already runs on ...` (exit 3) | A session runs on this ledger already: `tbot status` shows it, `tbot stop` ends it. Two configs on one ledger: give each its own `ledger:`. |
| `keeps a testnet book, not a live one` (exit 4) | The config names another mode's ledger: give it its own `ledger:`. |
| `telegram_chat_moved` in the log, or doctor says the group became a supergroup | Telegram gave the group a new id: set `TBOT_TELEGRAM_CHAT_ID` to it (doctor names it). A running bot already sends to the new id. |
| No Telegram messages | `tbot notify` sends a test message and says what is wrong. While Telegram refuses alerts (a revoked token, a blocked bot, a removed chat), the heartbeat pauses and the ledger records it (`tbot status <config>`); a real-money session does not start then. |
| `the account has USDT ... free, and the bot's budget needs ...` (exit 4) | A new book or a raised `initial_cash` needs that much free USDT: check the amount in the config, then lower it or deposit. |
| `that is not the budget in the config: nothing started` | A real-money start by hand asks you to type `initial_cash` as a check against typos: type the amount the config says, or fix the config. |
| `Telegram does not know TBOT_TELEGRAM_TOKEN` or `the bot cannot send to TBOT_TELEGRAM_CHAT_ID` (exit 4) | Real money waits for working alerts: `tbot notify` (with `--chat` for a new chat), then start again. |
| The monitor says the bot is down, but it runs | Pings pause while a stream is stale, reconciliation fails, or Telegram refuses alerts: `tbot log <config> --level warning` says which. |
| `Telegram commands paused: another program reads this bot's messages` | A second clone or computer uses the same bot (Telegram lets one program read its messages), or a webhook is set: give each clone a bot of its own (`tbot notify` with a new token). |
| `tbot halt` says `halted, but not sold yet` (exit 1) | The sale failed (see the alerts); the session tries again within minutes. `tbot log <config> -f` follows it. |
| `tbot status` shows an error under a config | The file does not load: `tbot doctor <config>` names the key (and the line, for a YAML syntax error). A session already running on it runs on, but its next start fails (exit 4): fix the file. |
| `.env holds UTF-16 text` or `cannot read .env` (exit 4) | PowerShell's `>`, `>>`, or `Out-File` wrote part of it as UTF-16: open it in Notepad and save it as UTF-8. Or another account owns it (an elevated prompt of another administrator): give your account access to it. |
| `the bot may not write to the chat` (doctor), or no alert arrives | You blocked the bot, or it was removed from the group: unblock it in Telegram (or add it again), then `tbot notify`. |
| `rebalance.min_notional 10 counts in BTC, the quote asset` (exit 4) | The symbols are quoted in BTC (or another coin): give `initial_cash` and `rebalance.min_notional` in that asset, near Binance's minimum. |
| `NO EXCHANGE STOP: ...` in the stopped alert or the summary | A stop could not be placed (see the alerts before it); the next start, or the next reconciliation, places it. |
| `LOW DISK: ... GB free` | The drive the ledger or the market data is on is nearly full, and a full disk stops the bot: make room. |
| `... is damaged (...); tbot download ... sets it aside` from `tbot check`, or an alert `stream BTCUSDT 1h gets no bars: ... is damaged` | A market data file was cut short or garbled (a crash, a full disk, a bad copy). Run the `tbot download` it names (a session's start does the same): it renames the file `<year>.parquet.damaged` (kept; delete it when you like) and fetches that year again. A running session resumes the stream by itself. |
| `no market data socket for 15 min (cannot connect to stream.binance.com:9443 ...)` | A firewall or proxy blocks Binance's stream ports. The bot tries ports 9443 and 443 in turn and trades on with REST bars up to 35 s late; allow outbound connections to `stream.binance.com` on port 443 (or 9443). It says so again once it connects. |
| `a /halt ... came while no session read this chat` | The `/halt` arrived during a restart and was not carried out: send `/halt` again. |
| `only network checks failed` from `tbot autostart` | Binance, Telegram, or the heartbeat monitor could not be reached: try again when `tbot doctor` passes, or pass `-SkipDoctor`. |
| `was written by tbot X, newer than this tbot` (exit 3) | The ledger was kept by a newer version: `tbot update`, or restore a backup made before. |
| `.env, with the keys, can be read by ...` (doctor) | Limit `.env` to your account: see Security below. |
| `the stored bars start ..., long after the period's start` | Only recent bars are stored: run the `tbot download` it names, or set a later `start:`. |
| `tbot check` reports gaps of a week or more | Data is missing (a deleted file, an interrupted copy): delete the stream's folder under `data/binance/spot/klines/` and run `tbot download`. |

Run any command with `TBOT_DEBUG` set in the terminal (PowerShell: `$env:TBOT_DEBUG = 1`) for a full traceback, and `tbot log <config> --level warning` for the problems a session logged. To ask for help, attach the report `tbot support` writes (no keys or tokens in it; read it first: it shows balances and trades).

### Words used here

- **Bar**: one candle of a timeframe (4h: open, high, low, close, volume); a **bar event** is the moment one closes and the strategies decide.
- **Ledger**: the session's SQLite file (`data/paper.sqlite`); the **book** is what it says the bot holds (cash and coins), rebuilt from it at every start.
- **Reconciliation**: comparing the book with the exchange's balances every few minutes, and booking what changed from outside.
- **Kill switch** (guard): sells everything and halts after a set drawdown from the peak, until `tbot resume`; `tbot halt` and `/halt` trip it by hand.
- **In-sample / out-of-sample**: data the settings were chosen on, and data they were not; only the second says how a strategy may trade.
- **Supervisor**: `scripts\run_bot.ps1` under the scheduled task (systemd on Linux), which starts the bot again after a crash.

## Security

- `.env` holds the API keys and the Telegram token; it is never committed (the git hooks refuse it and scan every commit for keys). Setup and `tbot notify` make a new `.env` readable by your account only and keep that when they change it; `tbot doctor` warns when other accounts can read it. The ledgers and logs hold no secrets, and `tbot support` leaves any key-shaped text out of its report.
- The API key needs spot trading only: withdrawals off, so a stolen key cannot withdraw. It can still trade the whole account, and a thief can lose money that way (buying a thin coin from themselves), so keep the bot on a sub-account that holds only its budget, and restrict the key to your IP where you can. `tbot doctor live` fails a key that can withdraw and warns about other rights it does not need, and a live session refuses to start with a key that can withdraw.
- Keep the repository, and every `ledger:`, out of folders that OneDrive, Dropbox, Google Drive (also `My Drive` and `Shared drives`), or iCloud sync: they upload `.env`, and syncing a ledger while the bot writes it can damage it (`tbot doctor` warns).
- On a PC that other people use, restrict the repository folder to your account, so nobody else can read `.env` or change the code the bot runs. Once, in PowerShell in the folder: `icacls . /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"` (you, SYSTEM, and administrators; files created later inherit it).
- To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```sh
git config core.hooksPath .githooks     # once per clone (setup does it)
uv sync --group dev                     # the tools below; users need none of them
uv run python -m ruff check .
uv run python -m ruff format .
uv run python -m mypy
uv run python -m pytest
```

GitHub Actions runs the same checks, the git guard's audit, and the tests on Windows and Linux for every push to main, every release tag, and every pull request (on Windows also the PowerShell scripts, with the scheduled-task and elevation commands stubbed); a release tag also publishes a GitHub release with that version's CHANGELOG section, and every Monday against the newest versions of the dependencies (`.github/workflows/ci.yml`).

The hooks are shell scripts (Git for Windows runs them):

| Hook | Checks |
|---|---|
| `pre-commit` | Hangul and secrets in staged changes, ruff, mypy |
| `commit-msg` | Hangul and secrets in the message |
| `pre-push` | Hangul and secrets in every outgoing commit and tag, tests |

Run a full scan of tracked files and history with `uv run python scripts/git_guard.py audit`. Mark a known false-positive secret line with `guard: allow-secret`.

## License

No license has been chosen yet, so all rights are reserved by the author. Choose one before inviting others to use or change the code.
