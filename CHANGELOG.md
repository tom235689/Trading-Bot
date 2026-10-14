# Changelog

## 0.9.2 (2026-10-14)

The release of 0.9.1, whose Linux check failed on a test, not on the bot: the test that `tbot check` finds a 1m bar still open read the real clock and failed in the last ten seconds of a minute; it now sets the time. Nothing else changed.

## 0.9.1 (2026-10-14)

Fixes from a review of the whole code base in seven parts: the session core, orders and the exchange, the command line, market data, research, operations, and the documents against the code. Upgrade notes:
- `tbot halt` exits 1 when the sale did not go through, and names what is not sold yet; the session tries again within minutes.
- `risk.target_volatility` above 3 is refused: it is a fraction (0.4 means 40%), and a value such as 40 turned the scaling off without a word.
- A testnet or live config whose symbols are quoted in two assets (BTCUSDT with ETHBTC) is refused at start (exit 4) and by doctor; the book keeps one cash.
- `tbot doctor --offline` fails a real-money config without Telegram, as `tbot live` does, so `tbot update` no longer says it started a task that ends at once.
- Paper and live sessions replay one bar less at start, so their first event sees what a backtest starting there sees.

**Getting out**
- A halt whose sale fails (a rate limit, an exchange outage) is tried again a minute later, then less often, up to every 15 minutes, until it goes through; before, nothing tried again until the next close, a day away on 1d bars.
- Telegram's `/halt` also halts the sessions a supervisor starts again (between its restarts, or at the next boot) and those whose config broke without a `ledger:` line; the answer names those that halt at their next start.
- `tbot resume` withdraws a halt request no session took, also when the book is not halted; `tbot resume` without a name finds such a request, and `tbot status <config>` shows it. Before, the next start, maybe days later, sold everything.
- `tbot halt` without a name takes the running session (one whose config no longer loads included) before a stopped book, and `tbot stop` and `tbot halt` reach a session whose config broke and names no `ledger:`.
- Dust below the exchange minimum is no longer sent as an order at every halted bar, nor named as a holding by `tbot halt`, `tbot stop`, and the alerts.

**Real money**
- A changed `initial_cash` is checked against the book valued at the latest stored closes, not at the last bar before the bot stopped: a cut after a fall could leave a live book with negative equity and a kill switch that never trips, and halted a paper book at once.
- A protective stop refitted after the price fell through its new level (at a restart, or a reconciliation between bars) cancelled the working stop and was then refused, leaving the position without one until the next close: the working stop now stays, and a missing one goes under the current price.
- `protective_stop_pct: 0` cancels the stop an earlier run placed, which could still sell the whole position.
- After a sale cancelled the stop and then failed, the alert said the stop stays: it now says the position has none. A failed refit for a pair the book does not hold is logged, not alerted.
- Market orders are capped at the largest order Binance takes (`MARKET_LOT_SIZE`, `LOT_SIZE` maximum); the rest goes at the next decision instead of failing at every bar.
- Dust of a pair the config no longer trades no longer blocks every start; more than dust is refused for good (exit 4) instead of being retried every 15 minutes.
- Doctor's budget check counts the cash of the other budget books on the account, as the start does.

**Messages and commands**
- The daily summary and `tbot status` no longer call a change against a snapshot days old a 24-hour change.
- `tbot keys` warns when other accounts can read `.env` instead of saying it is yours only, keeps an old key it could not try on the testnet (no network) as the testnet pair, and it and `tbot notify` say when an environment variable overrides what they saved.
- The hint for a key Binance rejects names the pair the session uses (the testnet pair on testnet).
- `tbot paper` given a live config names the whole command, `--live` included. Output piped from a console no longer fails on a name its code page cannot show, and a question whose input was closed says so in one line.
- A supervised start that gives up (exit 3 or 4) tries its last alert for about two minutes, since at a boot the network may come up after the task.
- A session says once when another program reads the bot's messages (a second clone or computer with the same bot, a webhook), which keeps /halt from reaching it.
- `tbot check` finds a 1m bar that is still open.

