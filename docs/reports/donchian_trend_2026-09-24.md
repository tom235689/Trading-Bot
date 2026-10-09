# Validation Report: donchian_trend on BTCUSDT, ETHUSDT 4h

Date: 2026-09-24. Config: `config/donchian_validation.yaml`. Data: Binance spot 4h bars, 2018-01-01 to 2026-09-23.

**Superseded** by `donchian_voltarget_2026-10-05.md`, which repeats the full-exposure candidate with corrected tools; its verdict replaces the one below.

## Verdict

Gate FAILED on one of five checks: the out-of-sample max drawdown is -28.5% against a -25% limit. Everything else passed. The strategy has a real but modest edge whose cost is deep drawdowns; at 100% exposure it cannot meet the drawdown limit. The next lever is exposure control (volatility targeting, per-symbol caps), not parameter tuning.

## Findings

- **Baseline** (entry 55, exit 20) in-sample 2018-2024: CAGR 38.0%, Sharpe 1.03, max drawdown -40.1%, 196 trades.
- **Cost stress** at 2x fees and slippage: CAGR 32.3%, Sharpe 0.92. Costs are not what makes or breaks this strategy.
- **Sweep** (30 points): Sharpe ranges 0.96 to 1.40 across the whole grid, a plateau rather than a peak. Faster exits (exit 10-15) are better everywhere. The baseline ranks 26 of 30; its neighborhood mean is 1.17. Best point: entry 40, exit 10 (Sharpe 1.40, neighborhood 1.30).
- **Walk-forward** (train 36 months, test 12 months, 2021-2024): stitched out-of-sample CAGR 28.9%, Sharpe 0.95, max drawdown -28.5%, 159 trades. Three of four test years were positive; 2022 lost 4.8%. Chosen params moved between windows (entry 30-70, exit 10-15), consistent with the flat plateau.
- **Monte Carlo** (2000 shuffles of 196 trades): median max drawdown -34%, 5th percentile -49%. A drawdown beyond 25% occurs in 94% of orderings, so the observed -40% was ordinary for this strategy.
- **Deflated Sharpe**: 30 distinct parameter sets on the in-sample data; luck alone would reach an annualized Sharpe of 0.24; probability the baseline's true Sharpe is above zero: 98%.
- **Holdout** (2025-01-01 onward, evaluated twice): CAGR 4.6%, Sharpe 0.30, max drawdown -30.7%, +8.1%, 46 trades. Positive but weak; 2025-2026 was not a trend-friendly period for these two coins.

## Report

```
1. Baseline (entry=55, exit=20), in-sample
                 CAGR   38.0%  Sharpe  1.03  MaxDD  -40.1%  Return  +852.2%  Trades  196

2. Cost stress x2
                 CAGR   32.3%  Sharpe  0.92  MaxDD  -43.2%  Return  +610.5%  Trades  196

3. Parameter sweep: 30 points, objective sharpe
   best entry=40, exit=10: 1.40, neighborhood mean 1.30
   baseline rank 26 of 30, neighborhood mean 1.17
   entry\exit      10      15      20      30      40
           20    0.96    1.13    0.97    1.01    1.16
           30    1.26    1.26    1.03    1.03    1.12
           40    1.40    1.37    1.15    1.10    1.15
           55    1.29    1.21    1.03    1.03    1.03
           70    1.32    1.31    1.18    1.14    1.14
           90    1.16    1.19    1.05    1.05    1.07

4. Walk-forward: train 36m, test 12m
   test 2021-01-01 -> 2022-01-01: entry=30, exit=10  train sharpe 1.87  test Sharpe  1.24  return  +56.9%  trades 54
   test 2022-01-01 -> 2023-01-01: entry=40, exit=10  train sharpe 1.81  test Sharpe -0.08  return   -4.8%  trades 35
   test 2023-01-01 -> 2024-01-01: entry=70, exit=10  train sharpe 1.57  test Sharpe  0.96  return  +20.9%  trades 30
   test 2024-01-01 -> 2025-01-01: entry=40, exit=15  train sharpe 1.04  test Sharpe  1.44  return  +52.9%  trades 40
   stitched OOS  CAGR   28.9%  Sharpe  0.95  MaxDD  -28.5%  Return  +176.0%  Trades  159

5. Monte Carlo: 2000 runs over 196 trades
   max drawdown p5 -49.1%  p50 -34.1%  p95 -24.5%; P(drawdown beyond 25%) = 93.6%

6. Deflated Sharpe: 30 logged trials; luck alone would reach Sharpe 0.24; P(true Sharpe > 0) = 98.2%

7. Holdout (baseline params), evaluated 2 time(s) so far
                 CAGR    4.6%  Sharpe  0.30  MaxDD  -30.7%  Return    +8.1%  Trades   46

8. Gate
   PASS  out-of-sample Sharpe: 0.949 vs 0.8
   FAIL  out-of-sample max drawdown: -0.285 vs -0.25
   PASS  out-of-sample trades: 159 vs 100
   PASS  return at 2x costs: 6.1 vs 0
   PASS  holdout return: 0.0811 vs 0
   FAILED
```

## Caveats

- Two symbols, one market regime history (2018-2026). Breadth across more coins is still open.
- Walk-forward segments start flat, so a position open across a window boundary is closed and re-entered.
- The holdout has now been evaluated twice (once during development of the report). Each further look makes it less of a holdout.
