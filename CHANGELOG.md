# Changelog

## 0.5.1 (2026-10-10)

- Linux and macOS: an `--out` or `--html` value ending in a backslash names a folder, as on Windows; it named a file such as `new\/a.html`. The new CI found it on its first Linux run.
- CI: failed tests also show as annotations on the commit page.

## 0.5.0 (2026-10-10)

Product readiness: five reviews of 0.4.2 (new users, months of unattended running, security, packaging and portability, and the 0.4.2 changes themselves). Upgrade notes:
- `tbot update` works from 0.4.2 on.
- `tbot doctor` now fails a `mode: live` config without Telegram, so `tbot autostart` refuses it until `tbot notify` has set alerts up. Testnet and paper only warn, as before.
- `tbot notify` now picks the chat by a one-time code you send to the bot (in a group as a command, `/<code>@<bot name>`), and only at a terminal. A chat id already in `.env` stays.
- The heartbeat now pauses while a stream is stale or new orders wait for reconciliation, so your monitor reports the bot down then; a halted bot still pings.
- A supervised session (autostart) writes only warnings and errors to `logs\<config>.console.txt`; the JSON log keeps everything.

**Live trading**
- A connection lost while reading (balances, prices, order lookups, open orders) is retried instead of costing the bar's orders until the next 4h bar. Orders are still never sent twice.
- A rate limit or IP ban Binance asks to wait out is waited out without further requests, which made bans longer.
- Symbol rules (price and lot steps, minimums) are read again every hour and right after Binance refuses an order for them; before, a changed filter failed every order and stop until a restart by hand.
- A pair Binance pauses (`BREAK`) no longer stops the start, or every other pair, for good: its orders and stops wait, the rest trades, and Telegram says when it stops and starts trading.
- A key whose signature Binance rejects (a wrong secret, or an Ed25519 or RSA key; tbot signs with HMAC) ends the start with exit 4 and a hint, instead of a retry every 15 minutes forever.
- A sale or stop never asks for more than the free balance: a balance a hair under a lot step (99.99999999 of 100) was rounded up and refused at every bar since 0.4.2.
- A stop that covers only part of a position, because other orders lock the rest, is alerted once and named in the summary.
- A lowered `initial_cash` that the bot's cash covered owes nothing: since 0.4.2 every cut set a floor below zero for the book's cash, and a book that went negative for another reason was no longer set back to zero. What a cut still owes now shrinks as sales repay it.

**Alerts and monitoring**
- An alert Telegram cannot take (no network, Telegram down or busy) is sent again until it goes through, for up to a day; it was dropped, and the alert about an outage is the one most likely to meet one. A stream reported stale is reported back when its bars arrive.
- `tbot notify` takes only the chat that sends the code it shows: bot names are public, and the first chat that had written to the bot, possibly a stranger's, could be saved with Enter or without a terminal.
- `tbot doctor` no longer prints the heartbeat URL when a ping fails, names paused and unknown symbols, and says which key type a rejected key must be.
- The start alert and event, `tbot status`, `tbot doctor`, and the session's first console line name the version.

**Command line**
- `tbot` and `python -m tbot` run in the repository folder wherever they are typed, as `tbot.cmd` always did: a session started from another folder began a second book there.
- `tbot status` and `tbot doctor` point out two configs that name one ledger (a copied config without its own `ledger:`).
- A supervised restart that meets `tbot status` or another command reading the ledger at that moment waits for it instead of ending the supervisor without an alert (since 0.4.2).
- YAML merge keys (`<<: *defaults`) load again; 0.4.2 refused them.
- `tbot account` without keys says so before any request; the missing-keys message says where keys come from; `tbot check` with no data points at `tbot download`; a missing ledger names the command that starts it; `--data-dir`, `--workers`, and `stop --timeout` explain themselves; `TBOT_DEBUG=0` no longer turns on tracebacks; a config that does not load fits on its `tbot status` line.

**Windows scripts**
- `tbot setup -h`, `tbot update -h`, and `tbot autostart -h` show each description at its own parameter; they were shifted by one.
- `tbot autostart` replaces, and `-Remove` removes, a task whose repository folder was moved or renamed, instead of refusing it as another folder's task; a mistyped config name says how to remove a task only an administrator sees.

