# Live Trading Runtime

Sanitized application snapshot of a personal MetaTrader 5 execution runtime.
The package is meant to demonstrate trading-systems engineering, not to provide
credentials, broker connectivity, or a managed-account product.

## Snapshot Scope

The retained portfolio contains three representative strategies:

| Strategy ID | Symbol | Style |
|---|---|---|
| `audcad_h4_reversion` | `AUDCAD` | H4 mean reversion |
| `eurgbp_h4_reversion_return_filter` | `EURGBP` | H4 mean reversion with higher-timeframe return filter |
| `xau_h4_continuation_breakout` | `XAUUSD` | H4 continuation breakout |

Removed from this public snapshot: real credentials, local launchers, runtime
databases, raw broker exports, archived strategies, and private workflow notes.

## Architecture

- `run_hub.py` starts one logical hub and resolves strategy membership from
  `strategies.yaml`.
- `hub_runtime.py` owns one MT5 lifecycle per hub and runs isolated
  `StrategyRuntime` workers inside that process.
- `shared/registry.py` keeps strategy identity in one place: symbol, asset
  class, magic number, account and enabled flag.
- `core/` wraps broker access, order submission, position management and local
  state transitions.
- `risk/` contains account-level loss limits, position sizing and kill-switch
  state.
- `analytics/` records trades and can build a read-only R-multiple report from
  the atomic `runtime/analytics/trades.csv` export.
- `monitoring/` handles heartbeat text and alert routing.

## Configuration

- `secret_config.example.py` is the credential template. Copy it to ignored
  `secret_config.py` for a real local run.
- `accounts.py` defines demo account identity, risk limits and broker-clock
  defaults.
- `portfolio_config.py` defines shared execution, guard, reconnect and alert
  settings.
- `strategies.yaml` defines stable strategy identity only.
- `strategies/<id>/config.py` defines strategy behavior and risk parameters.

Identity and behavior are intentionally separated so broker routing does not
drift from strategy tuning.

## Local Verification

Supported Python: `>=3.12,<3.13`.

```powershell
python -m pip install -r requirements-runtime.txt -r requirements-dev.txt
python -m pytest -q
python -m compileall -q .
```

Unit tests block real MT5 and socket access. A brokerless test suite does not
replace a supervised shadow run against the exact terminal, symbols and account
permissions.

## Example Commands

```powershell
# Static config check. Requires a local ignored secret_config.py or an explicit
# test-safe secret path in code/tests.
python -m shared.config_validator

# Shadow loop for broker/login recovery and heartbeat checks.
python run_hub.py hub_demo --shadow

# Single-strategy diagnostic runner.
python run_bot.py audcad_h4_reversion

# Read-only Markdown trade report from a copied/exported trades.csv file.
python -m analytics.trade_report --csv runtime/analytics/trades.csv --output reports/live_trade_report.md
```

See [sample live trade report](sample_reports/live_trade_report_sample.md) for
the generated Markdown format.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment checklist](docs/DEPLOYMENT_CHECKLIST.md)
- [Incident runbook](docs/INCIDENT_RUNBOOK.md)
- [Operations](docs/OPERATIONS.md)
- [Strategy config standard](docs/strategy_config_standard.md)
