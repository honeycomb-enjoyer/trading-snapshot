# Strategy Profile: BasicMeanReversion_AUDCAD

## Identity and dataset

- Symbol: `AUDCAD`
- Asset class: `fx`
- Strategy class: `BasicMeanReversion`
- Dataset: `full`; 413534 bars
- Signal timeframe: `H4`
- Execution timeframe: `M5`
- Lower-timeframe replay: `enabled`
- Replay entry / exit bar offsets: `0` / `0`
- Replay skips - incomplete closed history / unavailable entry bar: `0` / `0`
- Period: `2021-01-03 22:00:00+00:00` - `2026-07-15 20:55:00+00:00`
- Venue: `composite`; timezone: `UTC`
- Dataset SHA-256: `22cb281a11ff2f30f710b722668d972d5002683102895899c8caebc3a3320e31`

### Data quality warnings

- 6 suspicious data gap(s); first: Suspicious gap from 2023-12-25T06:00:00+00:00 to 2023-12-25T22:00:00+00:00 (0 days 16:00:00)
- 261 suspicious data gap(s); first: Suspicious gap from 2021-02-15T22:20:00+00:00 to 2021-02-15T22:30:00+00:00 (0 days 00:10:00)

## Parameters

| Parameter | Value |
|---|---:|
| `range_lookback` | `16` |
| `atr_period` | `20` |
| `atr_multiplier` | `1.0` |
| `tp_fraction` | `1.0` |

## Management

| Setting | Value |
|---|---:|
| `use_break_even` | `False` |
| `break_even_trigger` | `0.0` |
| `break_even_offset` | `0.0` |
| `daily_sl_limit` | `2` |
| `weekly_sl_limit` | `4` |
| `max_simultaneous_positions` | `1` |
| `execution_mode` | `open_bar` |
| `close_positions_on_friday` | `True` |
| `friday_close_time_utc` | `22:00` |

## Full backtest

| Metric | Result |
|---|---:|
| Trades | 1010 |
| Wins / losses / BE | 462 / 548 / 0 |
| Win rate | 45.74% |
| Net R | 129.41R |
| Max drawdown | 13.88R |
| Profit factor | 1.242 |
| Expectancy | 0.128R |
| Average win / loss | 1.44R / -0.98R |
| Best / worst trade | 3.33R / -1.14R |
| Average execution cost | 0.081R |
| Median execution cost | 0.078R |
| P90 execution cost | 0.107R |
| Execution cost profile | AUDCAD / baseline_cost_profile |
| Max consecutive wins / losses | 8 / 9 |
| Calendar time in market | 34.31% |
| Available M5 bars with a position | 48.24% |
| Max simultaneous positions | 1 |
| Same-bar exits | 24 (2.38%) |
| Same-bar SL+TP, stop-first | 0 |

## Train and holdout

| Segment | Trades | Net R | Max DD | PF | Expectancy | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| train | 740 | 108.66R | 13.88R | 1.278 | 0.147R | 46.35% |
| holdout | 270 | 20.75R | 10.71R | 1.144 | 0.077R | 44.07% |

## Year-by-year stability

| Year | Trades | Net R | Max DD | PF | Expectancy |
|---:|---:|---:|---:|---:|---:|
| 2021 | 190 | 28.71R | 10.74R | 1.292 | 0.151R |
| 2022 | 185 | 38.82R | 8.24R | 1.406 | 0.210R |
| 2023 | 182 | 26.94R | 13.88R | 1.275 | 0.148R |
| 2024 | 183 | 14.19R | 11.56R | 1.144 | 0.078R |
| 2025 | 172 | 17.25R | 10.68R | 1.192 | 0.100R |
| 2026 | 98 | 3.50R | 10.71R | 1.065 | 0.036R |

Rolling 365-day Net R: minimum `-4.24R`, median `22.55R`, maximum `44.72R`.

Longest max-drawdown episode: `58` trades to trough and `69` trades to recovery.

## Monte Carlo

Mode: `shuffle`, simulations: `1000`.

