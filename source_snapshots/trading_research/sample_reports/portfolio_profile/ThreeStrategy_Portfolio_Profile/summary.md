# Portfolio Profile: ThreeStrategy_Portfolio_Profile

## Identity and Dataset

- Source: `reports/strategy_profile/*` machine artifacts
- Period: `2021-01-03 23:00:00+00:00` - `2026-07-15 20:55:00+00:00`
- Bars: `2063701`
- Timeframe model: union of component intraday timestamps; missing component bars are forward-filled

## Components

| Label | Profile | Symbol | Risk per trade | Source Net R | Source Max DD |
|---|---|---:|---:|---:|---:|
| `B` | `BasicMeanReversion_AUDCAD` | `AUDCAD` | 1.00% | 129.41R | 13.88R |
| `E` | `MeanReversionWithReturnFilter_EURGBP` | `EURGBP` | 0.75% | 36.66R | 4.47R |
| `X` | `ContinuationBreakout_XAUUSD` | `XAUUSD` | 0.65% | 87.48R | 9.37R |

## Portfolio Performance

| Metric | Result |
|---|---:|
| Component trade events | 2330 |
| Wins / losses / BE | 1444 / 886 / 0 |
| Win rate | 61.97% |
| Final balance | 214.22% |
| Final equity | 214.22% |
| Closed-event max DD | 14.30% |
| Realized balance max DD | 14.30% |
| Intraday MTM max DD | 15.44% |
| Profit factor | 1.288 |
| Expectancy per event | 0.092% |
| Avg win / loss | 0.66% / -0.84% |
| Best / worst event | 3.33% / -1.14% |
| Max consecutive event wins / losses | 14 / 8 |

## Rolling 365-Day Return

Minimum `-0.65%`, median `37.76%`, maximum `69.85%`.

Longest max-drawdown episode: `34` events to trough and `None` events to recovery.

## Exposure

| Metric | Result |
|---|---:|
| Calendar time in market | 46.17% |
| Bars with position | 65.05% |
| Max simultaneous positions | 3 |

Exposure overlap is percent of bars where both components are active among bars where either component is active.

| Pair | Overlap |
|---|---:|
| `BE` | 11.22% |
| `BX` | 17.87% |
| `EX` | 10.29% |

## Margin Utility

| Scenario | Mean | Median | P95 | P99 | Maximum | Bars >100% |
|---|---:|---:|---:|---:|---:|---:|
| `baseline_mixed_leverage` | 2.77% | 2.95% | 7.46% | 9.35% | 13.34% | 0 |
| `all_symbols_1_20` | 11.23% | 11.42% | 28.46% | 35.60% | 49.47% | 0 |
| `all_symbols_1_30` | 7.49% | 7.61% | 18.97% | 23.73% | 32.98% | 0 |

## Daily Equity

| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |
|---|---:|---:|---:|
| UTC | 2.71% / 3.27% | 3.28% / 4.19% | 3.91% / 5.81% |
| Europe/Prague | 2.71% / 3.27% | 3.27% / 4.19% | 3.97% / 5.10% |
| Etc/GMT-3 | 2.77% / 3.27% | 3.39% / 4.19% | 4.02% / 5.16% |

## Evaluation Rule Simulation

Rule profile: `Generic two-step evaluation`; reset timezone `Etc/GMT-3`; phase targets `[10.0, 6.0]`; daily loss `4.0%`; max loss `12.0%`.

| Horizon | Starts | Pass | Fail | Unresolved | Median pass days | Fail causes |
|---:|---:|---:|---:|---:|---:|---|
| 365d | 155 | 92.9% | 2.6% | 4.5% | 121.3 | max_loss: 2.6% |
| 730d | 155 | 92.9% | 2.6% | 4.5% | 121.3 | max_loss: 2.6% |

## Monte Carlo

Mode: `trade_shuffle`, simulations: `1000`.

| Metric | Result |
|---|---:|
| Mean / median max DD | 18.89% / 18.05% |
| Best / worst max DD | 10.75% / 38.03% |
| 95% worst max DD | 27.27% |
| Probability DD > 10% | 100.0% |
| Probability DD > 15% | 81.6% |
| Probability DD > 20% | 34.4% |
| Probability DD > 30% | 2.1% |

## Daily Closed-Return Correlation

| Component | `B` | `E` | `X` |
|---|---:|---:|---:|
| `B` | 1.000 | -0.018 | 0.015 |
| `E` | -0.018 | 1.000 | -0.001 |
| `X` | 0.015 | -0.001 | 1.000 |

## Limitations

- Portfolio profile is assembled from completed strategy_profile artifacts; it does not re-run strategy logic.
- Component intraday equity is exact only at each component profile timeframe; between missing timestamps it is forward-filled.
- The report does not model live portfolio guards that reject new entries, force-close positions, or resize orders after a breach.
- Trade-shuffle Monte Carlo breaks cross-strategy timing and regime dependence; use it only as a rough tail diagnostic.
- Evaluation rolling starts overlap and should be read as a historical stress map, not independent probabilities.

## Artifact Index

- `summary.md` - Primary human-readable portfolio report.
- `equity_curve.png` - Portfolio realized balance and equity envelope.
- `monthly_returns.png` - Calendar-month portfolio returns.
- `daily_returns_histogram.png` - Histogram of daily realized balance changes.
- `drawdown_distribution.png` - Closed-equity drawdown distribution.
- `mc_drawdown_histogram.png` - Trade-shuffle Monte Carlo max-drawdown histogram.
- `data/summary.json` - Complete machine-readable portfolio metrics and assumptions.
