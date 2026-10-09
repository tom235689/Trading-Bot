# Validation Report: donchian_trend with volatility targeting (0.4)

Date: 2026-09-29. Config: `config/donchian_voltarget_validation.yaml` (base `config/donchian_voltarget.yaml`). Data: Binance spot 4h bars, 2018-01-01 to 2026-09-28. Compare with `donchian_trend_2026-09-24.md` (same strategy, full exposure).

**Superseded** by `donchian_voltarget_2026-10-05.md`, which repeats this validation with corrected tools; its verdict replaces the one below. Configs have changed since: the per-symbol cap in `config/live.yaml` is now 0.5.

## Verdict

Gate FAILED on one of five checks, narrowly: out-of-sample max drawdown -27.0% against a -25% limit (was -28.5% at full exposure). Everything else passed with room to spare. Volatility targeting did what it was meant to do: the same signals, less exposure in violent periods, better risk-adjusted returns. The remaining drawdown comes from one walk-forward window (2022) where the grid search chose an atypical parameter set (entry 90, exit 40) that lost 14%; that is parameter-selection instability, not exposure.

## Choosing the target

`tbot backtest` over the full period, base params, each level a logged trial:

| target volatility | CAGR | Sharpe | max drawdown | avg exposure |
|---|---|---|---|---|
| none | 30.6% | 0.92 | -40.1% | 34.7% |
| 0.8 | 31.5% | 0.98 | -38.9% | 33.1% |
| 0.6 | 31.0% | 1.04 | -34.9% | 30.5% |
| 0.5 | 29.2% | 1.08 | -30.3% | 27.8% |
| 0.4 | 26.5% | 1.14 | -24.9% | 23.9% |
| 0.3 | 20.8% | 1.14 | -19.7% | 18.6% |

The effect is monotone and explainable: lower targets cut drawdown almost linearly while Sharpe rises, because the edge is unchanged and only exposure in high-volatility stretches shrinks. 0.4 keeps the Sharpe of 0.3 with a drawdown at the gate limit; below 0.4 returns fall faster than risk. This is a risk-appetite setting, not a fitted signal parameter, but it was chosen after seeing these numbers.

## Findings (target 0.4)

- **Baseline** in-sample 2018-2024: CAGR 32.2%, Sharpe 1.29, max drawdown -24.9%, 196 trades (full exposure: 38.0%, 1.03, -40.1%).
- **Cost stress** at 2x: CAGR 28.6%, Sharpe 1.17.
- **Sweep** (30 points): Sharpe 1.24 to 1.53 across the grid, a higher plateau than before (0.96 to 1.40). Baseline still ranks 27 of 30; faster exits still win.
- **Walk-forward** (2021-2024): stitched OOS CAGR 22.0%, Sharpe 1.08, max drawdown -27.0%, 123 trades. Windows: +21.3%, -14.1%, +31.0%, +62.3%.
- **Monte Carlo** (2000 shuffles): median max drawdown -19.7%, 5th percentile -29.5%; probability of a drawdown beyond 25%: 16% (was 94%).
- **Deflated Sharpe**: 30 distinct parameter sets on this sample; luck alone reaches 0.16; probability the true Sharpe is above zero: 99.9%.
- **Holdout** (2025-01-01 onward, evaluated 4 times now): CAGR 5.9%, Sharpe 0.40, max drawdown -22.0%, +10.5%, 46 trades.

## Report

```
1. Baseline (entry=55, exit=20), in-sample
                 CAGR   32.2%  Sharpe  1.29  MaxDD  -24.9%  Return  +606.3%  Trades  196

2. Cost stress x2
                 CAGR   28.6%  Sharpe  1.17  MaxDD  -26.7%  Return  +481.6%  Trades  196

3. Parameter sweep: 30 points, objective sharpe
   best entry=40, exit=15: 1.53, neighborhood mean 1.41
   baseline rank 27 of 30, neighborhood mean 1.38
   entry\exit      10      15      20      30      40
           20    1.26    1.43    1.31    1.30    1.39
           30    1.41    1.49    1.31    1.30    1.35
           40    1.47    1.53    1.37    1.29    1.34
           55    1.39    1.41    1.29    1.24    1.28
           70    1.42    1.51    1.43    1.36    1.38
           90    1.29    1.41    1.32    1.30    1.33

4. Walk-forward: train 36m, test 12m
   test 2021-01-01 -> 2022-01-01: entry=30, exit=10  train sharpe 2.01  test Sharpe  1.09  return  +21.3%  trades 54
   test 2022-01-01 -> 2023-01-01: entry=90, exit=40  train sharpe 2.06  test Sharpe -0.90  return  -14.1%  trades 16
   test 2023-01-01 -> 2024-01-01: entry=70, exit=15  train sharpe 1.65  test Sharpe  1.36  return  +31.0%  trades 26
   test 2024-01-01 -> 2025-01-01: entry=70, exit=15  train sharpe 1.01  test Sharpe  2.15  return  +62.3%  trades 27
   stitched OOS  CAGR   22.0%  Sharpe  1.08  MaxDD  -27.0%  Return  +121.6%  Trades  123

5. Monte Carlo: 2000 runs over 196 trades
   max drawdown p5 -29.5%  p50 -19.7%  p95 -14.0%; P(drawdown beyond 25%) = 16.2%

6. Deflated Sharpe: 30 logged trials; luck alone would reach Sharpe 0.16; P(true Sharpe > 0) = 99.9%

7. Holdout (baseline params), evaluated 4 time(s) so far
                 CAGR    5.9%  Sharpe  0.40  MaxDD  -22.0%  Return   +10.5%  Trades   46

8. Gate
   PASS  out-of-sample Sharpe: 1.08 vs 0.8
   FAIL  out-of-sample max drawdown: -0.27 vs -0.25
   PASS  out-of-sample trades: 123 vs 100
   PASS  return at 2x costs: 4.82 vs 0
   PASS  holdout return: 0.105 vs 0
   FAILED
```

## What would move the last check

- Selecting walk-forward parameters by the neighborhood mean instead of the single best point would avoid picks like 90/40 that sit on the grid's edge. That is a change to the selection rule, to be decided before looking at its effect on this holdout.
- A per-symbol cap of 0.3 (as in `config/live.yaml`) lowers exposure further at the cost of return.
- The holdout has now been looked at four times. Treat further holdout numbers as in-sample.