| Metric | Result |
|---|---:|
| Mean / median max DD | 21.21R / 20.13R |
| Best / worst max DD | 11.33R / 42.89R |
| 95% worst max DD | 31.67R |
| Probability DD > 10R | 100.0% |
| Probability DD > 15R | 92.1% |
| Probability DD > 20R | 50.9% |
| Probability DD > 30R | 7.6% |

## Trade excursion and exposure

| Metric | Mean | Median | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|
| MAE | 0.76R | 0.96R | 1.09R | 1.11R | 1.14R |
| MFE | 1.08R | 1.04R | 2.42R | 2.96R | 4.34R |
| Bar coverage, hours | 16.46 | 10.67 | 51.67 | 81.20 | 116.00 |

## Margin utility

Values are percent of account equity occupied while a position is open, normalized to `1%` risk per trade.
For the linear price-risk model: `margin % = risk % x entry / (effective leverage x stop distance)`.

| Effective leverage | Mean | Median | P95 | P99 | Maximum | Trades >100% |
|---:|---:|---:|---:|---:|---:|---:|
| 1:20 | 18.58% | 18.51% | 25.19% | 27.64% | 32.29% | 0 |
| 1:30 | 12.39% | 12.34% | 16.79% | 18.43% | 21.53% | 0 |
| 1:50 | 7.43% | 7.40% | 10.07% | 11.06% | 12.92% | 0 |
| 1:100 | 3.72% | 3.70% | 5.04% | 5.53% | 6.46% | 0 |
| 1:200 | 1.86% | 1.85% | 2.52% | 2.76% | 3.23% | 0 |
| 1:500 | 0.74% | 0.74% | 1.01% | 1.11% | 1.29% | 0 |

Maximum-margin observation across the configured scenarios:

- Effective leverage: `1:20`
- Open time: `2024-05-29 01:30:00+00:00`; side: `SELL`
- Entry / initial SL / price risk: `0.91` / `0.91` / `0.0014`
- Normalized margin at 1% risk: `32.29%`
- Trade result: `1.66R` (`tp`)

## Intraday equity by reset timezone

All values are in R. Peak-to-trough is a conservative M5 OHLC envelope.

| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |
|---|---:|---:|---:|
| UTC | 2.32R / 2.97R | 3.11R / 3.77R | 3.45R / 4.50R |
| Europe/Prague | 2.38R / 3.19R | 3.06R / 3.77R | 3.46R / 4.50R |
| America/New_York | 2.36R / 3.17R | 2.97R / 3.77R | 3.45R / 4.61R |

## Risk scaling reference

This table scales isolated historical and simulated R metrics. It is not a portfolio recommendation.

| Risk per trade | Historical DD | MC 95% DD | MC worst DD | Day-start loss P99 / max | Intraday peak-to-trough P99 / max |
|---:|---:|---:|---:|---:|---:|
| 0.25% | 3.47% | 7.92% | 10.72% | 0.59% / 0.80% | 0.78% / 0.94% |
| 0.33% | 4.58% | 10.45% | 14.15% | 0.78% / 1.05% | 1.03% / 1.25% |
| 0.50% | 6.94% | 15.84% | 21.45% | 1.19% / 1.60% | 1.55% / 1.89% |
| 0.75% | 10.41% | 23.76% | 32.17% | 1.78% / 2.39% | 2.33% / 2.83% |
| 1.00% | 13.88% | 31.67% | 42.89% | 2.38% / 3.19% | 3.11% / 3.77% |
| 1.50% | 20.81% | 47.51% | 64.34% | 3.56% / 4.79% | 4.66% / 5.66% |
| 2.00% | 27.75% | 63.35% | 85.78% | 4.75% / 6.38% | 6.22% / 7.55% |

## Close reasons

| Reason | Trades |
|---|---:|
| `sl` | 466 |
| `tp` | 359 |
| `friday_close` | 157 |
| `sl_same_bar_trigger` | 12 |
| `tp_same_bar_trigger` | 8 |
| `tp_gap` | 5 |
| `sl_gap` | 2 |
| `sl_before_tp` | 1 |

## Limitations

- M5 OHLC cannot identify the true intrabar high/low order.
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
