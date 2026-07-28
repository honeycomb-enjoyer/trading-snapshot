# Backtest Sample

Example terminal-style summary for the default `ContinuationBreakout_XAUUSD`
research run.

## Run Context

- Strategy: `ContinuationBreakout`
- Symbol: `XAUUSD`
- Signal timeframe: `H4`
- Execution replay timeframe: `M1`
- Dataset: `full`
- Period: `2021-01-03 23:00:00+00:00` - `2026-07-22 20:59:00+00:00`
- Bars: `1,968,672`
- Execution cost profile: `baseline_cost_profile`

## Full Backtest

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

## Train / Holdout

| Segment | Trades | Net R | Max DD | PF | Expectancy | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| Train | 311 | 50.11R | 9.37R | 1.329 | 0.161R | 48.87% |
| Holdout | 148 | 37.37R | 8.58R | 1.552 | 0.253R | 52.70% |

## Output Artifacts

- `equity_curve.png` - cumulative backtest equity in R.
- `monthly_returns.png` - calendar-month return profile.

This backtest sample is intentionally compact. The strategy profile sample for
`ContinuationBreakout_XAUUSD` contains the fuller breakdown: execution costs,
year-by-year stability, Monte Carlo, margin utility, intraday equity and close
reason diagnostics.
