# Operations

This document describes the intended local operating model for the sanitized
runtime snapshot. It omits private paths, credentials and account identifiers.

## Preflight

1. Create ignored `secret_config.py` from `secret_config.example.py`.
2. Keep all credentials, terminal paths and chat IDs outside version control.
3. Confirm every enabled `strategies.yaml` entry maps to an account in
   `accounts.py`.
4. Run:

   ```powershell
   python -m shared.config_validator
   python -m pytest -q
   ```

## Shadow Run

Use shadow mode before any real order routing:

```powershell
python run_hub.py hub_demo --shadow
```

Check broker login, symbol visibility, heartbeat text, account monitor status
and strategy state recovery.

## Runtime Files

The runtime writes local state under `runtime/`:

- `account_state.sqlite3`
- `order_intents.sqlite3`
- `trade_ledger.sqlite3`
- `analytics/trades.csv`
- `<hub>.health.json`
- `<hub>.lock`

These files are intentionally excluded from the public snapshot.

## State Reset

For a genuine account lifecycle reset:

1. Stop the hub.
2. Verify the broker account is flat.
3. Set `reset_state_on_startup` to `True` for that account in `accounts.py`.
4. Start the hub once and verify the audit entry.
5. Confirm the flag has returned to `False`.

Do not reset state by deleting SQLite or JSON files while a hub is active.

## Shutdown

Stop the hub from the owning process and let it release the MT5 session and lock
file. Do not start another process for the same hub until the previous process
has fully exited.
