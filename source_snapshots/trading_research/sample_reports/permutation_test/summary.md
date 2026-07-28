# Permutation Test Sample

Static example artifacts from the permutation-test workflow.

Example run:

- Strategy: `BasicMeanReversion_AUDCAD`
- Data: `AUDCAD H4`
- Lower-timeframe execution replay: `disabled`
- Permutations: `200`
- Valid permutations: `200`
- Original PF: `1.579`
- Noise median PF: `1.123`
- Noise max PF: `1.326`
- p-value: `0.005`

- `pf_histogram.png` - distribution of profit factors from permuted OHLC paths versus the original strategy result.
- `equity_overlay.png` - original equity curve overlaid against permuted-path equity curves.

These files are included as presentation samples; raw permutation CSV/output dumps are intentionally excluded from the application package.
