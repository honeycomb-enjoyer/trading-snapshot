# Architecture

This snapshot shows a hub-based MT5 runtime for a small strategy portfolio. The
main design constraint is conservative process ownership: one hub owns one MT5
session, and strategies inside the hub share broker connectivity while keeping
state, risk and alerts isolated.

## Components

| Layer | Responsibility |
|---|---|
| `run_hub.py` | CLI entry point, config validation, hub selection |
| `hub_runtime.py` | Hub lifecycle, worker orchestration, heartbeat aggregation |
| `run_bot.py` | Single-strategy diagnostic runner and shared construction helpers |
| `shared/registry.py` | Strategy identity from `strategies.yaml` |
| `core/` | Broker facade, data feed, order execution, position management |
| `risk/` | Account monitor, position sizing, daily/weekly limits, kill switch |
| `analytics/` | Durable trade ledger, CSV export, read-only Markdown reporting |
| `monitoring/` | Telegram-compatible alerts and heartbeat formatting |

## Data And State Flow

1. `strategies.yaml` maps enabled strategy IDs to symbol, asset class, magic
   number and account.
2. `run_hub.py <hub_id>` selects the enabled strategies assigned to that hub.
3. The hub opens a single broker session and builds one worker per strategy.
4. Each worker updates market state, validates guards, submits order intents and
   reconciles cached execution state.
5. Trade outcomes are written to the durable ledger; the CSV export is only a
   reporting compatibility artifact.

## Retained Strategies

- `audcad_h4_reversion`
- `eurgbp_h4_reversion_return_filter`
- `xau_h4_continuation_breakout`

No archived or experimental strategy package is loaded by the public registry.

## Fail-Closed Boundaries

- Unknown hub IDs fail before broker initialization.
- Duplicate magic numbers fail during static registry validation.
- Strategy identity lives in `strategies.yaml`; behavioral parameters live in
  each strategy config.
- Single-instance hub locks prevent two local processes from managing the same
  account scope.
- Order intents are durable so a restart can reconcile unknown submission state
  before opening new exposure.
