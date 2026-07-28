# Incident Runbook

## Broker Disconnect

Stop opening new exposure, verify terminal connectivity, then restart only the
owning hub process. Do not run a second hub instance against the same account.

## Stale Or Missing Ticks

Confirm the symbol is visible in MT5 and that market hours allow updates. Keep
the strategy blocked until ticks resume and guards return to OK.

## Pending Execution Timeout

The order-intent store is the source of truth for unresolved submissions.
Reconcile with broker history before submitting replacement orders.

## Unexpected Open Position

Compare broker position magic numbers against `strategies.yaml`. If the position
belongs to a retained strategy, let reconciliation recover it. If ownership is
unknown, handle it manually and keep the hub halted until state is clear.

## Risk Halt

Do not clear a halt by editing runtime databases. Investigate the daily/weekly
or account-level breach, verify broker state manually, and only then perform a
documented account lifecycle reset if appropriate.

## Analytics Mismatch

Treat `trade_ledger.sqlite3` as authoritative. Regenerate the CSV export rather
than editing `runtime/analytics/trades.csv` by hand.
