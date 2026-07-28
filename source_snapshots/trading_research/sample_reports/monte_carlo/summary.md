# Monte Carlo Sample

Example terminal-style summary for the `ContinuationBreakout_XAUUSD`
robustness-check workflow.

## Run Context

- Strategy: `ContinuationBreakout`
- Symbol: `XAUUSD`
- Mode: `shuffle`
- Simulations: `1000`
- Source trades: `459`
- Baseline net result: `87.48R`

In shuffle mode, every run contains the same closed trades in a different
sequence. Final R therefore stays effectively constant; the useful output is
the drawdown distribution and path dependency.

## Drawdown Results

| Metric | Result |
|---|---:|
| Mean max DD | 12.76R |
| Median max DD | 12.13R |
| Best max DD | 6.47R |
| Worst max DD | 35.97R |
| 95% worst max DD | 18.75R |
| 99% worst max DD | 22.58R |
| Probability DD > 10R | 80.6% |
| Probability DD > 15R | 21.2% |
| Probability DD > 20R | 3.0% |
| Probability DD > 30R | 0.1% |

## Output Artifacts

- `mc_equity_distribution.png` - readable bootstrap equity-path sample with 80 paths, a P5-P95 band and a median path.
- `mc_drawdown_histogram.png` - max-drawdown distribution across Monte Carlo runs.

The source workspace can regenerate fuller Monte Carlo outputs when market data
is available.
