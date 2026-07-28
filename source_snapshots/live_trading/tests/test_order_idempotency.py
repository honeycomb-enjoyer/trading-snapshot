"""P0-T05 brokerless acceptance tests for durable order intent handling."""

import sys
import sqlite3
import tempfile
import threading
import types
import unittest
from contextlib import closing
from pathlib import Path


# These tests deliberately do not import or initialize the real MT5 terminal.
if "MetaTrader5" not in sys.modules:
    mt5 = types.ModuleType("MetaTrader5")
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.TRADE_RETCODE_DONE_PARTIAL = 10010
    mt5.TRADE_RETCODE_PLACED = 10008
    sys.modules["MetaTrader5"] = mt5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.order_executor import OrderExecutor  # noqa: E402
from core.order_intent_store import OrderIntentStore  # noqa: E402


class Config:
    ACCOUNT = "hub_demo"
    STRATEGY_NAME = "TEST_STRATEGY"
    SYMBOL = "XAUUSD"
    MAGIC = 7654321
    MAX_SLIPPAGE_AS_STOP_FRACTION = 0.5
    TAKE_PROFIT_MODEL = "R_MULTIPLE"
    STOP_LOSS_MODEL = "FIXED_PRICE"


class Position:
    def __init__(self, ticket=101, volume=0.4, price_open=2000.25):
        self.ticket = ticket
        self.volume = volume
        self.price_open = price_open


class FakeBroker:
    def __init__(self, result, position=None):
        self.result = result
        self.position = position
        self.send_calls = 0
        self.refs = []
        self.requests = []

    def broker_now(self):
        return 1_700_000_000

    def get_tick(self):
        return types.SimpleNamespace(ask=2000.0, bid=1999.9)

    def send_market_order(self, **kwargs):
        self.send_calls += 1
        self.refs.append(kwargs["client_reference"])
        self.requests.append(kwargs)
        return self.result

    def find_position_for_intent(self, client_reference, magic):
        self.refs.append(client_reference)
        return self.position

    def get_spread_points(self):
        return 2.0

    def account_equity(self):
        return 10_000.0


class FakePositionManager:
    def has_position(self, magic):
        return False


class FakeRiskManager:
    def calculate_position_size(self, entry, sl):
        return {"valid": True, "lot": 0.4, "actual_risk_usd": 100.0}


class MarginBlockingRiskManager(FakeRiskManager):
    def validate_margin_for_order(self, **_kwargs):
        return {"valid": False, "reason": "MARGIN_STRESS_LIMIT"}


class FakeStateManager:
    def __init__(self):
        self.cache = {}
        self.strategy = {}

    def set_execution_cache(self, ticket, value):
        self.cache[str(ticket)] = value

    def set_strategy_value(self, key, value):
        self.strategy[key] = value


class FakeTradeLogger:
    def __init__(self):
        self.opens = []

    def record_trade_open(self, **kwargs):
        self.opens.append(kwargs)
        return "trade-1"


class FakeStrategy:
    def __init__(self):
        self.pending = 0
        self.filled = 0
        self.rejected = 0

    def mark_order_pending(self, now):
        self.pending += 1

    def register_filled_entry(self, side):
        self.filled += 1

    def register_rejected_order(self, now):
        self.rejected += 1

    def register_skipped_signal(self, side):
        pass

    def save_to_state(self, state_manager):
        pass


def signal(signal_id="sig-1"):
    return {
        "signal_id": signal_id,
        "side": "BUY",
        "expected_entry": 2000.0,
        "stop_distance": 10.0,
        "tp_distance": 20.0,
    }


