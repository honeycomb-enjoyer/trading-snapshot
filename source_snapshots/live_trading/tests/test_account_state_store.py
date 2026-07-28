"""P0-T03 acceptance tests for transactional account state."""

import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.account_state_store import AccountStateStore, AccountStateStoreError


class AccountStateStoreTests(unittest.TestCase):
    def make_store(self, directory, account="hub_demo", **kwargs):
        return AccountStateStore(
            account,
            db_path=Path(directory) / "account state.sqlite3",
            legacy_runtime_dir=directory,
            **kwargs,
        )

    def test_concurrent_peak_updates_never_decrease(self):
        with tempfile.TemporaryDirectory() as directory:
            values = [10_000.0 + index * 13.0 for index in range(40)]
            random.Random(7).shuffle(values)
            barrier = threading.Barrier(len(values))
            failures = []

            def writer(value):
                try:
                    barrier.wait()
                    self.make_store(directory).record_equity(value, 10_000.0)
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=writer, args=(value,)) for value in values]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(
                self.make_store(directory).read_state()["peak_equity"], max(values)
            )

    def test_property_peak_is_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            previous_peak = 0.0
            for _ in range(200):
                state = store.record_equity(random.uniform(1.0, 50_000.0), 10_000.0)
                self.assertGreaterEqual(state["peak_equity"], previous_peak)
                previous_peak = state["peak_equity"]

    def test_concurrent_halt_is_sticky(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record_equity(10_000.0, 10_000.0)
            barrier = threading.Barrier(2)
            failures = []

            def halt_writer():
                try:
                    barrier.wait()
                    store.halt("DD_BREACH")
                except BaseException as error:
                    failures.append(error)

            def equity_writer():
                try:
                    barrier.wait()
                    store.record_equity(11_000.0, 10_000.0)
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=halt_writer), threading.Thread(target=equity_writer)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            state = store.read_state()
            self.assertTrue(state["halted"])
            state, newly_halted = self.make_store(directory).halt("PROFIT_TARGET")
            self.assertFalse(newly_halted)
            self.assertTrue(state["halted"])
            self.assertEqual(state["halt_reason"], "DD_BREACH")

    def test_two_accounts_are_isolated_and_restart_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_store(directory, "hub_1").record_equity(10_000.0, 9_000.0)
            self.make_store(directory, "hub_2").record_equity(20_000.0, 19_000.0)
            self.make_store(directory, "hub_1").halt("DD_BREACH")
            hub_1 = self.make_store(directory, "hub_1").read_state()
            hub_2 = self.make_store(directory, "hub_2").read_state()
            self.assertTrue(hub_1["halted"])
            self.assertFalse(hub_2["halted"])
            self.assertEqual(hub_1["peak_equity"], 10_000.0)
            self.assertEqual(hub_2["peak_equity"], 20_000.0)

    def test_legacy_json_is_backed_up_migrated_once_and_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "account_state_hub_demo.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "starting_equity": 9_000.0,
                        "peak_equity": 12_000.0,
                        "last_equity": 11_000.0,
                        "halted": False,
                        "halt_reason": None,
                        "last_breach": {"type": "old", "ts": 123.0},
                        "updated_at": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            store = self.make_store(directory)
            state = store.read_state()
            self.assertEqual(state["peak_equity"], 12_000.0)
            self.assertEqual(state["last_breach_at"], 123.0)
            self.assertTrue(legacy_path.exists())
            self.assertTrue(legacy_path.with_suffix(".json.bak").exists())
            legacy_path.write_text("{}", encoding="utf-8")
            self.assertEqual(self.make_store(directory).read_state()["peak_equity"], 12_000.0)

    def test_malformed_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "account state.sqlite3"
            db_path.write_bytes(b"not a database")
            with self.assertRaises(AccountStateStoreError):
                self.make_store(directory).read_state()

    def test_malformed_legacy_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "account_state_hub_demo.json").write_text(
                "{not valid json", encoding="utf-8"
            )
            with self.assertRaises(AccountStateStoreError):
                self.make_store(directory).read_state()

    def test_locked_database_retries_and_windows_compatible_path(self):
        with tempfile.TemporaryDirectory(prefix="state store ") as directory:
            store = self.make_store(
                directory,
                busy_timeout_ms=10,
                lock_retry_attempts=5,
                lock_retry_base_sec=0.02,
            )
            store.record_equity(10_000.0, 10_000.0)
            lock = sqlite3.connect(
                store.db_path, isolation_level=None, check_same_thread=False
            )
            lock.execute("BEGIN IMMEDIATE")
            releaser = threading.Thread(
                target=lambda: (time.sleep(0.06), lock.execute("COMMIT"), lock.close())
            )
            releaser.start()
            self.assertEqual(store.record_equity(10_100.0, 10_000.0)["peak_equity"], 10_100.0)
            releaser.join()


if __name__ == "__main__":
    unittest.main()
