"""P0-T03A tests for controlled, audited hub risk-state reset."""

from __future__ import annotations

import tempfile
import threading
import unittest
import sqlite3
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.account_monitor import AccountMonitor
from risk.account_state_store import AccountStateStore
from accounts import consume_startup_reset_flag, startup_reset_token


class FakeBroker:
    def __init__(self, *, equity=10_000.0, balance=10_000.0, positions=None, fail_positions=False):
        self.equity = equity
        self.balance = balance
        self.positions = list(positions or [])
        self.fail_positions = fail_positions

    def account_equity(self):
        return self.equity

    def account_balance(self):
        return self.balance

    def list_all_positions(self):
        if self.fail_positions:
            raise RuntimeError("broker query failed")
        return list(self.positions)


class FakeKillSwitch:
    def __init__(self):
        self.triggered = False
        self.reasons = []

    def trigger(self, reason):
        self.triggered = True
        self.reasons.append(reason)

    def flatten_all(self, **_kwargs):
        pass


class FakeAlerts:
    def __init__(self):
        self.critical = []
        self.info = []

    def send_critical(self, message):
        self.critical.append(message)

    def send_info(self, message):
        self.info.append(message)


def make_store(directory: str, account="hub_demo") -> AccountStateStore:
    return AccountStateStore(
        account,
        db_path=Path(directory) / "account-state.sqlite3",
        legacy_runtime_dir=directory,
    )


