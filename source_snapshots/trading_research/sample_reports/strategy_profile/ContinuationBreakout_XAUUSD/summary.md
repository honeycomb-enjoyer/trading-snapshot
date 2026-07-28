# Strategy Profile: ContinuationBreakout_XAUUSD

## Identity and dataset

- Symbol: `XAUUSD`
- Asset class: `metal`
- Strategy class: `ContinuationBreakout`
- Dataset: `full`; 1968672 bars
- Signal timeframe: `H4`
- Execution timeframe: `M1`
- Lower-timeframe replay: `enabled`
- Replay entry / exit bar offsets: `0` / `0`
- Replay skips - incomplete closed history / unavailable entry bar: `0` / `0`
- Period: `2021-01-03 23:00:00+00:00` - `2026-07-22 20:59:00+00:00`
- Venue: `composite`; timezone: `UTC`
- Dataset SHA-256: `1f9b7be43cbd67ac1551e9f823da693dc14c46dcf943900c681e3ba460378044`

### Data quality warnings

- 23 suspicious data gap(s); first: Suspicious gap from 2021-01-18T14:00:00+00:00 to 2021-01-18T22:00:00+00:00 (0 days 08:00:00)
- 1505 suspicious data gap(s); first: Suspicious gap from 2021-01-04T21:59:00+00:00 to 2021-01-04T23:00:00+00:00 (0 days 01:01:00)

## Parameters

| Parameter | Value |
|---|---:|
| `lookback` | `24` |
| `atr_period` | `20` |
| `sl_atr` | `1.25` |
| `rr` | `1.5` |
| `direction` | `both` |
| `use_return_filter` | `True` |
| `return_filter_timeframe` | `W1` |
| `return_filter_mode` | `continuation` |

## Management

| Setting | Value |
|---|---:|
| `use_break_even` | `False` |
| `break_even_trigger` | `0.0` |
| `break_even_offset` | `0.0` |
| `daily_sl_limit` | `None` |
| `weekly_sl_limit` | `3` |
| `max_simultaneous_positions` | `1` |
| `execution_mode` | `open_bar` |
| `close_positions_on_friday` | `True` |
| `friday_close_time_utc` | `22:00` |

## Full backtest

| Metric | Result |
|---|---:|
| Trades | 459 |
| Wins / losses / BE | 230 / 229 / 0 |
| Win rate | 50.11% |
| Net R | 87.48R |
| Max drawdown | 9.37R |
| Profit factor | 1.398 |
| Expectancy | 0.191R |
| Average win / loss | 1.34R / -0.96R |
| Best / worst trade | 1.50R / -1.35R |
| Average execution cost | 0.031R |
| Median execution cost | 0.018R |
| P90 execution cost | 0.073R |
| Execution cost profile | XAUUSD / baseline_cost_profile |
| Max consecutive wins / losses | 7 / 6 |
| Calendar time in market | 14.78% |
| Available M1 bars with a position | 21.90% |
| Max simultaneous positions | 1 |
| Same-bar exits | 5 (1.09%) |
| Same-bar SL+TP, stop-first | 0 |

## Train and holdout

| Segment | Trades | Net R | Max DD | PF | Expectancy | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| train | 311 | 50.11R | 9.37R | 1.329 | 0.161R | 48.87% |
| holdout | 148 | 37.37R | 8.58R | 1.552 | 0.253R | 52.70% |

## Year-by-year stability

| Year | Trades | Net R | Max DD | PF | Expectancy |
|---:|---:|---:|---:|---:|---:|
| 2021 | 74 | -0.02R | 7.77R | 0.999 | -0.000R |
| 2022 | 79 | 14.44R | 6.29R | 1.370 | 0.183R |
| 2023 | 83 | 23.31R | 6.27R | 1.656 | 0.281R |
| 2024 | 75 | 12.38R | 5.45R | 1.336 | 0.165R |
| 2025 | 96 | 26.78R | 5.08R | 1.630 | 0.279R |
| 2026 | 52 | 10.59R | 8.58R | 1.421 | 0.204R |

Rolling 365-day Net R: minimum `-2.97R`, median `14.76R`, maximum `39.32R`.