class OrderIdempotencyTests(unittest.TestCase):
    def make_executor(self, directory, broker):
        logger = FakeTradeLogger()
        return OrderExecutor(
            broker=broker,
            position_manager=FakePositionManager(),
            risk_manager=FakeRiskManager(),
            state_manager=FakeStateManager(),
            trade_logger=logger,
            alerts=None,
            config=Config,
            intent_store=OrderIntentStore(Path(directory) / "order intents.sqlite3"),
        ), logger

    def test_duplicate_execute_returns_existing_filled_intent_without_second_send(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            executor, logger = self.make_executor(directory, broker)
            strategy = FakeStrategy()
            first = executor.execute_signal(signal(), strategy)
            second = executor.execute_signal(signal(), strategy)
            self.assertEqual(first.intent_id, second.intent_id)
            self.assertEqual(second.status, "FILLED")
            self.assertEqual(broker.send_calls, 1)
            self.assertEqual(len(logger.opens), 1)
            self.assertTrue(broker.requests[0]["client_reference"].startswith("oi-"))
            self.assertEqual(len(broker.requests[0]["client_reference"]), 19)

    def test_signal_without_take_profit_submits_zero_and_logs_none(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            executor, logger = self.make_executor(directory, broker)
            executor.config = types.SimpleNamespace(**{
                name: getattr(Config, name)
                for name in ("ACCOUNT", "STRATEGY_NAME", "SYMBOL", "MAGIC", "MAX_SLIPPAGE_AS_STOP_FRACTION")
            }, TAKE_PROFIT_MODEL="NONE")
            no_tp_signal = signal()
            no_tp_signal["tp_distance"] = None
            result = executor.execute_signal(no_tp_signal, FakeStrategy())
            self.assertEqual(result.status, "FILLED")
            self.assertEqual(broker.requests[0]["tp"], 0.0)
            self.assertIsNone(logger.opens[0]["initial_tp"])
            self.assertIsNone(logger.opens[0]["take_distance_points"])

    def test_custom_stop_keeps_absolute_previous_week_extreme(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            executor, logger = self.make_executor(directory, broker)
            executor.config = types.SimpleNamespace(**{
                name: getattr(Config, name)
                for name in ("ACCOUNT", "STRATEGY_NAME", "SYMBOL", "MAGIC", "MAX_SLIPPAGE_AS_STOP_FRACTION")
            }, TAKE_PROFIT_MODEL="NONE", STOP_LOSS_MODEL="CUSTOM")
            custom = signal()
            custom.update(stop_distance=20.0, stop_price=1980.0, tp_distance=None)

            result = executor.execute_signal(custom, FakeStrategy())

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(broker.requests[0]["sl"], 1980.0)
            self.assertEqual(logger.opens[0]["initial_sl"], 1980.0)

    def test_missing_take_profit_is_rejected_for_normal_model(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            executor, _ = self.make_executor(directory, broker)
            invalid = signal()
            invalid["tp_distance"] = None
            result = executor.execute_signal(invalid, FakeStrategy())
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(broker.send_calls, 0)

    def test_margin_guard_rejects_intent_before_broker_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            executor, _ = self.make_executor(directory, broker)
            executor.risk_manager = MarginBlockingRiskManager()
            strategy = FakeStrategy()

            result = executor.execute_signal(signal(), strategy)

            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(result.last_error, "MARGIN_STRESS_LIMIT")
            self.assertEqual(broker.send_calls, 0)
            self.assertEqual(strategy.rejected, 1)

    def test_lost_response_after_fill_is_unknown_then_reconciled_without_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(None, Position())
            executor, _ = self.make_executor(directory, broker)
            strategy = FakeStrategy()
            unknown = executor.execute_signal(signal(), strategy)
            self.assertEqual(unknown.status, "UNKNOWN")
            recovered = executor.execute_signal(signal(), strategy)
            self.assertEqual(recovered.status, "FILLED")
            self.assertEqual(broker.send_calls, 1)

    def test_order_send_none_without_visible_position_remains_unknown_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(None, None)
            executor, _ = self.make_executor(directory, broker)
            first = executor.execute_signal(signal(), FakeStrategy())
            second = executor.execute_signal(signal(), FakeStrategy())
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(second.status, "RECONCILING")
            self.assertEqual(broker.send_calls, 1)

    def test_delayed_position_visibility_enters_reconciling_then_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=11), None)
            executor, _ = self.make_executor(directory, broker)
            strategy = FakeStrategy()
            pending = executor.execute_signal(signal(), strategy)
            self.assertEqual(pending.status, "RECONCILING")
            broker.position = Position()
            recovered = executor.execute_signal(signal(), strategy)
            self.assertEqual(recovered.status, "FILLED")
            self.assertEqual(broker.send_calls, 1)

    def test_partial_fill_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10010, order=12), Position(volume=0.2))
            executor, logger = self.make_executor(directory, broker)
            result = executor.execute_signal(signal(), FakeStrategy())
            self.assertEqual(result.status, "PARTIALLY_FILLED")
            self.assertEqual(result.filled_volume, 0.2)
            self.assertEqual(logger.opens[0]["volume"], 0.2)

    def test_placed_order_is_accepted_then_reconciled_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10008, order=12), None)
            executor, _ = self.make_executor(directory, broker)
            outcome = executor.execute_signal(signal(), FakeStrategy())
            self.assertEqual(outcome.status, "RECONCILING")
            self.assertEqual(broker.send_calls, 1)

    def test_transient_retcode_is_recorded_rejected_and_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10004, order=0), None)
            executor, _ = self.make_executor(directory, broker)
            first = executor.execute_signal(signal(), FakeStrategy())
            second = executor.execute_signal(signal(), FakeStrategy())
            self.assertEqual(first.status, "REJECTED")
            self.assertEqual(second.status, "REJECTED")
            self.assertEqual(broker.send_calls, 1)

    def test_zero_broker_ids_are_null_and_do_not_collide_across_failed_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(
                types.SimpleNamespace(retcode=10031, order=0, deal=0), None
            )
            executor, _ = self.make_executor(directory, broker)

            first = executor.execute_signal(signal("connection-1"), FakeStrategy())
            second = executor.execute_signal(signal("connection-2"), FakeStrategy())

            self.assertEqual(first.status, "REJECTED")
            self.assertEqual(second.status, "REJECTED")
            self.assertIsNone(first.broker_order_id)
            self.assertIsNone(second.broker_order_id)
            self.assertEqual(broker.send_calls, 2)

    def test_restart_with_new_signal_id_cannot_bypass_unresolved_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(None, None)
            first_executor, _ = self.make_executor(directory, broker)
            first = first_executor.execute_signal(signal("before-restart"), FakeStrategy())
            self.assertEqual(first.status, "UNKNOWN")
            second_executor, _ = self.make_executor(directory, broker)
            after_restart = second_executor.execute_signal(signal("after-restart"), FakeStrategy())
            self.assertEqual(after_restart.intent_id, first.intent_id)
            self.assertEqual(broker.send_calls, 1)

    def test_same_hub_signal_is_isolated_after_broker_login_change(self):
        with tempfile.TemporaryDirectory() as directory:
            first_broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=10), Position())
            second_broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=11), Position())
            first_broker.login = 11111
            second_broker.login = 22222
            first_executor, _ = self.make_executor(directory, first_broker)
            second_executor, _ = self.make_executor(directory, second_broker)
            first = first_executor.execute_signal(signal("same"), FakeStrategy())
            second = second_executor.execute_signal(signal("same"), FakeStrategy())
            self.assertNotEqual(first.intent_id, second.intent_id)
            self.assertEqual(first.account_id, "hub_demo::11111")
            self.assertEqual(second.account_id, "hub_demo::22222")
            self.assertEqual(first_broker.send_calls, 1)
            self.assertEqual(second_broker.send_calls, 1)

    def test_closed_intent_is_idempotent_and_never_submits_again(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker(types.SimpleNamespace(retcode=10009, order=13), Position())
            executor, _ = self.make_executor(directory, broker)
            filled = executor.execute_signal(signal(), FakeStrategy())
            closed = executor.intent_store.transition(filled.intent_id, "CLOSED")
            repeated = executor.execute_signal(signal(), FakeStrategy())
            self.assertEqual(closed.intent_id, repeated.intent_id)
            self.assertEqual(repeated.status, "CLOSED")
            self.assertEqual(broker.send_calls, 1)

    def test_unique_signal_claim_is_safe_under_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "order intents.sqlite3"
            barrier = threading.Barrier(2)
            results = []
            failures = []

            def claim():
                try:
                    barrier.wait()
                    results.append(OrderIntentStore(db_path).claim_signal(
                        account_id="hub_demo", strategy_id="TEST_STRATEGY", signal_id="same",
                        symbol="XAUUSD", side="BUY",
                    ))
                except BaseException as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(sum(claimed for _, claimed in results), 1)
            self.assertEqual(len({intent.intent_id for intent, _ in results}), 1)

    def test_legacy_global_broker_id_schema_migrates_without_losing_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "order intents.sqlite3"
            store = OrderIntentStore(db_path)
            original, _ = store.claim_signal(
                account_id="hub_1", strategy_id="alpha", signal_id="old",
                symbol="EURUSD", side="BUY",
            )
            with closing(sqlite3.connect(db_path)) as connection:
                current_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='order_intents'"
                ).fetchone()[0]
                legacy_sql = current_sql.replace(
                    "broker_order_id TEXT", "broker_order_id TEXT UNIQUE", 1
                ).replace(
                    "broker_position_id TEXT", "broker_position_id TEXT UNIQUE", 1
                ).replace(
                    ",\n                    UNIQUE(account_id, broker_order_id)", ""
                ).replace(
                    ",\n                    UNIQUE(account_id, broker_position_id)", ""
                )
                connection.execute("ALTER TABLE order_intents RENAME TO current_scope")
                connection.execute(legacy_sql)
                connection.execute("INSERT INTO order_intents SELECT * FROM current_scope")
                connection.execute("DROP TABLE current_scope")
                connection.commit()
            migrated = OrderIntentStore(db_path)
            self.assertEqual(migrated.get("hub_1", "alpha", "old").intent_id, original.intent_id)
            with closing(sqlite3.connect(db_path)) as connection:
                migrated_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='order_intents'"
                ).fetchone()[0]
            self.assertNotIn("broker_order_id TEXT UNIQUE", migrated_sql)
            self.assertIn("UNIQUE(account_id, broker_order_id)", migrated_sql)

    def test_startup_repairs_legacy_zero_id_collision_and_stuck_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "order intents.sqlite3"
            store = OrderIntentStore(db_path)
            rejected, _ = store.claim_signal(
                account_id="hub_demo::123", strategy_id="daily", signal_id="first",
                symbol="AUDCAD", side="BUY",
            )
            store.transition(rejected.intent_id, "SUBMITTING")
            rejected = store.transition(rejected.intent_id, "REJECTED", retcode=10031)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE order_intents SET broker_order_id = '0' WHERE intent_id = ?",
                    (rejected.intent_id,),
                )
                connection.commit()

            stuck, claimed = store.claim_signal(
                account_id="hub_demo::123", strategy_id="daily", signal_id="second",
                symbol="AUDCAD", side="BUY",
            )
            self.assertTrue(claimed)
            store.transition(stuck.intent_id, "SUBMITTING")

            repaired = OrderIntentStore(db_path)
            repaired_rejected = repaired.get("hub_demo::123", "daily", "first")
            repaired_stuck = repaired.get("hub_demo::123", "daily", "second")
            self.assertIsNone(repaired_rejected.broker_order_id)
            self.assertEqual(repaired_stuck.status, "REJECTED")
            self.assertEqual(repaired_stuck.retcode, 10031)
            self.assertIn("zero broker-order", repaired_stuck.last_error)


if __name__ == "__main__":
    unittest.main()
