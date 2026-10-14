# Config reference

Configs are YAML files in `config/`. A command takes a file or a name from that folder: `tbot doctor live` reads `config/live.yaml`. Every key may appear once per mapping (a key given twice is an error, not a silent override); YAML anchors and merge keys (`<<: *defaults`) work inside the keys below (a mapping of its own, such as `defaults:`, is an unknown key). An unknown key is an error with a suggestion, so a typo never passes silently. Defaults below apply when a key is left out. Fractions are written as fractions: `0.45` is 45%.

There are four kinds of configs:

| Kind | Recognized by | Used by | Shipped examples |
|---|---|---|---|
| backtest | `start` | `tbot backtest` | `donchian_trend.yaml`, `donchian_voltarget.yaml`, `multi.yaml` |
| validation | `backtest` and `grid` | `tbot validate` | `*_validation.yaml` |
| paper session | anything else (no `start`, `backtest`, or `mode`) | `tbot paper` and the session commands | `paper.yaml` |
| testnet or live session | `mode` | `tbot live` and the session commands | `testnet.yaml`, `live.yaml` |

## Trading settings (every kind but validation)

| Key | Default | Meaning |
|---|---|---|
| `initial_cash` | 10000; required in live mode | Quote currency (USDT) the bot trades with. In live mode with `ownership: budget` it is the most the bot can lose, and a real-money config must set it. Changing it later moves money into or out of the book at the next start, like a transfer (Telegram says so); a cut larger than the book is worth is refused, and so is a new budget, or a raise, that the account's free USDT does not cover. A first real-money start, or one after a raise, started by hand asks you to type the amount. With less than about 300 USDT, Binance's 10 USDT order minimum makes small rebalances impossible and results drift from the backtest. |
| `costs.fee_rate` | 0.001 | Fee per fill, as a fraction: 0.1% is Binance spot taker at VIP 0 without the BNB discount. Backtests and paper trading charge it; live trading books what Binance charged. |
| `costs.slippage_bps` | 5 | Price paid beyond the quoted price in backtests and paper trading, in basis points (5 = 0.05%). Below 10000. |
| `risk.max_symbol_weight` | 1.0 | Largest share of equity in one symbol (above 0, up to 1). |
| `risk.max_gross_exposure` | 1.0 | Largest share of equity invested in all symbols together; above 1 changes nothing on spot, which has no leverage. |
| `risk.long_only` | true | Must stay true: spot trading cannot sell short. |
| `risk.target_volatility` | 0 | Annualized volatility each symbol is scaled down to (0.4 = 40%); 0 turns scaling off. |
| `risk.volatility_lookback_days` | 30 | Days of returns the volatility is measured on; with `target_volatility` set, at least 10 bars of every strategy's timeframe (10 days for 1d). |
| `rebalance.min_notional` | 10 | Smallest order value in quote currency; Binance refuses smaller orders. |
| `rebalance.rebalance_threshold` | 0.02 | Weight changes smaller than this (2% of equity) are not traded. |
| `strategies` | required | A list of strategies; see below. Their `allocation`s add up to at most 1. |

Each strategy:

| Key | Meaning |
|---|---|
| `name` | A registered strategy: `donchian_trend` or `rsi_reversion`. |
| `symbols` | Binance spot symbols such as `BTC/USDT`, `BTC-USDT`, or `BTCUSDT` (all mean `BTCUSDT`); none twice. Testnet and live need one quote asset for all. |
| `timeframe` | Bar length the strategy trades on: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, or `1d`; the shipped configs use `4h`. |
| `allocation` | Share of equity for this strategy, more than 0 and at most 1. |
| `params` | The strategy's own settings. `donchian_trend`: `entry` (55, bars of the breakout high) and `exit` (20, bars of the exit low). `rsi_reversion`: `period` (14), `low` (30), `high` (70), `trend_period` (0 = off, else 2 or more; buy only above this moving average). |

## Backtest

| Key | Default | Meaning |
|---|---|---|
| `start` | required | First day, `YYYY-MM-DD`. Earlier bars are loaded as warmup. |
| `end` | latest stored bar | First day not included. |
| `guard` | none | The session guard below; with it the backtest stops trading where the kill switch would. |

## Validation

