"""Brokerless regression tests for AccountMonitor on the P0-T03 state store."""

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.account_monitor import AccountMonitor
from risk.account_state_store import AccountStateStore


class FakeBroker:
    def __init__(self, equity=10_000.0, balance=10_000.0, positions=None):
        self.equity = equity
        self.balance = balance
        self.positions = positions or []

    def account_equity(self):
        return self.equity

    def account_balance(self):
        return self.balance

    def list_all_positions(self):
        return list(self.positions)


class FakeKillSwitch:
    def __init__(self):
        self.triggered = False
        self.flatten_calls = 0

    def trigger(self, _reason):
        self.triggered = True

    def flatten_all(self, **_kwargs):
        self.flatten_calls += 1


class FakeAlerts:
    def __init__(self):
        self.critical = []

    def send_critical(self, message):
        self.critical.append(message)

    def send_info(self, _message):
        pass


RULES = {
    "max_dd_percent": 5.0,
    "hard_dd_percent": 10.0,
    "profit_target_percent": 8.0,
    "profit_warning_percent": 6.0,
    "starting_equity": 10_000.0,
}


class AccountMonitorTests(unittest.TestCase):
    def make_monitor(self, directory, *, equity=10_000.0, account="hub_demo"):
        broker = FakeBroker(equity)
        kill_switch = FakeKillSwitch()
        monitor = AccountMonitor(
            account,
            dict(RULES),
            broker,
            object(),
            kill_switch,
            FakeAlerts(),
            state_store=AccountStateStore(
                account,
                db_path=Path(directory) / "account state.sqlite3",
                legacy_runtime_dir=directory,
            ),
        )
        return monitor, broker, kill_switch

    def test_drawdown_halts_and_flattens_once(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, broker, kill_switch = self.make_monitor(directory)
            self.assertEqual(monitor.check()["action"], "OK")
            broker.equity = 9_400.0
            result = monitor.check()
            self.assertEqual(result["type"], "DD_BREACH")
            self.assertTrue(kill_switch.triggered)
            self.assertEqual(kill_switch.flatten_calls, 1)

    def test_profit_warning_does_not_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, broker, kill_switch = self.make_monitor(directory)
            monitor.check()
            broker.equity = 10_600.0
            self.assertEqual(monitor.check()["action"], "WARN")
            self.assertFalse(kill_switch.triggered)

    def test_restart_reads_shared_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            first, broker, first_switch = self.make_monitor(directory)
            first.check()
            broker.equity = 8_000.0
            first.check()
            self.assertTrue(first_switch.triggered)

            second, _broker, second_switch = self.make_monitor(directory)
            result = second.check()
            # A persisted halt is not proof that a crashed sibling reached
            # flatten.  This old fake exposes no verification result, so the
            # recovery remains explicitly degraded and retries flatten.
            self.assertEqual(result["action"], "HALT_DEGRADED")
            self.assertTrue(second_switch.triggered)
            self.assertEqual(second_switch.flatten_calls, 1)

    def test_unreadable_database_blocks_new_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "broken.sqlite3"
            db_path.write_bytes(b"not a sqlite database")
            kill_switch = FakeKillSwitch()
            monitor = AccountMonitor(
                "hub_demo",
                dict(RULES),
                FakeBroker(),
                object(),
                kill_switch,
                FakeAlerts(),
                state_store=AccountStateStore("hub_demo", db_path=db_path),
            )
            result = monitor.check()
            self.assertEqual(result["type"], "STATE_STORE_UNAVAILABLE")
            self.assertTrue(kill_switch.triggered)


if __name__ == "__main__":
    unittest.main()
