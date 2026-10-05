# Validation Report: donchian_trend with volatility targeting (0.4), corrected tools

Date: 2026-10-05. Config: `config/donchian_voltarget_validation.yaml` (unchanged since `donchian_voltarget_2026-09-29.md`). Data: Binance spot 4h bars, 2018-01-01 to 2026-10-05.

## Why a re-run

An independent review of the research code found that the validation made results look better than they were. The defects, all fixed:

- Monte Carlo compounded closed-trade returns only, so losses while a trade was open and streaks of bad days were invisible. It now resamples daily returns in 20-day blocks.
- The trial log merged runs that differed only in risk settings, so the deflated Sharpe counted 30 trials where 115 had been run on this sample.
- The holdout counter only counted runs tagged `holdout`, not the full-period backtests (among them the six used to choose the 0.4 target) that also covered 2025-2026.
- Paper and live run a risk guard that the backtest never simulated. The backtest now applies it when the config has one.

Small engine fixes from the same round changed the walk-forward's parameter choice for 2022.

## Verdict

Gate FAILED on two checks: out-of-sample Sharpe 0.79 (limit 0.8) and out-of-sample drawdown -43.5% (limit -25%). The 2026-09-29 result (Sharpe 1.08, drawdown -27.0%) depended on a near tie: for 2022 the train window ranked entry 20 / exit 40 at Sharpe 2.07 and entry 90 / exit 40 at 2.06, and the engine fixes flipped the pick from the second to the first, which lost 30% instead of 14%. A result that turns on the second decimal of a train Sharpe is not robust.

What still holds: the in-sample edge is significant after counting every trial (deflated Sharpe: P(true Sharpe > 0) = 99.6% over 115 trials; luck alone reaches 0.33 against 1.29), and it survives doubled costs. What changed is the expected pain: a typical stretch draws down about 28%, a bad one (5th percentile) about 44%.

## Consequences

- **Kill switch.** The shipped configs halted at 15% drawdown. Run through the backtest with the guard, those settings halt on 2018-11-09 and stay flat for eight years (+2.0% in total against +655.4% without the guard). The configs now use 45%, beyond the 5th-percentile Monte Carlo drawdown, so the switch catches a broken strategy rather than a normal drawdown; at 30% or 45% it never trips on the history. The 3% daily loss limit changed nothing on this history.
- **Plateau selection.** `donchian_voltarget_plateau_2026-10-05.md` compared its result with the 09-29 numbers, which came from the older engine. On the same engine both rules pick entry 20 / exit 40 for 2022; plateau selection reaches Sharpe 0.53 against 0.79, so it is still rejected.
- **Holdout.** Runs over the holdout period: 19. It is in-sample data now.
- **Going live** remains the owner's decision. Size `initial_cash` for a 45% loss.

## Report

```
Validation of donchian_trend on BTCUSDT, ETHUSDT 4h
In-sample 2018-01-01 -> 2025-01-01, holdout from 2025-01-01 to latest

1. Baseline (entry=55, exit=20), in-sample
                 CAGR   32.2%  Sharpe  1.29  MaxDD  -24.9%  Return  +605.6%  Trades  196

2. Cost stress x2
                 CAGR   28.6%  Sharpe  1.17  MaxDD  -26.7%  Return  +481.0%  Trades  196

3. Parameter sweep: 30 points, objective sharpe
   best entry=40, exit=15: 1.53, neighborhood mean 1.41
   baseline rank 27 of 30, neighborhood mean 1.38
   entry\exit      10      15      20      30      40
           20    1.26    1.43    1.31    1.30    1.39
           30    1.41    1.49    1.31    1.30    1.35
           40    1.47    1.53    1.37    1.29    1.34
           55    1.38    1.41    1.29    1.24    1.28
           70    1.42    1.51    1.43    1.36    1.38
           90    1.29    1.41    1.32    1.30    1.33

4. Walk-forward: train 36m, test 12m, pick best sharpe
   test 2021-01-01 -> 2022-01-01: entry=30, exit=10  train sharpe 2.01  test Sharpe  1.19  return  +24.3%  trades 54
   test 2022-01-01 -> 2023-01-01: entry=20, exit=40  train sharpe 2.07  test Sharpe -1.31  return  -30.1%  trades 37
   test 2023-01-01 -> 2024-01-01: entry=70, exit=15  train sharpe 1.65  test Sharpe  1.35  return  +31.0%  trades 26
   test 2024-01-01 -> 2025-01-01: entry=70, exit=15  train sharpe 1.02  test Sharpe  2.16  return  +62.5%  trades 27
   stitched OOS  CAGR   16.6%  Sharpe  0.79  MaxDD  -43.5%  Return   +85.0%  Trades  144

5. Monte Carlo: 2000 runs over 2558 days in 20-day blocks
   max drawdown p5 -44.1%  p50 -28.4%  p95 -19.5%; P(drawdown beyond 25%) = 70.2%
   final return p5 +119.6%  p50 +619.4%  p95 +2523.1%

6. Deflated Sharpe: 115 logged trials; luck alone would reach Sharpe 0.33; P(true Sharpe > 0) = 99.6%

7. Holdout (baseline params), seen 19 time(s) so far, counting every backtest over it
                 CAGR    4.0%  Sharpe  0.30  MaxDD  -22.0%  Return    +7.1%  Trades   48

8. Gate
   FAIL  out-of-sample Sharpe: 0.791 vs 0.8
   FAIL  out-of-sample max drawdown: -0.435 vs -0.25
   PASS  out-of-sample trades: 144 vs 100
   PASS  return at 2x costs: 4.81 vs 0
   PASS  holdout return: 0.0705 vs 0
   FAILED
```

## Kill switch on the shipped settings

`config/donchian_voltarget.yaml` plus `guard: {daily_loss_limit: 0.03, max_drawdown: X, stale_seconds: 0}`, 2018-01-01 to 2026-10-05:

| max_drawdown | Return | CAGR | Sharpe | Max drawdown | Trades | Kill switch |
|---|---|---|---|---|---|---|
| none | +655.4% | 26.0% | 1.12 | -24.9% | 244 | - |
| 0.15 | +2.0% | 0.2% | 0.07 | -15.3% | 20 | 2018-11-09, flat from then on |
| 0.30 | +655.4% | 26.0% | 1.12 | -24.9% | 244 | - |
| 0.45 | +655.4% | 26.0% | 1.12 | -24.9% | 244 | - |