**Research**
- `objective: calmar` ended the validation with an error after the whole run, its holdout look included, had been made.
- `selection: neighborhood` compared neighbors in the grid as typed: numeric grid values are put in order.
- `tbot compare` with a symbol on two timeframes compared fills past where the backtest stopped and flagged right ones as only in the session.

**Operations**
- `tbot update` run as another administrator (a standard user's prompt) refuses before it stops anything when that account cannot read `.env`, and names the repository safe for git; before, it stopped every session and started none.
- The scripts work in a folder whose name has brackets, and setup no longer rebuilds `.venv` at every run when the Python path has non-ASCII letters.
- The git hooks find Hangul in UTF-8 text with a NUL byte, in UTF-16 without a byte order mark, and three or more Hangul syllables in other binary files.
- A weekly CI run and a push no longer cancel each other.
- A live feed waits at most 5 seconds for another process's write to the store, and a busy store is no longer reported as a silent socket.

**Documents**
- README: uninstalling while a session is stopped, the clock warning, which exits are alerted, where backups go, group chats and one bot per clone, and new troubleshooting rows; DESIGN and CONFIG follow the code.

**Tests**
- 36 new tests (641 in all); undoing any of 52 changes makes a test fail.

## 0.9.0 (2026-10-14)

Safer and easier trading, from three reviews: what real money could suffer, a new user's way from paper to live, and the changes of this release themselves. Upgrade notes:
- A real-money config must set `initial_cash`; the default of 10,000 no longer applies in `mode: live`.
- A real-money session does not start while Telegram refuses its token or chat. In budget mode a new book, or a raised `initial_cash`, starts only when the account's free USDT, less the cash of the other books in `config/` on that account, covers it (exit 4, alerted). A first real-money start, or one after a raise, started by hand at a terminal asks you to type the budget.
- `telegram_commands` is on by default and in every shipped config: the sessions of a clone take turns reading the bot's messages, a real-money session first, so the option no longer has to be turned off elsewhere.
- A stale bar or failing reconciliation now holds back buys only: sales still go out.
- Alerts of a config whose name is not its mode carry both, such as `[live btc_only]`.

**The panic button**
- `tbot halt <config>` sells what the bot holds at once and halts it until `tbot resume`: a running session books stops that executed, cancels its exchange stops, and sells within seconds; a stopped one is marked, and its next start sells before it trades on. A real-money session must be named; a running session is reached even when its config no longer loads.
- Telegram `/halt` does the same from the phone for every running session of the clone, after a second message with the four-digit code it answers (two minutes, one try), so a tap on the command never sells.
- `tbot resume` withdraws a halt request no session took, and doctor names one that waits.
- `tbot stop` and the stop alert name the positions left, and for a scheduled session that its task starts it again at the next boot; the protective stop alert says the strategy may buy back at the next close.

**Real money**
- The budget is checked against the account before it is booked, typed by the owner on a first start or a raise (`ownership: account` asks for the word `account`), and named in every real-money start alert and in the start banner, with the way out.
- `tbot resume` first says where the book stands and what trips the kill switch again, asks you to type `resume` for real money, and a running session announces the resume.
- Telegram refusing alerts (a revoked token, a blocked bot) is recorded in the ledger and pauses the heartbeat, so the monitor reports the silence.
- The start and `tbot doctor` warn about key rights tbot never uses (margin, futures, options, transfers), and the start alerts when it cannot read them; doctor names what `ownership: account` takes over, and warns about a repository in a synced folder (OneDrive, Dropbox, Google Drive, iCloud).
- No message suggests selling by hand on Binance: the book would sell its amount again, from the owner's own coins.

**Convenience**
- `tbot keys testnet|live` asks for the API keys (the secret hidden), checks them with Binance, and saves them in `.env` only when they work; a testnet key the main pair held moves to the testnet pair.
- `tbot notify --chat` picks the chat again, and pairs while a session runs: the session lets go of the bot's messages for it. Both say which running sessions need a restart.
- The daily summary and `/status` show the profit since the start and say which bar's prices value the book.
- `tbot paper` is suggested without the default name, and reconciliation alerts carry the session's label.

**Documents**
- README: getting out, a protective stop that follows the last close, why a sale by hand is bought back, a sub-account for the bot, keys through `tbot keys`, and new troubleshooting rows; CONFIG and DESIGN follow.

**Tests**
- 42 new tests (605 in all); undoing any of 52 changes makes a test fail.

## 0.8.0 (2026-10-13)

Product readiness, from six reviews of 0.7.0: its own changes, months of unattended running, security, a new user following the README on a fresh clone, a randomized simulation of live trading with injected faults (12,800 bars, 28,877 reconciliations, 915 crashes), and what a shippable product lacks. Upgrade notes:
- A live session (`mode: live`) refuses to start without Telegram, or with an API key that can withdraw or cannot trade spot; `tbot doctor` already failed these.
- Ledgers now record their schema version and the tbot that wrote them; an older tbot refuses a ledger a newer one wrote (exit 3) instead of misreading it.
- `uv sync` installs the bot only: ruff, mypy, and pytest come with `uv sync --group dev` (the git hooks and CI ask for them). uv 0.12 or later is required.
- `tbot doctor` warns about the clock from 3 seconds (was 1), and calls a running session OK.
- Testnet keys may go in `TBOT_BINANCE_TESTNET_API_KEY` and `_SECRET`, so a testnet session keeps running once the main pair is a live key.

**Live trading**
- An order whose answer was lost is no longer taken for a transfer: while one is in doubt, reconciliation does not compare balances (0.7.0's two minutes of patience let it book the trade a second time).
- After a crash during a sell, the restart puts the stop back at once: the patience runs from the order's event, not from the restart.
- A pair Binance stops listing with a stop or an order in doubt on it no longer fails every reconciliation and pauses all trading.
- Each event first books stops that executed since the last look, so the kill switch never values coins a stop already sold.
- A changed `initial_cash` rescales the guard at the first reconciliation, once stops that executed while the bot was down are booked.
- A stop refused for a filter Binance changed is placed again on reloaded rules at once; a finer step found by a reload gets the remainder a stop; a book less than a step above the account gets no false "locked" alert.
- The saved strategy targets keep moving while a pair is paused or a stream is stale.

**Alerts and monitoring**
- A ledger that cannot be written (a full disk) no longer silences the crash alert, and a session that dies outside its own alerts is alerted by the command.
- A websocket silent across a bar close is replaced; quiet pairs that send nothing between closes are left alone (0.7.0 reconnected them every two minutes).
- A log backup held open by another program no longer loses a whole log generation at the next rotation.
- Doctor and the daily summary say when a newer release is out (`git ls-remote` on the clone's remote, once a day).
- The clock counts a Linux suspend; the clock warning is logged once, not every ten minutes.

**Security**
- `tbot notify` pairs only with the exact code (8 hex digits) sent after it was shown: a stranger can no longer flood the bot with every code to take the alerts.
- `.env` is created readable by your account only (setup, `tbot notify`), keeps its permissions when changed, and doctor warns when others can read it.
- Spaces around a key in `.env` are dropped, so an HTTP error can no longer carry the key into the log.
- The git guard opens zip files (also docx, xlsx), reads UTF-32, and knows the ping URLs of other monitors.

**Command line**
- `tbot support` writes a report for a help request: versions, configs, offline doctor checks, the overview, recent warnings, without keys or tokens.
- A backtest whose stored bars start long after its period's start is refused instead of passing a few months off as years.
- `tbot check` names short gaps as normal exchange pauses and lists only gaps of a day or more; default streams never downloaded are not failures.
- `tbot compare` needs 10 matched fills before it says a session tracks its backtest, and after one bar event says when the first comparison comes.
- A -2015 key error says what to check; a never-started live config no longer suggests starting it; times of the next decision show local time too; status names the last closed bar.
- A backtest prints a reminder that past results are no forecast, and a failed validation says plainly not to trade it with money you need.
- A config of another kind that does not load as one says both; `--timeframes 15M` is 15 minutes (only `1M` is refused); `--html` with a wrong name fails before the run.
- `tbot help`, the doctor timeframe warning for sessions under 15m bars, and progress lines for long runs.

**Windows scripts**
- `tbot autostart` calls a Telegram or API outage a network failure too.
- `tbot update` says how to go on when a release changes lines you edited.
- Setup goes on to doctor when the download fails, and makes `.env` private.

**Project**
- CI tests the PowerShell scripts on Windows (every script parses; arguments survive the elevated hand-over; dry runs) and publishes a GitHub release with the CHANGELOG section for every version tag.
- README: a glossary, proxy and VPN notes, testnet keys, `git pull --autostash` for Linux, macOS marked untested, and the systemd journal described as it is.

**Tests**
- 48 new tests (563 in all); undoing any of 42 fixes makes a test fail.

## 0.7.0 (2026-10-12)

A line-by-line review of the whole code base in six parts: the order path, the session runner and its state, the command line, backtest and research, data and monitoring, and the scripts and documents. Upgrade notes:
- Protective stops are now stop-market orders (`STOP_LOSS`). The stop-limit orders of earlier versions had a limit 0.5% under the trigger, and Binance's `PERCENT_PRICE_BY_SIDE` filter refuses a sell priced under 0.8 times the 5-minute average on BTCUSDT, ETHUSDT, and most other pairs: at the default 20% nearly every stop was refused (each one alerted), so testnet and live positions mostly had no exchange stop. The first event after the update replaces them.
- `tbot autostart` from a normal terminal failed since 0.6.0 (the elevated window got `-SkipDoctor "True"`); it works again.
- A lower `initial_cash` that takes more than the book is worth is refused at start, with the lowest value it can be.
- `daily_loss_limit` now also sees the bar that closes at midnight UTC: it counts for the day it ends. On 1d bars the rule could never trip before. On the 2018-2026 backtest of the shipped config it changes no trade and the final equity by less than 0.01%.
- A config with `target_volatility` whose `volatility_lookback_days` gives fewer than 10 bars of a strategy's timeframe is refused: such a window was never measured, and the scaling silently did nothing.
- `--data-dir` is relative to the folder the command was typed in, like every other path.
- A config outside `config\` logs to `logs/<name>-<hash>.jsonl`, so two configs of the same name never share a log.

**Live trading**
- Stop-market protective stops (above).
- When Binance stops listing one configured pair, the others are still checked: Binance refuses a whole symbol-rules request for one unknown symbol, which froze every refresh of filters and statuses. The pair gets the status `UNLISTED` and waits like a paused one; a start names it.
- An order whose answer was lost counts as never sent only once Binance still does not know it two minutes after it went out; 0.6.0 could decide that seconds later, and an order that executed late then had no stop and was booked as the owner's.
- A buy booked by reconciliation gets its stop even when the rest of that reconciliation failed.
- A stop that was cancelled, then could not be replaced because the lookup failed, is alerted as gone, not as still in place.
- Two `initial_cash` changes before the next bar, or a change after a withdrawal, no longer trip the kill switch: the guard is rescaled from the book's value after the last transfer, not from the last bar's.
- A book emptied by a transfer no longer crashes the session with a division by zero at every bar.
- A `tbot stop` request still open in another process no longer crashes the session on Windows.

**Alerts and monitoring**
- A websocket that stays open but goes silent for two minutes is replaced; bars no longer arrive late for up to a day without an alert.
- Alerts follow a Telegram group that became a supergroup (new chat id), and the log says to update `TBOT_TELEGRAM_CHAT_ID`; doctor names the new id.
- `tbot doctor` asks Telegram whether it knows the token and the chat (nothing is sent), so a revoked token or a wrong chat id no longer passes.
- The JSON log keeps every record when Windows refuses its rotation (another process has it open), and rotates a minute later; before, every record was lost from then on.

**Command line**
- A config of another kind is named as one only when it is valid as that kind: a backtest config without `start:` now says `start: Field required`, not "a paper config". A live config is pointed at `tbot live <config> --live`.
- `--timeframes 1M` is refused (Binance's month) instead of read as one minute; `4H` and `1D` still work.
- A folder given as a config, a path that cannot be checked, and a YAML error in a validation's base file get a one-line message instead of a traceback or a misleading one.
- `tbot backup --out` never writes over an existing file, and `dashboard --out` and `backtest --html` write only `.html` files.
- "Run `tbot download`" advice names the symbols, timeframes, and data folder that are missing.
- `tbot help setup|update|autostart` says how to see the script's help; `tbot log` on a backtest config says it keeps no log; a ledger of another mode suggests a free file name; doctor's clock line says whether this computer is ahead or behind.
- A backtest of streams with different timeframes no longer notes that the coarser one "ends" early.

**Backtest, research, and data**
- `tbot validate` checks before anything runs that a walk-forward window fits before `holdout_start` and that `cost_multiplier` leaves a tradable fee, instead of failing after the sweep.
- RSI of a flat series is 50, not 100; `trend_period: 1` is refused; an infinite `initial_cash` is refused.
- `tbot check` counts exactly a week of missing bars as a long gap, and allows for a local clock a little behind Binance's.

**Windows scripts**
- `tbot autostart` works from a normal terminal again (above). It says "only network checks failed" only when the failures are network ones, not for a missing Telegram, a bad heartbeat URL, or a halted kill switch.
- With an administrator's password typed into the prompt (a standard account), autostart, update, and remove use the uv found before elevating, and the task runs as the owner, not the administrator.
- `tbot update -DryRun` says when it cannot tell what would be pulled (no upstream, a failed fetch) instead of "nothing to pull".

**Project**
- The commit-message hook skips only git's own closing comment block, so a `#` line in a message is checked too.
- README, DESIGN, CONFIG, and SECURITY updated for all of the above.

**Tests**
- 31 new tests (515 in all); undoing any of 40 fixes makes a test fail.

## 0.6.0 (2026-10-11)

A second round of five reviews: the 0.5 changes themselves, updates and autostart, a new user's first day and the documents, simulated live trading over nine scenarios (outages, paused pairs, stops that fill while the bot is down, two sessions on one account, budget changes), and the data and research tools. Upgrade notes:
- `tbot update` now restarts every scheduled session that could start: `tbot doctor --offline` fails only on what stops a start (a config the new version rejects, a ledger of another mode, missing API keys) and shows the rest as warnings. Since 0.5.0 an update left a live bot without Telegram, or a halted one, stopped. This update itself still runs the old script, which reads the new doctor, so it already restarts them.
- A "nothing has run with this config yet" answer from `status`, `compare`, `dashboard`, `backup`, and `resume` now exits with 1, like `tbot log`, not 4: the config is fine.
- Symbols may be written `BTC-USDT` or `BTC_USDT`; anything that is no symbol (`BTC.USDT`) is an error when the config loads.
- Every config in `config\` that is no backtest or validation config is a session config, also one without a `ledger:` line (it uses the default ledger, and `tbot status` says when that is shared).

**Live trading**
- A budget cut no longer pays an order in doubt with the owner's cash: 0.5.0 worked out what a lowered `initial_cash` leaves owing before orders in doubt were settled, so a buy that executed during a crash could be booked against the owner's USDT. The whole cut now counts until the first reconciliation, then only what the book's cash is short of. Ledgers from 0.4.2 keep the sum of their cuts, as then.
- A deposit, withdrawal, or budget change during a drawdown no longer trips the kill switch: the peak is rescaled with the equity, so a 33% drawdown stays 33% after the owner takes money out (it became 45.5% and flattened everything).
- An order Binance refuses because a lot step or tick changed is sent again on reloaded rules in the same event, instead of waiting for the next bar with no stop on the position.
- When Binance trades a paused pair again, its stop is refitted to what was booked during the pause.
- Lookups of an order in doubt are single tries each (they are the retries), so a network blip no longer holds the event for minutes.
- A stop that executed while the bot was down is booked at the time it executed, not at the restart: daily summaries put the loss on the right day.
- A stop that could not be replaced because Binance was unreachable says the one in place stays, instead of "the position has no exchange stop".

**Alerts and monitoring**
- `tbot notify` reports a message Telegram refuses (a wrong chat id) as failed; 0.5.0 said it was sent.
- A stream reported back, or stale, while the ledger is busy no longer loses the bars that arrived with it.
- The heartbeat keeps pinging while only a pair Binance has paused is stale: the rest trades.
- Session consoles print lines as `tbot log` does; other commands print only warnings and errors, and `tbot download` no longer reports the clock offset it corrects.

**Command line**
- Config errors name the fix: `initial_cahs: unknown key (did you mean initial_cash?)`, `guard.max_drawdown: ... (a fraction: 0.45 means 45%)`, `strategies.0.params.exti: unknown key (did you mean exit?)`, and a config given to the wrong command names the right one (`config\paper.yaml is a paper config: run it with tbot paper paper`) instead of listing every pydantic error.
- A session says when its first decision comes (the next 4h close), and so do `tbot status` and `tbot compare` while there is none yet.
- `tbot help` and `tbot help <command>`; `--timeframes 4H` works and a wrong value lists the right ones; a wrong date says the format.
- `python -m tbot` typed in another folder takes files you name from that folder again (0.5.0 looked for them in the repository); `tbot.cmd` is unchanged.
- `--out mydash` writes `mydash.html`; `--html` alone writes to `reports\`.
- A broken `.env` stops `tbot paper` before it announces a start; `tbot status` shows readable times; small wording fixes.

**Backtest, research, and data**
- A stream that ends before the period's `end` (a symbol Binance delisted) is an error before the run, instead of silently cutting every stream short; without an `end`, the note says what to do. A symbol whose data starts after the period starts, and a week or more of missing bars inside it, are named before the run.
- `tbot check` fails on gaps of a week or more (missing data, not a market pause) and says how to download the stream again.
- `tbot validate` checks before anything runs that the holdout has stored bars.
- A backtest of several strategies is logged as a portfolio, not as a trial of the first strategy, so it no longer inflates the deflated Sharpe's trial count.
- `tbot compare` flags fills that cost more than 1 bps beyond the backtest (both already include the modeled slippage; it needed nearly double before), and names a `tbot resume` and a config change in the period.
- A first download of a recently listed symbol no longer asks REST for every month before its listing.
- A walk-forward train window without a qualifying run is named, with what to change; the gate prints returns and drawdowns in percent; a backtest shorter than 30 days shows no annualized ratios.
- The dashboard of a ledger without bar events shows the book at today's marks, labels the exposure tile "Exposure now", and shows a flat drawdown as 0.0%.

**Windows scripts**
- `tbot autostart` checks the config and runs doctor before it asks for administrator rights, says when only network checks failed, registers the task at normal priority (Task Scheduler's default is below normal), and checks ten seconds after the start that the session still runs.
- The supervisor ends (exit 4) after three tries without uv instead of retrying forever, and keeps the console of the run before as `.1`.
- `tbot update` tells a config that no longer loads apart from a bot that did not stop, points at `tbot autostart <config>` to start a session again, and says "nothing to pull" on a dry run.
- `tbot setup` prints its next steps also when its last doctor run fails.

**Project**
- The git guard matches monitor ping URLs by the shape of real keys, so examples in docs no longer block a commit.
- `tbot doctor` names the symbol Binance does not list, asking one at a time (Binance's answer names none).
- README, docs/CONFIG.md, DESIGN, SECURITY.md, and .env.example corrected where they disagreed with the code; DESIGN lists two known limits (BNB fees for a bot that trades BNB, a remainder below a coarser lot step).

**Tests**
- 36 new tests (484 in all); undoing any of 47 fixes makes a test fail.

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
