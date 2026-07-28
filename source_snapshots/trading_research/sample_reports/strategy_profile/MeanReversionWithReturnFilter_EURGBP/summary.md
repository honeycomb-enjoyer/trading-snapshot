# Strategy Profile: MeanReversionWithReturnFilter_EURGBP

## Identity and dataset

- Symbol: `EURGBP`
- Asset class: `fx`
- Strategy class: `MeanReversionWithReturnFilter`
- Dataset: `full`; 2056897 bars
- Signal timeframe: `H4`
- Execution timeframe: `M1`
- Lower-timeframe replay: `enabled`
- Replay entry / exit bar offsets: `0` / `0`
- Replay skips - incomplete closed history / unavailable entry bar: `0` / `0`
- Period: `2021-01-03 22:00:00+00:00` - `2026-07-15 20:59:00+00:00`
- Venue: `composite`; timezone: `UTC`
- Dataset SHA-256: `e4b071a69985497b9178930ec3025b6845ef1b4c9265ad8e76686f7dcb55cd2e`

### Data quality warnings

- 6 suspicious data gap(s); first: Suspicious gap from 2023-12-25T06:00:00+00:00 to 2023-12-25T22:00:00+00:00 (0 days 16:00:00)
- 7811 suspicious data gap(s); first: Suspicious gap from 2021-01-04T22:42:00+00:00 to 2021-01-04T22:44:00+00:00 (0 days 00:02:00)

## Parameters

| Parameter | Value |
|---|---:|
| `range_lookback` | `12` |
| `atr_period` | `20` |
| `atr_multiplier` | `2.0` |
| `tp_fraction` | `0.25` |
| `use_return_filter` | `True` |
| `return_filter_timeframe` | `W1` |
| `return_filter_mode` | `reversion` |

## Management

| Setting | Value |
|---|---:|
| `use_break_even` | `False` |
| `break_even_trigger` | `0.0` |
| `break_even_offset` | `0.0` |
| `daily_sl_limit` | `None` |
| `weekly_sl_limit` | `4` |
| `max_simultaneous_positions` | `1` |
| `execution_mode` | `open_bar` |
| `close_positions_on_friday` | `True` |
| `friday_close_time_utc` | `22:00` |

## Full backtest

| Metric | Result |
|---|---:|
| Trades | 862 |
| Wins / losses / BE | 752 / 110 / 0 |
| Win rate | 87.24% |
| Net R | 36.66R |
| Max drawdown | 4.47R |
| Profit factor | 1.405 |
| Expectancy | 0.043R |
| Average win / loss | 0.17R / -0.82R |
| Best / worst trade | 0.62R / -1.30R |
| Average execution cost | 0.031R |
| Median execution cost | 0.030R |
| P90 execution cost | 0.042R |
| Execution cost profile | EURGBP / baseline_cost_profile |
| Max consecutive wins / losses | 47 / 3 |
| Calendar time in market | 9.65% |
| Available M1 bars with a position | 13.64% |
| Max simultaneous positions | 1 |
| Same-bar exits | 45 (5.22%) |
| Same-bar SL+TP, stop-first | 0 |

## Train and holdout

| Segment | Trades | Net R | Max DD | PF | Expectancy | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| train | 622 | 28.33R | 4.47R | 1.430 | 0.046R | 87.14% |
| holdout | 240 | 8.34R | 3.37R | 1.338 | 0.035R | 87.50% |

## Year-by-year stability

| Year | Trades | Net R | Max DD | PF | Expectancy |
|---:|---:|---:|---:|---:|---:|
| 2021 | 155 | 1.94R | 3.90R | 1.092 | 0.013R |
| 2022 | 149 | 9.93R | 4.18R | 1.676 | 0.067R |
| 2023 | 158 | 5.92R | 4.47R | 1.347 | 0.037R |
| 2024 | 160 | 10.53R | 2.24R | 1.810 | 0.066R |
| 2025 | 152 | 6.05R | 3.37R | 1.399 | 0.040R |
| 2026 | 88 | 2.29R | 2.68R | 1.240 | 0.026R |

Rolling 365-day Net R: minimum `-3.82R`, median `6.18R`, maximum `15.76R`.

Longest max-drawdown episode: `74` trades to trough and `110` trades to recovery.

## Monte Carlo

Mode: `shuffle`, simulations: `1000`.

| Metric | Result |
|---|---:|
| Mean / median max DD | 6.12R / 5.81R |
| Best / worst max DD | 3.26R / 16.62R |
| 95% worst max DD | 8.99R |
| Probability DD > 10R | 3.1% |
| Probability DD > 15R | 0.2% |
| Probability DD > 20R | 0.0% |
| Probability DD > 30R | 0.0% |

## Trade excursion and exposure