**Research**
- Parallel sweeps (`tbot validate`) start their workers fresh on every system: on Linux, forking a process that runs polars threads can deadlock.

**Project**
- GitHub Actions run the git guard's audit, ruff, mypy, the tests, and a package build on Windows and Linux for every push, and every Monday against the newest dependencies.
- The git guard catches heartbeat ping URLs and Telegram tokens of longer bot ids, and no longer reads icons and bitmaps as UTF-16 text (it reported Hangul in them since 0.4.2).
- New: [docs/CONFIG.md](docs/CONFIG.md) (every setting), [SECURITY.md](SECURITY.md), and README sections on Linux and macOS (a systemd unit), moving to another PC, uninstalling, troubleshooting, and security.

**Tests**
- 34 new tests (448 in all). Undoing any of 46 fixes makes a test fail; the sweep's start method shows only on Linux, in CI.

## 0.4.2 (2026-10-10)

Fixes from five reviews of 0.4.1 (live orders, the session loop and monitoring, the backtest, data and research, the command line and scripts). Upgrade notes:
- `tbot update` works from 0.4.1 on.
- A ledger now keeps its mode (paper, testnet, or live); one an older version started takes the mode of its first start. A config that names a ledger of another mode, such as a testnet config turned into a live one without a new `ledger:`, refuses to start (exit 4): give it a ledger of its own.
- Configs that no longer load, with a message saying why: a key given twice, `long_only: false`, a symbol listed twice in one strategy, `slippage_bps` of 10000 or more.
- Order and stop ids now carry a session tag; stops placed by 0.4.1 are replaced as usual at the first event.

**Live**
- Protective stops ask Binance for a full answer. For a stop, Binance's default answer leaves out side and status, so 0.4.1 took every stop it placed for a failure: it alerted "protective stop failed", showed NO EXCHANGE STOP, and cancelled and placed the stop again at every reconciliation. The test exchange now answers as Binance does.
- Two sessions on one account no longer cancel each other's stops, and their order ids differ.
- A lowered `initial_cash` that takes out more than the bot's cash stays lowered: reconciliation no longer books the shortfall back as a deposit; the bot buys nothing until sales repay it.
- Fills that add up to a float a hair below the exchange amount (0.7 + 0.1) no longer leave one lot step unsold and unprotected.
- Coins bought while a triggered stop still fills, and a position whose coins other orders lock, are flagged and alerted as without a stop instead of passing silently.
- A start that would fail the same way again (missing API keys, a symbol Binance does not list) ends with exit 4, so the supervisor stops retrying; `tbot live` checks the keys before it creates a ledger, and `tbot doctor --offline` reports missing keys.
- A paper or testnet book never trades real money, and simulated fills never enter a real-money book; `tbot doctor` fails on a ledger of another mode.

**Session loop and monitoring**
- One stream REST cannot serve (an outage for it, a delisted symbol) no longer stops the bars of every stream: the others are stored and traded, and it catches up later. The socket keeps reading when a catch-up fails, and one the server closes waits before reconnecting.
- The server-synced clock runs on the monotonic clock between syncs, so a Windows time step can no longer make an open bar look closed.
- The dashboard chains its total return and drawdowns over transfers: a budget raised tenfold no longer shows a 5% loss as 50%.
- The daily summary values coins moved in or out at their time, as `tbot status` does, and a wall clock stepped back no longer sends it twice. Prices below a cent keep their digits in alerts, `/fills`, status, compare, and the dashboard. The stale-stream alert names the close of the last bar, not its open.
- A `Retry-After` in the date form no longer breaks a request.

**Backtest and research**
- `selection: neighborhood` counts a neighbor beyond the grid's edge as the worst one tested, so a point on the edge no longer weighs its own result more than one inside. The plateau report's addendum has the effect: out-of-sample Sharpe 0.54 instead of 0.53, verdict unchanged.
- The stitched walk-forward curve no longer repeats a record at every segment boundary; the report's out-of-sample Monte Carlo figures move by at most a point.
- The Monte Carlo shortens its blocks for a series shorter than two of them instead of repeating the series in every path; the report gives each run's block length.
- Attribution runs every row up to the same end.
- Two runs at once no longer overwrite each other's trial-log lines.

