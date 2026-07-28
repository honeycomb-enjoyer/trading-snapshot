# Strategy Config Standard

The runtime separates stable identity from strategy behavior.

## Identity

Identity lives in `strategies.yaml`:

- `symbol`
- `asset_class`
- `magic`
- `account`
- `enabled`

Every `strategies/<id>/config.py` imports these fields through the shared
registry. Strategy configs should not hardcode duplicated identity values.

## Behavior

Behavior and risk parameters stay in `strategies/<id>/config.py`:

- `SIGNAL_TIMEFRAME`
- `RISK_PER_TRADE_USD`
- `DAILY_SL_LIMIT_USD`
- `WEEKLY_SL_LIMIT_USD`
- stop-loss and take-profit model fields
- strategy-specific indicator or filter parameters

## Validation

Run the static validator before any supervised runtime start:

```powershell
python -m shared.config_validator
```

The validator checks registry shape, duplicate magic numbers, strategy package
presence, credential coverage and config proxy consistency without importing or
initializing MT5.
