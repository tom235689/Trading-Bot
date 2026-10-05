# Validation Report: plateau selection in walk-forward

Date: 2026-10-05. Config: `config/donchian_voltarget_plateau_validation.yaml` (base `config/donchian_voltarget.yaml`). Data: Binance spot 4h bars, 2018-01-01 to 2026-10-05. Compare with `donchian_voltarget_2026-09-29.md`, which proposed this rule before its effect was measured.

## Verdict

Rejected. On the same engine and data, picking each window's parameters by the neighborhood mean reaches an out-of-sample Sharpe of 0.53, against 0.79 for the single best point (`donchian_voltarget_2026-10-05.md`); both reach a drawdown of -43.5%. A first version of this report compared against the 2026-09-29 numbers (Sharpe 1.08, drawdown -27.0%), which came from an older engine, and blamed the plateau rule for the 2022 loss. That was wrong: on the current engine both rules pick entry 20, exit 40 for 2022, and that window loses 30% either way.

The 2022 bear market hurts every parameter set that worked in the preceding bull run; the selection rule only decides which one. The drawdown is a regime problem, so no selection rule on this grid should be expected to fix it.

## Decision

- Keep `selection: best` (the default) for this strategy. The `selection` option stays in the code for future strategies; this run is logged in `data/trials.jsonl`.
- Do not try further selection rules or grids against the same data to get under the drawdown limit. Each attempt is another trial, and the holdout has been seen too often to count as unseen data.
- The Monte Carlo and holdout lines below come from the tools before their correction; see `donchian_voltarget_2026-10-05.md` for the corrected figures.

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

4. Walk-forward: train 36m, test 12m, pick neighborhood sharpe
   test 2021-01-01 -> 2022-01-01: entry=40, exit=10  train sharpe 1.98  test Sharpe  1.39  return  +28.9%  trades 47
   test 2022-01-01 -> 2023-01-01: entry=20, exit=40  train sharpe 2.07  test Sharpe -1.31  return  -30.1%  trades 37
   test 2023-01-01 -> 2024-01-01: entry=90, exit=10  train sharpe 1.51  test Sharpe  1.03  return  +20.9%  trades 28
   test 2024-01-01 -> 2025-01-01: entry=55, exit=10  train sharpe 0.81  test Sharpe  1.44  return  +31.9%  trades 40
   stitched OOS  CAGR    9.5%  Sharpe  0.53  MaxDD  -43.5%  Return   +43.8%  Trades  152

5. Monte Carlo: 2000 runs over 196 trades
   max drawdown p5 -29.5%  p50 -19.7%  p95 -14.0%; P(drawdown beyond 25%) = 16.0%

6. Deflated Sharpe: 30 logged trials; luck alone would reach Sharpe 0.16; P(true Sharpe > 0) = 99.9%

7. Holdout (baseline params), evaluated 5 time(s) so far
                 CAGR    4.0%  Sharpe  0.30  MaxDD  -22.0%  Return    +7.1%  Trades   48

8. Gate
   FAIL  out-of-sample Sharpe: 0.529 vs 0.8
   FAIL  out-of-sample max drawdown: -0.435 vs -0.25
   PASS  out-of-sample trades: 152 vs 100
   PASS  return at 2x costs: 4.81 vs 0
   PASS  holdout return: 0.0705 vs 0
   FAILED
```