Longest max-drawdown episode: `39` trades to trough and `51` trades to recovery.

## Monte Carlo

Mode: `shuffle`, simulations: `1000`.

| Metric | Result |
|---|---:|
| Mean / median max DD | 12.76R / 12.13R |
| Best / worst max DD | 6.47R / 35.97R |
| 95% worst max DD | 18.75R |
| Probability DD > 10R | 80.6% |
| Probability DD > 15R | 21.2% |
| Probability DD > 20R | 3.0% |
| Probability DD > 30R | 0.1% |

## Trade excursion and exposure

| Metric | Mean | Median | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|
| MAE | 0.69R | 0.89R | 1.07R | 1.16R | 1.35R |
| MFE | 0.98R | 1.16R | 1.63R | 1.91R | 2.23R |
| Bar coverage, hours | 15.65 | 10.58 | 51.07 | 68.08 | 94.40 |

## Margin utility

Values are percent of account equity occupied while a position is open, normalized to `1%` risk per trade.
For the linear price-risk model: `margin % = risk % x entry / (effective leverage x stop distance)`.

| Effective leverage | Mean | Median | P95 | P99 | Maximum | Trades >100% |
|---:|---:|---:|---:|---:|---:|---:|
| 1:20 | 7.85% | 7.89% | 11.67% | 13.49% | 15.30% | 0 |
| 1:30 | 5.23% | 5.26% | 7.78% | 8.99% | 10.20% | 0 |
| 1:50 | 3.14% | 3.16% | 4.67% | 5.39% | 6.12% | 0 |
| 1:100 | 1.57% | 1.58% | 2.33% | 2.70% | 3.06% | 0 |
| 1:200 | 0.79% | 0.79% | 1.17% | 1.35% | 1.53% | 0 |
| 1:500 | 0.31% | 0.32% | 0.47% | 0.54% | 0.61% | 0 |

Maximum-margin observation across the configured scenarios:

- Effective leverage: `1:20`
- Open time: `2024-02-29 13:36:00+00:00`; side: `BUY`
- Entry / initial SL / price risk: `2041.34` / `2034.67` / `6.6723`
- Normalized margin at 1% risk: `15.30%`
- Trade result: `1.40R` (`tp`)

## Intraday equity by reset timezone

All values are in R. Peak-to-trough is a conservative M1 OHLC envelope.

| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |
|---|---:|---:|---:|
| UTC | 1.50R / 2.16R | 2.04R / 2.45R | 2.65R / 3.87R |
| Europe/Prague | 1.47R / 2.12R | 2.12R / 2.45R | 2.67R / 3.74R |
| America/New_York | 1.50R / 2.59R | 2.15R / 2.82R | 2.65R / 3.86R |

## Risk scaling reference

This table scales isolated historical and simulated R metrics. It is not a portfolio recommendation.

| Risk per trade | Historical DD | MC 95% DD | MC worst DD | Day-start loss P99 / max | Intraday peak-to-trough P99 / max |
|---:|---:|---:|---:|---:|---:|
| 0.25% | 2.34% | 4.69% | 8.99% | 0.38% / 0.65% | 0.54% / 0.71% |
| 0.33% | 3.09% | 6.19% | 11.87% | 0.50% / 0.85% | 0.71% / 0.93% |
| 0.50% | 4.68% | 9.37% | 17.98% | 0.75% / 1.29% | 1.07% / 1.41% |
| 0.75% | 7.03% | 14.06% | 26.98% | 1.13% / 1.94% | 1.61% / 2.12% |
| 1.00% | 9.37% | 18.75% | 35.97% | 1.50% / 2.59% | 2.15% / 2.82% |
| 1.50% | 14.05% | 28.12% | 53.95% | 2.25% / 3.88% | 3.22% / 4.24% |
| 2.00% | 18.74% | 37.50% | 71.94% | 3.00% / 5.18% | 4.29% / 5.65% |

## Close reasons

| Reason | Trades |
|---|---:|
| `sl` | 203 |
| `tp` | 193 |
| `friday_close` | 53 |
| `sl_gap` | 3 |
| `sl_same_bar_trigger` | 3 |
| `tp_gap` | 2 |
| `tp_same_bar_trigger` | 2 |

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