**Data**
- A bad checksum or an unreadable archive fails that stream only; `tbot download` goes on with the others, as 0.3.0 meant.
- The quality check fails NaN, infinite, and negative values.

**Command line**
- `tbot backtest` checks strategy names and params, and `tbot validate` every grid point, before anything runs (exit 4, nothing logged); a period outside the stored bars is named with the stored range.
- `tbot log <typo>` says there is no such config instead of waiting for a log; `tbot stop` without a config says it looked in `config/`; a second `tbot paper` or `tbot live` on a ledger in use is refused before it says how to stop it.
- `tbot notify` asks for a new token when Telegram no longer knows the one in `.env`.
- `--out` and `--html` take a folder (one that exists, or a path ending in a slash); configs may end in `.yml`.

**Windows scripts**
- A `!` in a value passed to setup, update, or autostart stays, and a folder with `!` in its name works. `tbot setup -h`, `tbot update -h`, and `tbot autostart -h` show the scripts' help.
- `tbot autostart` withdraws an earlier `tbot stop` request before it starts the task, which would otherwise end at once; `tbot doctor` warns about a waiting request.
- `tbot autostart` refuses to overwrite a task of the same name that runs another folder's bot, and `-Remove` to remove one; a mistyped config name no longer asks for administrator rights.
- `tbot update -DryRun` says when another program runs from `.venv`, and the update checks that before it stops any task.
- A task whose config was renamed or deleted writes why to its journal and exits 4.

**Git guard**
- Files git shows as binary are scanned too: UTF-16 text (PowerShell 5.1 writes it with `>`) in full, other files for secrets in their strings.