class AccountStateResetStoreTests(unittest.TestCase):
    def test_reset_clears_halt_audits_once_and_duplicate_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            store.record_equity(10_000.0, 10_000.0)
            store.record_equity(10_800.0, 10_000.0)
            store.halt("DD_BREACH")
            reset_id = "request-one"

            reset, performed = store.reset_if_new(
                reset_id,
                "new account phase",
                9_900.0,
                configured_starting_equity=9_500.0,
            )
            self.assertTrue(performed)
            self.assertEqual(reset["starting_equity"], 9_500.0)
            self.assertEqual(reset["peak_equity"], 9_900.0)
            self.assertEqual(reset["last_equity"], 9_900.0)
            self.assertFalse(reset["halted"])
            self.assertIsNone(reset["halt_reason"])
            self.assertEqual(reset["last_reset_id"], reset_id)

            duplicate, performed = store.reset_if_new(
                reset_id,
                "must not overwrite the first audit reason",
                12_000.0,
                configured_starting_equity=12_000.0,
            )
            self.assertFalse(performed)
            self.assertEqual(duplicate, reset)
            audit = store.list_reset_audit()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["reset_reason"], "new account phase")
            self.assertEqual(audit[0]["old_peak_equity"], 10_800.0)
            self.assertEqual(audit[0]["old_halt_reason"], "DD_BREACH")
            self.assertEqual(audit[0]["new_peak_equity"], 9_900.0)

    def test_balance_then_equity_are_the_starting_equity_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            state, performed = make_store(directory).reset_if_new(
                "balance-fallback", "new lifecycle", 9_750.0, broker_balance=9_500.0
            )
            self.assertTrue(performed)
            self.assertEqual(state["starting_equity"], 9_500.0)
            self.assertEqual(state["peak_equity"], 9_750.0)

            state, performed = make_store(directory, "hub_2").reset_if_new(
                "equity-fallback", "new lifecycle", 8_800.0
            )
            self.assertTrue(performed)
            self.assertEqual(state["starting_equity"], 8_800.0)

    def test_reset_clears_profit_target_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            store.record_equity(10_000.0, 10_000.0)
            store.halt("PROFIT_TARGET")
            state, performed = store.reset_if_new(
                "profit-reset", "new lifecycle", 10_800.0
            )
            self.assertTrue(performed)
            self.assertFalse(state["halted"])
            self.assertIsNone(state["halt_reason"])

    def test_concurrent_same_reset_id_writes_one_audit_event(self):
        with tempfile.TemporaryDirectory() as directory:
            reset_id = "concurrent-request"
            barrier = threading.Barrier(2)
            outcomes = []

            def reset():
                barrier.wait()
                outcomes.append(
                    make_store(directory).reset_if_new(reset_id, "phase change", 10_000.0)[1]
                )

            threads = [threading.Thread(target=reset), threading.Thread(target=reset)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(outcomes), [False, True])
            self.assertEqual(len(make_store(directory).list_reset_audit()), 1)

    def test_existing_p0_t03_database_migrates_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "account-state.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE account_state (
                    account_id TEXT PRIMARY KEY,
                    starting_equity REAL,
                    peak_equity REAL,
                    last_equity REAL,
                    halted INTEGER NOT NULL DEFAULT 0,
                    halt_reason TEXT,
                    last_breach_at REAL,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                INSERT INTO account_state VALUES
                    ('hub_demo', 10000, 11000, 10500, 1, 'DD_BREACH', 1, 7, 2);
                """
            )
            connection.close()
            store = make_store(directory)
            self.assertEqual(store.read_state()["last_reset_id"], None)
            state, performed = store.reset_if_new(
                "migration-request", "migration reset", 9_500.0
            )
            self.assertTrue(performed)
            self.assertFalse(state["halted"])
            self.assertEqual(len(store.list_reset_audit()), 1)


class AccountMonitorStartupResetTests(unittest.TestCase):
    def make_monitor(self, directory: str, rules: dict, broker: FakeBroker):
        kill_switch = FakeKillSwitch()
        alerts = FakeAlerts()
        consumed = []
        monitor = AccountMonitor(
            "hub_demo",
            rules,
            broker,
            object(),
            kill_switch,
            alerts,
            state_store=make_store(directory),
            reset_flag_consumer=lambda account, token: consumed.append((account, token)) or True,
        )
        return monitor, kill_switch, alerts, consumed

    def test_false_keeps_persisted_halt_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            store.record_equity(10_000.0, 10_000.0)
            store.halt("PROFIT_TARGET")
            monitor, kill_switch, _alerts, _consumed = self.make_monitor(
                directory,
                {"reset_state_on_startup": False, "starting_equity": 20_000.0},
                FakeBroker(),
            )
            self.assertTrue(monitor.perform_startup_reset())
            state = store.read_state()
            self.assertTrue(state["halted"])
            self.assertEqual(state["starting_equity"], 10_000.0)
            self.assertFalse(kill_switch.triggered)

    def test_true_resets_without_extra_command_and_open_positions_block_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, kill_switch, _alerts, consumed = self.make_monitor(
                directory, {"reset_state_on_startup": True}, FakeBroker()
            )
            self.assertTrue(monitor.perform_startup_reset())
            self.assertFalse(kill_switch.triggered)
            self.assertEqual(len(consumed), 1)
            self.assertFalse(monitor.risk_rules["reset_state_on_startup"])

        with tempfile.TemporaryDirectory() as directory:
            monitor, kill_switch, _alerts, _consumed = self.make_monitor(
                directory,
                {"reset_state_on_startup": True},
                FakeBroker(positions=[object()]),
            )
            self.assertFalse(monitor.perform_startup_reset())
            self.assertTrue(kill_switch.triggered)
            self.assertEqual(make_store(directory).list_reset_audit(), [])

        with tempfile.TemporaryDirectory() as directory:
            monitor, kill_switch, _alerts, _consumed = self.make_monitor(
                directory,
                {"reset_state_on_startup": True},
                FakeBroker(fail_positions=True),
            )
            self.assertFalse(monitor.perform_startup_reset())
            self.assertTrue(kill_switch.triggered)

    def test_reset_has_no_operator_text_and_stops_if_flag_cannot_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _kill_switch, alerts, consumed = self.make_monitor(
                directory,
                {"reset_state_on_startup": True, "starting_equity": None},
                FakeBroker(equity=10_100.0, balance=10_000.0),
            )
            self.assertTrue(monitor.perform_startup_reset())
            self.assertEqual(len(consumed), 1)
            self.assertNotIn("reset_reason", "\n".join(alerts.info + alerts.critical))

        with tempfile.TemporaryDirectory() as directory:
            monitor = AccountMonitor(
                "hub_demo",
                {"reset_state_on_startup": True},
                FakeBroker(),
                object(),
                FakeKillSwitch(),
                FakeAlerts(),
                state_store=make_store(directory),
                reset_flag_consumer=lambda _account, _token: False,
            )
            self.assertFalse(monitor.perform_startup_reset())
            self.assertTrue(monitor.kill_switch.triggered)


class AccountsResetFlagTests(unittest.TestCase):
    def test_successful_reset_flag_is_consumed_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "accounts.py"
            source.write_text(
                "ACCOUNTS = {'hub_demo': {'risk_rules': {'reset_state_on_startup': True}}}\n",
                encoding="utf-8",
            )
            token = startup_reset_token("hub_demo", source_path=source)
            self.assertTrue(
                consume_startup_reset_flag("hub_demo", token, source_path=source)
            )
            self.assertIn("'reset_state_on_startup': False", source.read_text(encoding="utf-8"))
            self.assertFalse(
                consume_startup_reset_flag("hub_demo", token, source_path=source)
            )


if __name__ == "__main__":
    unittest.main()
