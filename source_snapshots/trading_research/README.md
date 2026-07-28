# Trading Research

Cleaned application snapshot of a personal Python research lab for systematic
strategy testing. The goal of this snapshot is to show the research and
validation workflow, not to present a turnkey trading product or claim live
alpha.

## What This Demonstrates

- Causal OHLC backtesting with explicit execution semantics.
- Lower-timeframe execution replay for final validation.
- Instrument-aware execution costs: spread, slippage, commission and swap.
- UTC-normalized market-data contracts and dataset provenance manifests.
- Train/holdout splits.
- Grid search with configurable scoring gates.
- Robustness checks: Monte Carlo, permutation test and walk-forward test.
- Strategy-level and portfolio-level report generation.
- Brokerless test suite around the research engine and strategy contracts.

Current sanitized-package verification result:

```text
86 passed
```

## Scope Of This Snapshot

The original private research workspace contained more exploratory scripts and
unfinished strategy ideas. This application package keeps only the parts that
are useful to review:

```text
trading_research/
  data/                 # data contracts, downloaders, UTC normalization
  engine/               # causal backtester, precompute, execution costs
  optimizer/            # grid search and scoring
  overfit_tests/        # Monte Carlo, permutation, walk-forward
  portfolio_profile/    # portfolio-level report assembly
  runners/              # CLI workflow implementations
  sample_reports/       # small selected report artifacts
  strategy/             # three representative implemented strategies
  strategy_profile/     # strategy-level report generation
  tests/                # brokerless/unit coverage
  master_config.py      # single workflow configuration surface
  run_*.py              # thin CLI entrypoints
```

## Included Strategies

### `BasicMeanReversion`

Range-bound mean reversion with ATR-based protective risk. Kept as the simplest
implemented strategy example and as a portfolio component.

### `MeanReversionWithReturnFilter`

Mean reversion with an optional higher-timeframe return filter. This shows how
the same base idea can be gated by completed D1/W1 information without reading
the current incomplete period.

### `ContinuationBreakout`

Breakout/continuation model with ATR risk, optional weekly return filter and
explicit trigger semantics. This is the default strategy in `master_config.py`.

## Pipeline

1. Prepare or load normalized market data through `DataManager`.
2. Select `train`, `holdout` or `full` split.
3. Precompute only the features requested by strategy parameters or optimizer grid.
4. Run a causal backtest with explicit execution-cost assumptions.
5. Search parameter grids on train windows.
6. Validate robustness with Monte Carlo, permutation and walk-forward tests.
7. Build strategy profile artifacts.
8. Assemble portfolio profile from completed strategy profiles.

The root `run_*.py` files are intentionally thin wrappers around `runners/`.
They are kept because they make the workflow discoverable for reviewers.

## Data Boundary

Raw and normalized market-data dumps are intentionally excluded from this
application package. The code still includes the data contracts and downloader
logic because those are part of the evidence: timestamp normalization,
provenance, gap handling and manifest-based references are important to the
research process.

The empty `data/raw/` folder is kept only so tests can create temporary fixture
CSV files.

## Sample Reports

`sample_reports/` contains a small selected set of generated outputs:

- strategy equity curves;
- portfolio equity, monthly return, daily-return and drawdown charts;
- walk-forward charts;
- Monte Carlo equity and drawdown charts;
- permutation-test charts;
- portfolio profile summary;
- machine-readable portfolio summary JSON.

The full private `reports/` folder is not included.

## Running Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q .
```

Some CLI workflows require external market data or MetaTrader/Dukascopy access
and are not expected to run from this sanitized package without providing new
datasets.

## Review Notes

This is best read as evidence of research process and engineering judgement:
how a trading idea is tested, how execution assumptions are made explicit, and
how a strategy is rejected or promoted through reproducible checks.
