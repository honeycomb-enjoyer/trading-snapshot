# Deployment Checklist

This checklist is for a supervised local deployment of the runtime snapshot.

## Before Start

- `secret_config.py` exists locally and is ignored by Git.
- `python -m shared.config_validator` passes.
- `python -m pytest -q` passes in a brokerless environment.
- `strategies.yaml` contains only intended enabled strategies.
- Broker symbols are visible in the target terminal.
- Runtime state directory is local, writable and backed up if needed.

## Shadow Start

```powershell
python run_hub.py hub_demo --shadow
```

Verify:

- MT5 initializes once for the hub.
- Account monitor is not halted.
- Strategy heartbeat names match the registry.
- No unexpected open positions are detected.
- Trade ledger and analytics paths are writable.

## Supervised Start

```powershell
python run_hub.py hub_demo
```

Keep the first live session supervised. Watch order-intent reconciliation,
position validation, heartbeat status, and account-level loss counters.

## Stop Or Rollback

1. Stop the owning hub process.
2. Confirm no duplicate process is active.
3. Verify current broker positions manually.
4. Keep runtime state intact for reconciliation and audit.