**Documentation**
- The daily loss rule holds while equity is below the limit, not for the rest of the day; the holdout count covers logged runs (`tbot doctor`'s kill switch check and `tbot compare` are not logged).

**Tests**
- 34 new tests (408 in all); each fix was checked to fail its test when undone.

## 0.4.1 (2026-10-09)

Fixes from five reviews of 0.4.0 (command line, Windows scripts, live path, research, documentation). Upgrade notes:
- From 0.3.x: update as before with `powershell -ExecutionPolicy Bypass -File scripts\update.ps1` (there is no `tbot` command yet), then run `.\tbot setup -SkipDownload` once to put `tbot` on your PATH. Running bots may keep running: setup now leaves the Python environment alone while one does.
- From 0.4.0: run this one update with `powershell -ExecutionPolicy Bypass -File scripts\update.ps1` too. A `tbot update` would also work, but the old `tbot.cmd` reads the new one from the wrong place when the update replaces it, prints an error line, and runs the update a second time; from this version on it cannot.

**Command line**
- One config in `config/` that cannot be read (not UTF-8, a date like 2024-02-30) no longer breaks every command that looks through the folder; it is named in an error when used, and `tbot status` lists it as invalid.
- `tbot stop` without a config also finds a session whose config no longer loads (by the ledger the file names), and when no session runs but one has, it leaves the request for a supervisor about to restart it. Only a restart by the supervisor obeys such a request: a session you start by hand drops it, so an earlier `tbot stop` no longer ends it at once.
- `tbot resume` without a config never clears the kill switch of a real-money session; name it.
- The overview shows a row for a ledger it cannot read instead of failing as a whole, values a reconciled coin the config no longer trades from its stored bars, and leaves deposits and budget changes out of the 24-hour change. The daily Telegram summary leaves them out too.
- `tbot notify` shows the chat it found and asks before saving it, and passes on what Telegram says when another program reads the bot's messages. Every copy of a key in `.env` is updated, not only the first.
- A failed start says "often brief" only for errors that are (5xx, rate limits, Binance's firewall, the network); a rejected key or IP says what Binance answered and what to check.
- `tbot log -f` finishes the old file before following the new one after a rotation; `-n 0` shows only new lines. Ctrl+C ends any command without a traceback. Help texts say what each command takes when no config is given.
- `tbot backtest` and `tbot validate` say when a stream ends early (the run stops there) and when symbols trade on different bars, where a buy paid for by another symbol's sale fills later than in paper and live trading.

**Windows scripts**
- `tbot update` can replace `tbot.cmd` safely: the script runs and exits on one line, which cmd has read before the update changes the file.
- `tbot setup`, `tbot update`, and `tbot autostart` pass the rest of the line on exactly as typed (`=`, `,`, quotes, empty values), and the scripts refuse an unknown option such as `--dry-run` instead of ignoring it and running for real.
- Values with spaces, quotes, or a trailing backslash survive the switch to an administrator window. An elevated update waits for Enter on every failure, and a broken environment no longer stops it from reporting the version.
- An update whose `tbot stop` times out withdraws the request, so the bot does not stop later and stay down. `tbot autostart <config> -Remove` stops the session the task really runs, withdraws the request it leaves, and has `-DryRun`.
- Setup no longer stops at a PATH entry on a missing drive or in quotes.

**Live**
- An order found again by lookup whose executions are not listed yet, or cannot be read, is booked with the commission where Binance takes it by default (the coin bought, or the quote sold for), so the book holds what the account holds.

**Research and data**
- `selection: neighborhood` counts a neighbor with too few trades as no better than break-even or the worst qualifying point, so a lone peak among failing neighbors is no plateau (no logged run had too few trades, so no result changes).
- A trial-log line cut short by a crash is skipped with a warning, and the next append starts on a new line instead of joining it.
- Stored bar files are flushed to disk before they replace the old ones; the validation report says the Monte Carlo resamples bar returns; the default number of workers stays within what Windows allows (61).

**Documentation**
- The Monte Carlo inputs, the attribution numbers (Sharpe -0.36, correlation 0.19 on data to 2026-10-05), the heartbeat URL (`http://` is accepted, for a monitor on your own network), `tbot log` with `--log-file`, and the 0.4.0 notes are corrected; the 2026-09-24 and 2026-09-29 reports say the 2026-10-05 report supersedes them.

**Tests**
- 17 new tests (374 in all); each fix was checked to fail its test when undone.

## 0.4.0 (2026-10-09)

Easier to use every day. Nothing changes in how the bot trades. Upgrade notes: see 0.4.1, which corrects them.

**One short command**
- `tbot.cmd` in the repository runs every command from any folder, always in the repository, where `.env`, `config`, `data`, and `logs` live: `tbot status` instead of `uv run python -m tbot status config/paper.yaml`. Setup adds the folder to your user PATH (`-NoPath` skips it), keeping the other entries and their `%VARIABLES%` as they are.
- `tbot setup`, `tbot update`, and `tbot autostart <config>` run the PowerShell scripts, and the scripts take a config name (`paper`) as well as a path.
- A config can be named: `tbot status testnet`, `tbot backtest donchian_voltarget`. Session commands may leave it out and take the running session, else the only one that has run, else paper; they say which, and never guess between two. `tbot stop` stops the running session, `tbot resume` clears the halted one, and `tbot stop --cancel` withdraws every request.

**Seeing what the bot does**
- `tbot status` without a config shows every session on one line each: running or not, equity, profit (budget changes and transfers are money put in, not profit), 24-hour change, drawdown from the peak, positions, and a halt or orders in doubt.
- `tbot log` shows the JSON log as readable lines (an alert's text, tracebacks indented); `-f` follows it, also through rotations, and `--level warning` shows problems only.
- `tbot dashboard` and `tbot backtest --html` open the report in the browser when run at a terminal (`--no-open` to skip).
- A session prints its ledger, its log file, and how to stop it as it starts. A start that fails on the network says so in one line (host and status) and that a supervised bot tries again.

**Setting up**
- `tbot notify` sets Telegram up step by step: it asks for the token, checks it, waits for your first message to the bot, finds your chat, saves both in `.env` (every other line stays as it was), and sends a test message. `tbot doctor` points at it while Telegram is not set up.
- `tbot autostart paper` registers the scheduled task and starts it at once, unless a session started by hand already runs on the config (then it waits for the next boot); `-NoStart` waits too, and `-Remove` takes it away. Autostart, removal, and an update with scheduled tasks ask for administrator rights themselves from a normal terminal and run in a new window that stays open until you press Enter.
- `tbot update` ends with the old and new version.
- `tbot -h` ends with the first steps; commands are listed in the order they are used.

**Tests**
- 17 new tests (357 in all): naming and picking configs, the session overview, the log view and following it through a rotation, the guided Telegram setup, `.env` editing, opening a dashboard, and the message of a failed start. Each was checked to fail without the code it covers.

## 0.3.1 (2026-10-08)

Fixes from four independent reviews of 0.3.0 (live path, research, operations, and a mutation test of the suite). Upgrade notes: a paper session now logs to `logs\<config name>.jsonl`, like live (`paper.jsonl` for `config\paper.yaml`, as before); `.gitattributes` merges the trial log by union.

**Live trading safety**
- An order whose result is in doubt now holds its symbol back: the next event looks it up first and, if it executed or is still unknown, plans that symbol again at the next bar instead of ordering twice on a stale book. In budget mode that second order could sell the owner's coins or spend the owner's USDT.
- No protective stop is sized while a sell is in doubt (its free coins may be the owner's; 0.3.0 still did this until the next reconciliation), and a stop that triggered and partly filled is left to finish instead of being replaced above the market.
- An odd reply from Telegram (a proxy page, a malformed update) no longer ends the session; the command poller pauses and tries again.
- A close whose streams arrive in two events (a late 4h bar after the 1h one) is checkpointed only when every stream is in, so a restart in between still trades the late bar.

**Operations**
- `scripts\update.ps1` keeps local edits in every failure case (0.3.0 lost them when `uv sync` failed after the pull), restarts every task it stopped whatever happens, ends a supervisor waiting to restart, clears leftover stop requests, refuses to run with a leftover stash or a bot started by hand, checks configs with the new `tbot doctor --offline`, and exits with 1 when the update was not applied.
- `scripts\setup.ps1` refuses while a bot runs from `.venv`; `scripts\remove_task.ps1` ends a waiting supervisor and leaves everything as it was when the bot does not stop.
- `tbot stop --cancel` withdraws a stop request; `tbot backup --out` accepts a folder and never leaves a partial file; every session logs to its own file.

**Data and research**
- The Monte Carlo resamples every bar instead of daily closes, as the kill switch checks every bar. For the configured candidate out of sample: median drawdown -34%, 5th percentile -55%, 17% of four-year paths beyond 45% (0.3.0 said -33%, -54%, 16%).
- Delisted symbols are filled from their archives, partial months included; a 400 that is not "invalid symbol" stays an error; a first download of a symbol listed after `--start` stores its archives as they come.
- After a rate limit the live feed stays away from REST until the exchange allows it, and `tbot download` stops instead of hammering on.
- A leftover sold on its own belongs to the round trip that left it, so it is no longer a separate winning trade.

**Tests**
- 32 new tests (341 in all), among them a whole paper session from start to stop with the network replaced, the reconciliation pause and recovery, the guard shift through a reconciliation round, atomic ledger writes, the exchange-side order caps, volatility sizing without look-ahead, and kill-switch timing in the backtest. Each was checked to fail without the code it guards. Tests no longer depend on the folder pytest starts in.

## 0.3.0 (2026-10-08)

Product round after three more independent reviews (live path, research, operations). Upgrade notes: the supervisor's logs are per config now (`logs\<config>.supervisor.log`, `logs\<config>.console.txt`); a wrong command line or a `.env` that is not UTF-8 exits with 4 instead of 2 or 1; a changed `initial_cash` is booked as an adjustment at the next start.

**Use it as a product**
- `scripts\setup.ps1`: one command from clone to doctor, including a Python signed by the Python Software Foundation when Smart App Control is on.
- `scripts\update.ps1`: stops the scheduled sessions gracefully, pulls, syncs, runs doctor, starts them again; local edits are kept, or the update is undone when it collides with them. `scripts\remove_task.ps1` removes a task after a graceful stop.
- Ledger backups every 6 hours, 14 days kept (`backup_days`), `tbot backup` on demand, a tested restore; doctor warns when backups fall behind.
- Telegram: `/status` and `/fills` from the chat (`telegram_commands`, read only); `tbot notify` finds the chat id; the daily summary adds drawdown from the peak, fills, the last bar event, and any halt, pause, or position without a stop. Alerts are sent in the background, so a slow Telegram never delays an order.
- `tbot status` says whether a session is running; the dashboard shows when it was made and spaces points by time, so downtime shows.

**Live trading safety**
- An order in doubt, or a stop, that reconciliation books between bars now gets its protective stop at once instead of up to 4 hours later; an in-doubt sell no longer leaves a stop on the owner's coins.
- A restart no longer skips the bar that closed while the bot was down: bars within the guard's stale window are traded, and a crash in the middle of an event hands that bar over again.
- `tbot stop` also stops a bot the supervisor is about to restart, and no longer reports "stopped" for one that crashed.
- REST bars count as closed only 5 seconds after the close, and a slow clock resync keeps the previous offset, so a fast clock cannot store a bar that is still open.
- Orders in doubt that turn out never executed or unfilled are alerted.
- A changed `initial_cash` is a transfer everywhere: the dashboard's drawdown and `tbot compare` no longer read it as a loss.

**Data and research**
- The validation's Monte Carlo also resamples the out-of-sample returns and reports the chance of passing the kill switch. For the configured candidate: median drawdown -33% and 5th percentile -54% over four years, 16% of paths beyond 45% (report addendum).
- Syncs leave no hole when they fail part way (backfills write newest first; nothing after a month missing from the archives is stored before REST fills it); delisted symbols keep their archives; writers to one stream take turns; a long `Retry-After` fails at once instead of stalling the feed for hours; one failed stream no longer stops `tbot download`.
- A trade with no opening cost no longer breaks the backtest result.

**Operations**
- The supervisor keeps retrying with a clear log line when uv is missing, rotates its log, and keeps sessions' logs apart; the installer reads `mode: live` with quotes or a comment and asks for administrator rights up front.
- doctor no longer passes the kill switch on less than a year of history.

## 0.2.0 (2026-10-06)

Release hardening after three independent reviews. Upgrade note: exit codes changed (3 and 4 are new; the old 2 for a ledger in use is now 3), and the kill switch default is 45%.

**Live trading safety**
- A sell never exceeds the book's position, so a protective stop that fills just before an exit can no longer sell coins the owner holds; buys never spend negative book cash.
- Budget reconciliation sets a negative book position or cash back to zero and compares total quote; it reads balances between two settle passes and waits a round when something executed meanwhile.
- Fills, order rows, stop state, adjustments, and the guard shift are written atomically.
- A missing or failed protective stop is alerted once and retried at every reconciliation.
- Orders whose send status is unknown are looked up again before they count as failed; unexpected errors no longer crash an event or the reconciliation loop; a long Retry-After fails at once.
- A changed `initial_cash` and a `tbot resume` survive reconciliation; the feed never loses a bar when a catch-up fails.

**Validation**
- Monte Carlo resamples daily returns in blocks, so losses while a trade is open count.
- Trials include their setup and result; every backtest over the holdout counts as a look; the trial log is kept in git.
- Attribution pairs days by date; walk-forward windows no longer drift; streams stop at a common end.
- The backtest can run the session guard; the kill switch default moved from 15% (which halts the shipped strategy in 2018) to 45%.
- Re-validated: no configuration passes the gate.

**Operations**
- `tbot doctor`: strategies, ledger, alerts and heartbeat ping, clock, symbols, kill switch on history, API key permissions, budget.
- `tbot compare`: a session against a backtest of the same period.
- `tbot stop`: a graceful stop for a bot without a console.
- One-line errors and distinct exit codes (1 retry, 3 ledger unusable, 4 wrong config or command); `scripts/run_bot.ps1` restarts with backoff and logs to `logs/supervisor.log`; `scripts/install_task.ps1` checks the config and runs doctor before registering a startup task.
- Downloads backfill earlier history without leaving holes and use Binance's clock.

## 0.1.0

Data download and storage, event-driven backtester, Donchian and RSI strategies, volatility targeting, validation pipeline, paper trading, Binance live trading with reconciliation, risk guard, protective stops, Telegram alerts, heartbeat, and the HTML dashboard.