| Key | Default | Meaning |
|---|---|---|
| `backtest` | required | The backtest config to vary, relative to this file. It must have one strategy, no `guard`, and an `end` after `holdout_start`, or none. |
| `holdout_start` | required | Data from this day on is used once, by the final holdout run; after the backtest's start, and with stored bars after it (checked before anything runs). |
| `grid` | required | Params of the first strategy and the values to try, e.g. `entry: [20, 55, 90]`. |
| `objective` | sharpe | What the sweep maximizes: `sharpe`, `sortino`, `cagr`, `calmar`, or `total_return`. |
| `min_trades` | 30 | Runs with fewer trades cannot be selected. |
| `selection` | best | `best` grid point, or `neighborhood`: the point whose neighbors do best on average. |
| `walk_forward.train_months` / `test_months` | 36 / 12 | Window lengths; `step_months` must equal `test_months`. |
| `monte_carlo.runs` / `seed` / `block_days` | 2000 / 1 / 20 | Resampled paths (at least 100), their seed, and the block length in days. |
| `monte_carlo.kill_switch` | 0.45 | The `guard.max_drawdown` whose chance of tripping is reported. |
| `cost_multiplier` | 2.0 | Costs are multiplied by this in the stress test (at least 1). |
| `gate.min_oos_sharpe` / `max_drawdown` / `min_trades` | 0.8 / 0.25 / 100 | Thresholds the out-of-sample result must meet to pass. |

## Sessions (paper, testnet, live)

| Key | Default | Meaning |
|---|---|---|
| `ledger` | `data/paper.sqlite`, `data/testnet.sqlite`, `data/live.sqlite` | The session's book: fills, events, and state. Give every config its own (a copy without this line shares the default); `tbot status` and `tbot doctor` point out two configs that share one. A ledger keeps the mode it was started in. |
| `guard.daily_loss_limit` | 0.03 | While equity is this far below the day's open (UTC), no new entries; exits still trade. The bar that closes at midnight counts for the day it ends. 0 turns it off. |
| `guard.max_drawdown` | 0.45 | The kill switch: this far below the peak, sell everything and halt until `tbot resume`. 0 turns it off; below 1. Set it beyond the strategy's normal drawdowns (see the validation's Monte Carlo). Money moved in or out rescales the peak, so a transfer never moves the drawdown. `tbot halt` and Telegram's `/halt` trip it by hand. |
| `guard.stale_seconds` | 900 | A bar event later than this after its close buys nothing; its sales still go out. 0 turns the check off. |
| `stale_after_seconds` | 600 | A stream whose next bar is this late is reported stale (an alert; the heartbeat pauses). At least 60. |
| `batch_wait_seconds` | 5 | How long an event waits for the other streams closing at the same time. |
| `heartbeat_seconds` | 300 | How often `TBOT_HEARTBEAT_URL` is pinged (at least 30). |
| `summary_hour_utc` | 0 | Hour of the daily Telegram summary, UTC (0 to 23). |
| `backup_days` | 14 | Daily ledger copies kept in `data/backups/`; 0 turns them off. |
| `telegram_commands` | true | Answer `/status`, `/fills`, and `/halt` in the Telegram chat. Telegram lets one process read a bot's messages, so the sessions of a clone take turns: a real-money session first, else the one that started first. |

Testnet and live only:

| Key | Default | Meaning |
|---|---|---|
| `mode` | required | `testnet` (fake money on testnet.binance.vision) or `live` (real money; `tbot live` then needs `--live`). |
| `ownership` | budget | `budget`: the bot owns `initial_cash` and what it buys; the rest of the account is left alone. `account`: the bot owns the whole spot account; only for a dedicated account. |
| `protective_stop_pct` | 0.2 | An exchange-side stop-market order this far below the last close protects every position, also while the bot is down. 0 turns stops off. |
| `reconcile_seconds` | 300 | How often the book is compared with the exchange, stops are checked, and symbol rules are refreshed (hourly, and at once after Binance refuses an order for them). At least 30. |
| `reconcile_tolerance` | 0.002 | A relative difference between book and exchange smaller than this is noise. |
| `recv_window` | 5000 | Milliseconds a signed request stays valid (1000 to 60000). |

## Secrets and environment (`.env`)

| Variable | Meaning |
|---|---|
| `TBOT_BINANCE_API_KEY`, `TBOT_BINANCE_API_SECRET` | A system-generated (HMAC) key with spot trading on and withdrawals off. `tbot keys live` asks for them, checks them with Binance, and saves them. |
| `TBOT_BINANCE_TESTNET_API_KEY`, `TBOT_BINANCE_TESTNET_API_SECRET` | Testnet keys (testnet.binance.vision), so testnet keeps running once the pair above is a live key; without them testnet uses the pair above. `tbot keys testnet` saves them here. |
| `TBOT_TELEGRAM_TOKEN`, `TBOT_TELEGRAM_CHAT_ID` | Alerts; `tbot notify` fills them in (`--chat` picks the chat again). `tbot doctor` fails a live config without them (offline, only warns), and a real-money session does not start while Telegram refuses them. |
| `TBOT_HEARTBEAT_URL` | A monitor's ping URL (healthchecks.io, Uptime Kuma, ...). Keep it secret: anyone with it can report the bot alive. |
| `TBOT_DEBUG` | Set to 1 in the terminal (not in `.env`) to see a full traceback instead of a one-line error. |

`.env` must be UTF-8 (Notepad's default). Variables set in the environment win over `.env`.
