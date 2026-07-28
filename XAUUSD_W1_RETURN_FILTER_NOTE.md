# XAUUSD W1 Continuation Filter Note

This note explains the source of the weekly-return filter used in the
`ContinuationBreakout_XAUUSD` sample. It is not presented as production alpha.
It is a compact example of how I moved from a market observation to a testable
strategy filter.

## 1. Research Question

The starting question was:

Can the previous completed XAUUSD weekly candle direction contain enough
continuation tendency to be useful as a directional filter for lower-timeframe
breakout trades?

In plain terms:

- if the previous completed week closed above its open, prefer long breakouts;
- if the previous completed week closed below its open, prefer short breakouts;
- do not use the current incomplete week as information.

## 2. Source Research

The original source study is a bar-return continuation report:

- instrument: XAUUSD
- timeframe: W1
- return definition: `log(close / open)` for each complete weekly bar
- signal: sign of the previous completed weekly return
- measured outcome: next weekly return in the same direction

Source artifact names in the research workspace:

- `reports/bar_return_research/xauusd/w1/train/summary.txt`
- `reports/bar_return_research/xauusd/w1/full/summary.txt`

The public snapshot does not include the full research workspace, but the
strategy sample keeps the resulting filter configuration and implementation.

## 3. Key Findings From The Source Study

Train split:

- observations: 208 complete weekly observations
- period: 2021-01-02 to 2024-12-28
- previous-to-next weekly return correlation: 0.0650
- unconditional continuation rate: 51.44%
- mean continuation return: 14.722 bps
- compounded gross continuation return: 35.83%
- gross equity max drawdown: 11.21%

Full sample:

- observations: 287 complete weekly observations
- period: 2021-01-02 to 2026-07-04
- previous-to-next weekly return correlation: 0.0428
- unconditional continuation rate: 52.61%
- mean continuation return: 22.193 bps
- compounded gross continuation return: 89.07%
- gross equity max drawdown: 14.58%

The signal is weak, noisy and not enough by itself. I treated it as a directional
bias, not as a standalone trading system.

The most useful bin in the full sample was the `100 to 300 bps` previous-week
return bucket:

- observations: 81
- continuation rate: 65.432%
- mean continuation return: 60.160 bps
- 95% CI for mean continuation return: 15.992 to 104.329 bps

This supported the idea that moderate weekly directional movement in XAUUSD may
sometimes carry into the next week better than random direction choice.

## 4. How The Filter Enters The Strategy

In the public snapshot, the XAUUSD breakout strategy is configured as:

```python
STRATEGY_PARAMS = {
    "lookback": 24,
    "atr_period": 20,
    "sl_atr": 1.25,
    "rr": 1.5,
    "direction": "both",
    "use_return_filter": True,
    "return_filter_timeframe": "W1",
    "return_filter_mode": "continuation",
}
```

The strategy logic then maps the previous completed W1 return into an allowed
side:

```python
previous_return = completed_return.shift(1)

if previous_return > 0:
    allowed_side = "BUY"
elif previous_return < 0:
    allowed_side = "SELL"
else:
    allowed_side = None
```

So the filter does not create an entry by itself. It only blocks H4 breakouts
that go against the completed weekly directional bias.

## 5. Lookahead Controls

The filter was designed to avoid a common backtest mistake: reading the current
incomplete higher-timeframe candle.

The implementation:

- groups lower-timeframe bars into W1 periods;
- computes each weekly return from that completed week's open and close;
- shifts the weekly return by one period before using it;
- blocks trades if a completed higher-timeframe signal is missing;
- validates that the filter timeframe is higher than the strategy timeframe.

This means a trade inside week `T` can only use the completed return from week
`T-1`.

## 6. What The Strategy Then Tests

The final strategy is not simply "buy after an up week" or "sell after a down
week".

The actual XAUUSD sample tests whether H4 range breakouts, filtered by the
previous completed weekly direction, can survive more realistic validation:

- H4 signal timeframe;
- M1 lower-timeframe execution replay in the private research report;
- ATR-based stop distance;
- fixed reward/risk target;
- explicit spread, slippage, commission and swap assumptions;
- train/holdout reporting;
- year-by-year stability;
- Monte Carlo drawdown diagnostics;
- ambiguous same-bar breakout rejection.

The strategy profile in the public snapshot reports:

- trades: 459
- train net result: 50.11R
- holdout net result: 37.37R
- full-sample profit factor: 1.398
- full-sample expectancy: 0.191R
- full-sample max drawdown: 9.37R
- Monte Carlo 95% max drawdown: 18.75R

Again, I do not treat this as proof of production alpha. I treat it as an
example of a research workflow: find a weak market tendency, convert it into a
causal filter, then test whether a more complete strategy still looks coherent
after costs, execution assumptions and robustness checks.
