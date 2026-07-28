"""Focused safety regressions for P0-D01.

These are brokerless fixtures: the reconciliation case proves broker close
deal IDs, durable ledger deals, and exported CSV agree on the same close.
"""

from __future__ import annotations

import csv
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

# Reconciliation is tested with an injected history fixture; no local MT5
# installation or broker process is required.
if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = types.SimpleNamespace(
        DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2, ORDER_TYPE_BUY=0,
    )

from analytics import trade_reconciliation as reconciliation_module
from analytics.trade_logger import TradeLogger
from analytics.trade_store import TradeStore
from analytics.trade_reconciliation import ReconciliationWatermarkStore, TradeReconciliation
from risk.account_monitor import AccountMonitor
from risk.risk_manager import RiskManager


class ClockBroker:
    def __init__(self, now):
        self.now = now

    def broker_now(self):
        return self.now

    def decode_close_reason(self, _payload):
        return "EXTERNAL_CLOSE"


class State:
    def __init__(self, cache=None):
        self.state = {"execution_cache": cache or {}}

    def clear_execution_cache(self, ticket):
        self.state["execution_cache"].pop(str(ticket), None)


class Deal:
    def __init__(
        self, ticket, position_id, entry, time, *, price, volume, profit=0.0,
        magic=7, reason=0,
    ):
        self.ticket = ticket
        self.position_id = position_id
        self.entry = entry
        self.time = time
        self.symbol = "EURUSD"
        self.magic = magic
        self.type = 0
        self.price = price
        self.volume = volume
        self.profit = profit
        self.commission = 0.0
        self.swap = 0.0
        self.reason = reason
        self.comment = "manual"
        self.order = 101


