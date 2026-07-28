# Live Trade Report

Synthetic format sample for the read-only `analytics.trade_report` output. The
numbers below are example rows, not live account performance.

## Scope

- Closed trades: `4`
- First exit UTC: `2026-07-01T10:00:00+00:00`
- Last exit UTC: `2026-07-04T16:00:00+00:00`

## Trade Outcomes

| Scope | Trades | Wins | Losses | BE | Winrate | Winrate ex BE |
| --- | --- | --- | --- | --- | --- | --- |
| ALL | 4 | 2 | 1 | 1 | 50.00% | 66.67% |
| audcad_h4_reversion | 2 | 1 | 0 | 1 | 50.00% | 100.00% |
| xau_h4_continuation_breakout | 2 | 1 | 1 | 0 | 50.00% | 50.00% |

## R-Multiple Metrics

| Scope | Net R | Max DD | Profit Factor | Expectancy | Avg Win | Avg Loss | Best | Worst |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL | 1.00 | 1.00 | 2.00 | 0.250 | 1.00 | -1.00 | 1.00 | -1.00 |
| audcad_h4_reversion | 1.00 | 0.00 | N/A | 0.500 | 1.00 | N/A | 1.00 | 0.00 |
| xau_h4_continuation_breakout | 0.00 | 1.00 | 1.00 | 0.000 | 1.00 | -1.00 | 1.00 | -1.00 |