| Metric | Mean | Median | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|
| MAE | 0.33R | 0.19R | 1.02R | 1.04R | 1.30R |
| MFE | 0.20R | 0.18R | 0.39R | 0.64R | 1.25R |
| Bar coverage, hours | 5.42 | 1.72 | 23.21 | 40.24 | 75.65 |

## Margin utility

Values are percent of account equity occupied while a position is open, normalized to `1%` risk per trade.
For the linear price-risk model: `margin % = risk % x entry / (effective leverage x stop distance)`.

| Effective leverage | Mean | Median | P95 | P99 | Maximum | Trades >100% |
|---:|---:|---:|---:|---:|---:|---:|
| 1:20 | 13.77% | 13.82% | 19.92% | 22.48% | 24.17% | 0 |
| 1:30 | 9.18% | 9.22% | 13.28% | 14.99% | 16.11% | 0 |
| 1:50 | 5.51% | 5.53% | 7.97% | 8.99% | 9.67% | 0 |
| 1:100 | 2.75% | 2.76% | 3.98% | 4.50% | 4.83% | 0 |
| 1:200 | 1.38% | 1.38% | 1.99% | 2.25% | 2.42% | 0 |
| 1:500 | 0.55% | 0.55% | 0.80% | 0.90% | 0.97% | 0 |

Maximum-margin observation across the configured scenarios:

- Effective leverage: `1:20`
- Open time: `2026-06-04 09:18:00+00:00`; side: `SELL`
- Entry / initial SL / price risk: `0.86` / `0.87` / `0.0018`
- Normalized margin at 1% risk: `24.17%`
- Trade result: `0.10R` (`tp`)

## Intraday equity by reset timezone

All values are in R. Peak-to-trough is a conservative M1 OHLC envelope.

| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |
|---|---:|---:|---:|
| UTC | 1.12R / 1.88R | 1.32R / 2.05R | 1.36R / 2.05R |
| Europe/Prague | 1.12R / 1.88R | 1.27R / 1.93R | 1.36R / 2.00R |
| America/New_York | 1.12R / 1.91R | 1.36R / 2.02R | 1.37R / 2.02R |

## Risk scaling reference

This table scales isolated historical and simulated R metrics. It is not a portfolio recommendation.

| Risk per trade | Historical DD | MC 95% DD | MC worst DD | Day-start loss P99 / max | Intraday peak-to-trough P99 / max |
|---:|---:|---:|---:|---:|---:|
| 0.25% | 1.12% | 2.25% | 4.15% | 0.28% / 0.48% | 0.34% / 0.51% |
| 0.33% | 1.47% | 2.97% | 5.48% | 0.37% / 0.63% | 0.45% / 0.68% |
| 0.50% | 2.23% | 4.50% | 8.31% | 0.56% / 0.95% | 0.68% / 1.03% |
| 0.75% | 3.35% | 6.75% | 12.46% | 0.84% / 1.43% | 1.02% / 1.54% |
| 1.00% | 4.47% | 8.99% | 16.62% | 1.12% / 1.91% | 1.36% / 2.05% |
| 1.50% | 6.70% | 13.49% | 24.92% | 1.69% / 2.86% | 2.03% / 3.08% |
| 2.00% | 8.93% | 17.99% | 33.23% | 2.25% / 3.81% | 2.71% / 4.10% |

## Close reasons

| Reason | Trades |
|---|---:|
| `tp` | 682 |
| `sl` | 75 |
| `tp_same_bar_trigger` | 43 |
| `friday_close` | 36 |
| `tp_gap` | 20 |
| `sl_gap` | 4 |
| `sl_same_bar_trigger` | 2 |

## Limitations

- M1 OHLC cannot identify the true intrabar high/low order.
- Margin uses effective-leverage scenarios, not historical broker order_calc_margin snapshots.
- Execution cost is the configured R deduction; spread, swap, and slippage are not reconstructed unless supplied by the backtest.
- Shuffle Monte Carlo changes trade order but does not model regime persistence or cross-strategy dependence.

## Artifact index

- `summary.md` - Primary human-readable strategy report.
- `equity_curve.png` - Strategy equity curve.
- `monthly_returns.png` - Calendar-month returns chart.
- `data/summary.json` - Complete machine-readable metrics and assumptions.
- `data/trades.csv` - Enriched closed trades with risk, MAE/MFE, timing, and margin scenarios.
- `data/equity_curve.csv` - Trade-close equity curve in R.
- `data/monthly_returns.csv` - Calendar-month returns in R.
- `data/intraday_equity.csv` - Execution-timeframe mark-to-market equity envelope and simultaneous margin scenarios.
- `data/daily_equity.csv` - Daily equity diagnostics for every configured reset timezone.
- `data/monte_carlo_runs.csv` - Final R and max drawdown for every Monte Carlo run.
- `data/manifest.json` - Artifact inventory and schema version.