class P0D01Tests(unittest.TestCase):
    def test_lowercase_registry_id_blocks_realized_and_projected_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeStore(Path(directory) / "ledger.sqlite3")
            logger = TradeLogger(
                "hub_demo", store=store, csv_path=Path(directory) / "trades.csv",
                strategy_id="alpha", strategy_name="ALPHA",
            )
            trade_id = logger.record_trade_open(
                "11", 7, "ALPHA", "EURUSD", "BUY", datetime(2026, 7, 11, tzinfo=timezone.utc),
                1.0, 1.0, 0, 1, 0.9, 1.1, 0.1, 0.1, 1, 30, 1000,
            )
            logger.record_trade_close(
                trade_id, datetime(2026, 7, 11, 12, tzinfo=timezone.utc), 0.9,
                "SL", -30, None, None, 1,
            )
            config = types.SimpleNamespace(
                STRATEGY_NAME="ALPHA", DAILY_SL_LIMIT_USD=100, WEEKLY_SL_LIMIT_USD=100,
                RISK_PER_TRADE_USD=80, RISK_BUFFER=1.0, ALLOW_UNDERSIZED_LOT=True, MAX_LOT=None,
            )
            risk = RiskManager(ClockBroker(datetime(2026, 7, 11, 12, tzinfo=timezone.utc)), config, None, logger)
            allowed, reason = risk.can_open_new_trade()
            self.assertFalse(allowed)
            self.assertEqual(reason, "DAILY_LOSS_LIMIT_PROJECTED")
            self.assertEqual(logger.get_daily_strategy_pnl("ALPHA", risk.broker.broker_now()), 0.0)
            self.assertEqual(logger.get_daily_strategy_pnl("alpha", risk.broker.broker_now()), -30.0)

    def test_lock_transition_is_announced_once(self):
        class Logger:
            strategy_id = "alpha"
            def get_daily_strategy_pnl(self, *_args): return -100.0
            def get_weekly_strategy_pnl(self, *_args): return 0.0
        class Alerts:
            def __init__(self): self.messages = []
            def send_throttled_warning(self, **kwargs): self.messages.append(kwargs)
        config = types.SimpleNamespace(
            STRATEGY_NAME="ALPHA", DAILY_SL_LIMIT_USD=100, WEEKLY_SL_LIMIT_USD=None,
            RISK_PER_TRADE_USD=1, RISK_BUFFER=1.0, ALLOW_UNDERSIZED_LOT=True, MAX_LOT=None,
        )
        alerts = Alerts()
        risk = RiskManager(ClockBroker(datetime(2026, 7, 11, tzinfo=timezone.utc)), config, None, Logger(), alerts)
        self.assertFalse(risk.can_open_new_trade()[0])
        self.assertFalse(risk.can_open_new_trade()[0])
        self.assertEqual(len(alerts.messages), 1)
        self.assertEqual(risk.status_snapshot()["lock_state"], "DAILY_LOCKED")

    def test_exit_only_window_closes_existing_row_and_preserves_parity(self):
        original_mt5 = reconciliation_module.mt5

        class TpBroker(ClockBroker):
            def decode_close_reason(self, _payload):
                return "TP"

        class Alerts:
            def __init__(self):
                self.closed = []

            def alert_position_closed(self, **kwargs):
                self.closed.append(kwargs)

        try:
            with tempfile.TemporaryDirectory() as directory:
                now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
                store = TradeStore(Path(directory) / "ledger.sqlite3")
                csv_path = Path(directory) / "trades.csv"
                logger = TradeLogger("hub_demo", store=store, csv_path=csv_path, strategy_id="alpha", strategy_name="ALPHA")
                logger.record_trade_open("99", 7, "ALPHA", "EURUSD", "BUY", now - timedelta(days=2), 1, 1, 0, 1, 0.9, 1.1, 0.1, 0.1, 1, 20, 1000)
                close = Deal(501, 99, 1, now, price=1.2, volume=1, profit=25)
                alerts = Alerts()
                reconciliation_module.mt5 = types.SimpleNamespace(
                    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2,
                    history_deals_get=lambda _start, _end: [close], ORDER_TYPE_BUY=0,
                )
                reconciliation = TradeReconciliation(
                    TpBroker(now), logger, State(), alerts,
                    types.SimpleNamespace(SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA"),
                    watermark_path=Path(directory) / "watermark.json",
                )
                self.assertEqual(reconciliation.reconcile(), 0)
                trade = logger.get_trade_by_ticket("99")
                self.assertEqual(trade["status"], "CLOSED")
                self.assertEqual(trade["close_reason"], "TP")
                self.assertEqual(
                    alerts.closed,
                    [{
                        "strategy_name": "ALPHA",
                        "pnl_usd": 25.0,
                        "r_multiple": 1.25,
                        "reason": "TP",
                    }],
                )
                self.assertEqual(logger.ledger_deal_ids(now - timedelta(minutes=1), now + timedelta(minutes=1), entry_type="OUT"), {"501"})
                with csv_path.open(newline="", encoding="utf-8") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(rows[0]["deal_ids"], "501")
                self.assertEqual(reconciliation.health_snapshot()["status"], "SYNCED")
                reconciliation.reconcile()
                self.assertEqual(len(alerts.closed), 1)
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_manual_magic_zero_close_recovers_open_row_behind_watermark(self):
        original_mt5 = reconciliation_module.mt5

        class ManualBroker(ClockBroker):
            def decode_close_reason(self, payload):
                return "MANUAL_CLOSE" if payload.get("reason") == 4 else "EXTERNAL_CLOSE"

        try:
            with tempfile.TemporaryDirectory() as directory:
                now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
                store = TradeStore(Path(directory) / "ledger.sqlite3")
                logger = TradeLogger(
                    "hub_demo", store=store,
                    csv_path=Path(directory) / "trades.csv",
                    strategy_id="alpha", strategy_name="ALPHA",
                )
                logger.record_trade_open(
                    "99", 7, "ALPHA", "EURUSD", "BUY",
                    now - timedelta(days=10), 1.0, 1.0, 0, 1.0,
                    0.9, 1.1, 0.1, 0.1, 1, 20, 1000,
                )
                entry = Deal(
                    500, 99, 0, now - timedelta(days=10),
                    price=1.0, volume=1.0, magic=7,
                )
                close = Deal(
                    501, 99, 1, now - timedelta(days=1),
                    price=1.05, volume=1.0, profit=50.0, magic=0, reason=4,
                )

                def history(*_args, **kwargs):
                    return [entry, close] if kwargs.get("position") == 99 else []

                reconciliation_module.mt5 = types.SimpleNamespace(
                    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2,
                    DEAL_ENTRY_INOUT=3, DEAL_REASON_CLIENT=4,
                    history_deals_get=history, ORDER_TYPE_BUY=0,
                )
                watermark = Path(directory) / "watermark.json"
                ReconciliationWatermarkStore(watermark).save(now - timedelta(hours=1))
                reconciliation = TradeReconciliation(
                    ManualBroker(now), logger, State(), None,
                    types.SimpleNamespace(
                        SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA",
                    ),
                    watermark_path=watermark,
                )

                reconciliation.reconcile()

                trade = logger.get_trade_by_ticket("99")
                self.assertEqual(trade["status"], "CLOSED")
                self.assertEqual(trade["close_reason"], "MANUAL_CLOSE")
                self.assertEqual(trade["profit"], 50.0)
                self.assertEqual(reconciliation.health_snapshot()["status"], "SYNCED")
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_missing_entry_metadata_is_durable_and_pins_watermark(self):
        original_mt5 = reconciliation_module.mt5
        try:
            with tempfile.TemporaryDirectory() as directory:
                now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
                logger = TradeLogger("hub_demo", store=TradeStore(Path(directory) / "ledger.sqlite3"), strategy_id="alpha", strategy_name="ALPHA")
                close = Deal(601, 100, 1, now, price=1.2, volume=1, profit=-5)
                reconciliation_module.mt5 = types.SimpleNamespace(
                    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2,
                    history_deals_get=lambda _start, _end: [close], ORDER_TYPE_BUY=0,
                )
                watermark = Path(directory) / "watermark.json"
                reconciliation = TradeReconciliation(ClockBroker(now), logger, State(), None, types.SimpleNamespace(SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA"), watermark_path=watermark)
                reconciliation.reconcile()
                self.assertFalse(watermark.exists())
                self.assertEqual(reconciliation.health_snapshot()["status"], "DEGRADED")
                self.assertEqual(logger.reconciliation_issues()[0]["reason"], "ENTRY_METADATA_UNAVAILABLE")
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_recorded_out_deal_clears_stale_close_issues_without_broker_replay(self):
        original_mt5 = reconciliation_module.mt5
        try:
            with tempfile.TemporaryDirectory() as directory:
                now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
                store = TradeStore(Path(directory) / "ledger.sqlite3")
                logger = TradeLogger(
                    "hub_demo", store=store, strategy_id="alpha", strategy_name="ALPHA",
                )
                trade_id = logger.record_trade_open(
                    "99", 7, "ALPHA", "EURUSD", "BUY", now - timedelta(days=2),
                    1, 1, 0, 1, 0.9, 1.1, 0.1, 0.1, 1, 20, 1000,
                )
                logger.record_trade_close(
                    trade_id, now - timedelta(minutes=1), 1.2, "SL", -20,
                    None, None, 1, deal_id="501", volume=1,
                )
                logger.record_reconciliation_issue(
                    "__parity__", "BROKER_LEDGER_PARITY_MISMATCH", ["501"],
                )
                logger.record_reconciliation_issue(
                    "99", "ENTRY_METADATA_UNAVAILABLE", ["501"],
                )
                reconciliation_module.mt5 = types.SimpleNamespace(
                    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2,
                    DEAL_ENTRY_INOUT=3, history_deals_get=lambda _start, _end: [],
                    ORDER_TYPE_BUY=0,
                )
                reconciliation = TradeReconciliation(
                    ClockBroker(now), logger, State(), None,
                    types.SimpleNamespace(SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA"),
                    watermark_path=Path(directory) / "watermark.json",
                )

                reconciliation.reconcile()

                self.assertEqual(logger.reconciliation_issues(), [])
                self.assertEqual(reconciliation.health_snapshot()["status"], "SYNCED")
                self.assertTrue((Path(directory) / "watermark.json").exists())
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_degraded_alert_names_unresolved_reason_and_position(self):
        class Alerts:
            def __init__(self):
                self.messages = []

            def send_throttled_warning(self, **kwargs):
                self.messages.append(kwargs["message"])

        with tempfile.TemporaryDirectory() as directory:
            logger = TradeLogger(
                "hub_demo", store=TradeStore(Path(directory) / "ledger.sqlite3"),
                strategy_id="alpha", strategy_name="ALPHA",
            )
            logger.record_reconciliation_issue(
                "100", "ENTRY_METADATA_UNAVAILABLE", ["601"],
            )
            alerts = Alerts()
            reconciliation = TradeReconciliation(
                ClockBroker(datetime(2026, 7, 11, 12, tzinfo=timezone.utc)),
                logger, State(), alerts,
                types.SimpleNamespace(SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA"),
                watermark_path=Path(directory) / "watermark.json",
            )

            reconciliation._alert_degraded()

            self.assertEqual(
                alerts.messages,
                ["Trade reconciliation DEGRADED: ENTRY_METADATA_UNAVAILABLE [100]"],
            )

    def test_same_pass_resolved_issues_advance_watermark_without_warning(self):
        original_mt5 = reconciliation_module.mt5

        class Ledger:
            account_id = "hub_demo"
            strategy_id = "alpha"

            def __init__(self):
                self.issues = {}

            def get_trade_by_ticket(self, _ticket):
                return None

            def record_reconciliation_issue(self, ticket, reason, broker_deal_ids):
                self.issues[str(ticket)] = {
                    "position_id": str(ticket),
                    "reason": reason,
                    "broker_deal_ids": ";".join(broker_deal_ids),
                }

            def clear_reconciliation_issue(self, ticket):
                self.issues.pop(str(ticket), None)

            def reconciliation_issues(self):
                return list(self.issues.values())

            def ledger_deal_ids(self, *_args, **_kwargs):
                # Reproduce the bounded-window false negative seen after the
                # interrupted export; direct ID lookup below still proves the
                # deals are durable.
                return set()

            def missing_ledger_deal_ids(self, _deal_ids, **_kwargs):
                return set()

        class Alerts:
            def __init__(self):
                self.messages = []

            def send_throttled_warning(self, **kwargs):
                self.messages.append(kwargs["message"])

        try:
            with tempfile.TemporaryDirectory() as directory:
                now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
                close = Deal(701, 100, 1, now, price=1.2, volume=1, profit=-5)
                reconciliation_module.mt5 = types.SimpleNamespace(
                    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=2,
                    DEAL_ENTRY_INOUT=3, history_deals_get=lambda _start, _end: [close],
                    ORDER_TYPE_BUY=0,
                )
                alerts = Alerts()
                watermark = Path(directory) / "watermark.json"
                reconciliation = TradeReconciliation(
                    ClockBroker(now), Ledger(), State(), alerts,
                    types.SimpleNamespace(SYMBOL="EURUSD", MAGIC=7, STRATEGY_NAME="ALPHA"),
                    watermark_path=watermark,
                )

                reconciliation.reconcile()

                self.assertTrue(watermark.exists())
                self.assertEqual(reconciliation.health_snapshot()["status"], "SYNCED")
                self.assertEqual(alerts.messages, [])
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_persisted_halt_repeats_flatten_and_exposes_degraded_state(self):
        class Store:
            def read_state(self):
                return {
                    "halted": True, "halt_reason": "DD_BREACH", "peak_equity": None,
                    "starting_equity": None, "last_equity": None,
                }
        class Kill:
            triggered = False
            def __init__(self): self.calls = 0
            def trigger(self, _reason): self.triggered = True
            def flatten_all(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    is_flat=False, remaining_tickets=[42], verification_error=None,
                )
        class Alerts:
            def __init__(self): self.critical = []
            def send_critical(self, message): self.critical.append(message)
        kill, alerts = Kill(), Alerts()
        monitor = AccountMonitor("hub_demo", {}, object(), None, kill, alerts, state_store=Store())
        result = monitor.recover_persisted_halt()
        self.assertEqual(result["action"], "HALT_DEGRADED")
        self.assertEqual(monitor.status_snapshot()["halt_health"], "HALTED_DEGRADED")
        alert_count = len(alerts.critical)
        monitor.recover_persisted_halt()
        self.assertEqual(kill.calls, 2)
        self.assertEqual(len(alerts.critical), alert_count)  # alert is throttled, recovery is not


if __name__ == "__main__":
    unittest.main()
