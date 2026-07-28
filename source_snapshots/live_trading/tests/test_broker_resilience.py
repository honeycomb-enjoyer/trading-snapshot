"""P0-T07 broker resilience checks; all broker calls are local fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import stat
import sys
import tempfile
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The desktop test runtime has no MT5 package. The module is replaced with a
# behavioral fake below before every test, so this only satisfies import time.
if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = types.SimpleNamespace(
        TRADE_RETCODE_DONE=10009,
        TRADE_RETCODE_DONE_PARTIAL=10010,
        TRADE_RETCODE_PLACED=10008,
    )

from core import broker as broker_module
from core.broker import Broker, ConnectionState
from core.retry_policy import RetryPolicy
from analytics import trade_reconciliation as reconciliation_module
from analytics.trade_reconciliation import ReconciliationWatermarkStore, TradeReconciliation


class FakeMT5:
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008
    ORDER_FILLING_IOC = 0
    ORDER_FILLING_FOK = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2

    def __init__(self, *, initialize=True, account_login=42, healthy=False, responses=None):
        self.initialize_result = initialize
        self.account_login = account_login
        self.healthy = healthy
        self.responses = list(responses or [])
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.order_send_calls = 0
        self.last_request = None
        self.open_position = types.SimpleNamespace(ticket=17, volume=0.1, sl=0.0)
        self.history_orders = []
        self.history_deals = []

    def initialize(self, **kwargs):
        self.initialize_calls += 1
        return self.initialize_result

    def login(self, **kwargs):
        return True

    def shutdown(self):
        self.shutdown_calls += 1

    def terminal_info(self):
        return types.SimpleNamespace() if self.healthy else None

    def account_info(self):
        return types.SimpleNamespace(login=self.account_login) if self.healthy or self.initialize_result else None

    def last_error(self):
        return (1, "unavailable")

    def order_send(self, request):
        self.order_send_calls += 1
        self.last_request = request
        return self.responses.pop(0) if self.responses else None

    def symbol_info_tick(self, symbol):
        return types.SimpleNamespace(ask=100.0, bid=99.0)

    def symbol_info(self, symbol):
        return types.SimpleNamespace(digits=2, visible=True)

    def positions_get(self, **kwargs):
        return [self.open_position] if "ticket" in kwargs else []

    def history_orders_get(self, start, end):
        return self.history_orders

    def history_deals_get(self, start, end):
        return self.history_deals


class BrokerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.original_mt5 = broker_module.mt5

    def tearDown(self):
        broker_module.mt5 = self.original_mt5

    @staticmethod
    def policy(**changes):
        values = dict(
            reconnect_attempts=3,
            reconnect_initial_backoff_sec=2.0,
            reconnect_backoff_multiplier=2.0,
            reconnect_max_backoff_sec=10.0,
            reconnect_circuit_cooldown_sec=30.0,
            operation_attempts=3,
            operation_backoff_sec=0.0,
            history_attempts=1,
            history_backoff_sec=0.0,
            position_visibility_poll_sec=0.0,
        )
        values.update(changes)
        return RetryPolicy(**values)

    def test_terminal_unavailable_starts_disconnected_and_blocks_open_submit(self):
        fake = FakeMT5(initialize=False)
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        self.assertFalse(broker.connect())
        self.assertEqual(broker.connection_state, ConnectionState.DISCONNECTED)
        self.assertFalse(broker.can_submit_new_orders())
        self.assertIsNone(broker.send_market_order("BUY", 0.1, 99, 101, 7, client_reference="ref"))
        self.assertEqual(fake.order_send_calls, 0)

    def test_account_mismatch_is_rejected(self):
        fake = FakeMT5(initialize=True, account_login=999, healthy=True)
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        self.assertFalse(broker.connect())
        self.assertEqual(broker.connection_state, ConnectionState.DISCONNECTED)

    def test_reconnect_succeeds_on_configured_third_attempt_without_sleeping(self):
        fake = FakeMT5(initialize=False)
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        outcomes = iter((False, False, True))
        broker._initialize_terminal = lambda: next(outcomes)
        self.assertFalse(broker.ensure_connection(now=0.0))
        self.assertEqual(broker._next_reconnect_at, 2.0)
        self.assertFalse(broker.ensure_connection(now=1.0))  # scheduled, no attempt
        self.assertFalse(broker.ensure_connection(now=2.0))
        self.assertTrue(broker.ensure_connection(now=6.0))
        self.assertEqual(broker.connection_state, ConnectionState.CONNECTED)

    def test_reconnect_exhaustion_opens_circuit_and_blocks_new_orders(self):
        fake = FakeMT5(initialize=False)
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy(reconnect_attempts=2))
        self.assertFalse(broker.ensure_connection(now=0.0))
        self.assertFalse(broker.ensure_connection(now=2.0))
        self.assertEqual(broker.connection_state, ConnectionState.CIRCUIT_OPEN)
        self.assertFalse(broker.can_submit_new_orders())
        self.assertEqual(fake.shutdown_calls, 2)

    def test_permanent_close_retcode_is_not_retried(self):
        fake = FakeMT5(responses=[types.SimpleNamespace(retcode=10016)])
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        result = broker._send_close_with_retry({"volume": 0.1}, "SELL", 17)
        self.assertEqual(result.retcode, 10016)
        self.assertEqual(fake.order_send_calls, 1)

    def test_transient_close_retcode_retries_with_configured_limit(self):
        fake = FakeMT5(responses=[types.SimpleNamespace(retcode=10031), types.SimpleNamespace(retcode=10009)])
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        result = broker._send_close_with_retry({"volume": 0.1}, "SELL", 17)
        self.assertEqual(result.retcode, 10009)
        self.assertEqual(fake.order_send_calls, 2)

    def test_open_submit_has_no_retry_after_unknown_response(self):
        fake = FakeMT5(healthy=True, responses=[None])
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        broker.connection_state = ConnectionState.CONNECTED
        broker.detect_filling_mode = lambda: fake.ORDER_FILLING_IOC
        self.assertIsNone(broker.send_market_order("BUY", 0.1, 99, 101, 7, client_reference="ref"))
        self.assertEqual(fake.order_send_calls, 1)

    def test_disconnected_trade_server_blocks_open_submit(self):
        fake = FakeMT5(healthy=True)
        fake.terminal_info = lambda: types.SimpleNamespace(connected=False)
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        broker.connection_state = ConnectionState.CONNECTED

        self.assertFalse(broker.can_submit_new_orders())
        self.assertIsNone(broker.send_market_order("BUY", 0.1, 99, 101, 7, client_reference="ref"))
        self.assertEqual(fake.order_send_calls, 0)

    def test_connection_retcode_forces_reconnect_before_next_open(self):
        fake = FakeMT5(healthy=True, responses=[types.SimpleNamespace(retcode=10031)])
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        broker.connection_state = ConnectionState.CONNECTED
        broker.detect_filling_mode = lambda: fake.ORDER_FILLING_IOC

        result = broker.send_market_order("BUY", 0.1, 99, 101, 7, client_reference="ref")

        self.assertEqual(result.retcode, 10031)
        self.assertEqual(broker.connection_state, ConnectionState.DISCONNECTED)
        self.assertTrue(broker._reconnect_required)
        self.assertFalse(broker.can_submit_new_orders())
        self.assertTrue(broker.ensure_connection(now=broker._next_reconnect_at))
        self.assertFalse(broker._reconnect_required)
        self.assertEqual(fake.order_send_calls, 1)

    def test_durable_reference_is_sent_without_strategy_prefix(self):
        fake = FakeMT5(healthy=True, responses=[types.SimpleNamespace(retcode=10009)])
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        broker.connection_state = ConnectionState.CONNECTED
        broker.detect_filling_mode = lambda: fake.ORDER_FILLING_IOC
        reference = "oi-1234567890abcdef"
        broker.send_market_order(
            "BUY", 0.1, 99, 101, 7,
            comment="VERY_LONG_STRATEGY_NAME", client_reference=reference,
        )
        self.assertEqual(fake.last_request["comment"], reference)

    def test_unique_magic_fallback_is_not_critical(self):
        class Alerts:
            def __init__(self):
                self.info = []
                self.critical = []

            def send_info(self, message):
                self.info.append(message)

            def send_critical(self, message):
                self.critical.append(message)

        fake = FakeMT5()
        fake.open_position = types.SimpleNamespace(
            ticket=17, volume=0.1, sl=99.0, magic=7, comment="broker-rewritten"
        )
        fake.positions_get = lambda **_kwargs: [fake.open_position]
        broker_module.mt5 = fake
        alerts = Alerts()
        broker = Broker("XAUUSD", login=42, alerts=alerts, retry_policy=self.policy())
        position = broker.find_position_for_intent("oi-1234567890abcdef", 7)
        self.assertEqual(position.ticket, 17)
        self.assertEqual(alerts.critical, [])
        self.assertIn("unique-magic fallback", alerts.info[0])

    def test_exact_client_reference_history_lookup_never_guesses_by_magic(self):
        fake = FakeMT5()
        fake.history_orders = [
            types.SimpleNamespace(ticket=71, position_id=81, magic=7, comment="strategy|ref-1", time_done=1)
        ]
        fake.history_deals = [
            types.SimpleNamespace(ticket=91, order=71, position_id=81, magic=7, comment="strategy|ref-1", time=2, volume=0.1)
        ]
        broker_module.mt5 = fake
        broker = Broker("XAUUSD", login=42, retry_policy=self.policy())
        broker.clock.offset_seconds = 0
        match = broker.find_execution_for_intent("ref-1", 7)
        self.assertEqual(match.source, "history")
        self.assertEqual((match.order_id, match.position_id, match.deal_id), (71, 81, 91))
        self.assertIsNone(broker.find_execution_for_intent("missing", 7))

    def test_reconciliation_watermark_survives_restart_and_replays_overlap(self):
        class Ledger:
            account_id = "hub_demo"
            strategy_id = "test_strategy"

            @staticmethod
            def reconciliation_issues():
                return []

        class State:
            state = {"execution_cache": {}}

        class BrokerClock:
            def __init__(self, current):
                self.current = current

            def broker_now(self):
                return self.current

            def decode_close_reason(self, value):
                return "EXTERNAL_CLOSE"

        history_calls = []
        original_mt5 = reconciliation_module.mt5
        reconciliation_module.mt5 = types.SimpleNamespace(
            history_deals_get=lambda start, end: history_calls.append((start, end)) or []
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = types.SimpleNamespace(SYMBOL="XAUUSD", MAGIC=7, STRATEGY_NAME="test_strategy")
                first_time = datetime(2026, 7, 10, 12, 0, 0)
                path = os.path.join(directory, "watermark.json")
                first = TradeReconciliation(BrokerClock(first_time), Ledger(), State(), None, config,
                                            bootstrap_days=10, overlap_sec=120, watermark_path=path)
                self.assertEqual(first.reconcile(), 0)
                self.assertEqual(history_calls[-1][0], first_time.replace(tzinfo=timezone.utc) - timedelta(days=10))

                second_time = first_time + timedelta(days=5)
                second = TradeReconciliation(BrokerClock(second_time), Ledger(), State(), None, config,
                                             bootstrap_days=10, overlap_sec=120, watermark_path=path)
                second.reconcile()
                self.assertEqual(history_calls[-1][0], first_time.replace(tzinfo=timezone.utc) - timedelta(seconds=120))
        finally:
            reconciliation_module.mt5 = original_mt5

    def test_watermark_save_repairs_readonly_windows_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watermark.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"watermark_utc":"2026-07-01T00:00:00+00:00"}')
            os.chmod(path, stat.S_IREAD)
            original_replace = os.replace

            def windows_like_replace(source, destination):
                if not os.stat(destination).st_mode & stat.S_IWRITE:
                    raise PermissionError(13, "destination is read-only", destination)
                return original_replace(source, destination)

            reconciliation_module.os.replace = windows_like_replace
            try:
                store = ReconciliationWatermarkStore(
                    path, replace_attempts=2, replace_backoff_sec=0,
                )
                expected = datetime(2026, 7, 13, 8, 15, tzinfo=timezone.utc)
                store.save(expected)
                self.assertEqual(store.load(), expected)
                self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(directory)))
            finally:
                reconciliation_module.os.replace = original_replace
                if os.path.exists(path):
                    os.chmod(path, stat.S_IWRITE)

    def test_watermark_replace_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watermark.json")
            original_replace = os.replace
            attempts = []

            def transient_replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) < 3:
                    raise PermissionError(13, "temporary sharing lock", destination)
                return original_replace(source, destination)

            reconciliation_module.os.replace = transient_replace
            try:
                store = ReconciliationWatermarkStore(
                    path, replace_attempts=3, replace_backoff_sec=0,
                )
                expected = datetime(2026, 7, 13, 8, 20, tzinfo=timezone.utc)
                store.save(expected)
                self.assertEqual(store.load(), expected)
                self.assertEqual(len(attempts), 3)
            finally:
                reconciliation_module.os.replace = original_replace

    def test_runner_has_no_long_literal_sleep_in_control_path(self):
        with open(os.path.join(ROOT, "run_bot.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("time.sleep(1)", source)
        self.assertNotIn("time.sleep(0.2)", source)


if __name__ == "__main__":
    unittest.main()
