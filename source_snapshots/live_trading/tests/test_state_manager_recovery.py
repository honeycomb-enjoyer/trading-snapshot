"""P0-T04 recovery and migration tests (no broker or live secrets)."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state_manager import StateManager
from core.state_schema import CURRENT_SCHEMA_VERSION, StateLoadStatus, StateRecoveryCode


class StateManagerRecoveryTests(unittest.TestCase):
    def manager(self, directory):
        return StateManager("state_test", runtime_dir=directory)

    def test_missing_state_creates_current_versioned_default(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            result = manager.load()
            self.assertEqual(result.status, StateLoadStatus.CREATED)
            self.assertTrue(result.ready)
            self.assertEqual(manager.state["schema_version"], CURRENT_SCHEMA_VERSION)
            self.assertTrue(manager.state_file.exists())

    def test_valid_current_state_loads_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.load()
            original = manager.state_file.read_text(encoding="utf-8")
            reloaded = self.manager(directory)
            result = reloaded.load()
            self.assertEqual(result.status, StateLoadStatus.LOADED)
            self.assertEqual(reloaded.state_file.read_text(encoding="utf-8"), original)

    def test_old_state_migrates_and_missing_nested_defaults_are_added(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.state_file.write_text(
                json.dumps(
                    {
                        "last_h1_bar_time": "2026-07-10T10:00:00Z",
                        "pending_order": {"active": True},
                        "last_long_signal_bar": "2026-07-10T09:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            result = manager.load()
            self.assertEqual(result.status, StateLoadStatus.MIGRATED)
            self.assertEqual(manager.state["schema_version"], CURRENT_SCHEMA_VERSION)
            self.assertTrue(manager.state["engine"]["pending_order"]["active"])
            self.assertIsNone(manager.state["engine"]["pending_order"]["side"])
            self.assertIn("retry_after", manager.state["engine"]["pending_order"])
            self.assertFalse(manager.state["strategy"]["breakeven_done"])

    def test_version_one_state_migrates_without_losing_extension_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.state_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "engine": {},
                        "strategy": {"strategy_extension": "preserved"},
                        "execution_cache": {},
                    }
                ),
                encoding="utf-8",
            )
            result = manager.load()
            self.assertEqual(result.status, StateLoadStatus.MIGRATED)
            self.assertEqual(manager.state["schema_version"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(manager.state["strategy"]["strategy_extension"], "preserved")

    def test_malformed_and_truncated_json_require_recovery_and_quarantine(self):
        for payload in ("{not json", '{"engine":'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                manager = self.manager(directory)
                manager.state_file.write_text(payload, encoding="utf-8")
                result = manager.load()
                self.assertEqual(result.status, StateLoadStatus.RECOVERY_REQUIRED)
                self.assertFalse(result.ready)
                self.assertIsNone(manager.state)
                self.assertEqual(manager.state_file.read_text(encoding="utf-8"), payload)
                self.assertTrue(Path(result.quarantine_path).exists())

    def test_invalid_nested_object_requires_recovery_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            payload = json.dumps({"schema_version": CURRENT_SCHEMA_VERSION, "engine": []})
            manager.state_file.write_text(payload, encoding="utf-8")
            result = manager.load()
            self.assertEqual(result.status, StateLoadStatus.RECOVERY_REQUIRED)
            self.assertEqual(manager.state_file.read_text(encoding="utf-8"), payload)

    def test_recovery_gate_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.state_file.write_text("{broken", encoding="utf-8")
            self.assertFalse(manager.load().ready)
            restarted = self.manager(directory)
            self.assertFalse(restarted.load().ready)
            self.assertIsNone(restarted.state)

    def test_atomic_write_interruption_keeps_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.load()
            original = manager.state_file.read_text(encoding="utf-8")
            manager.state["strategy"]["last_long_signal_bar"] = "new"
            with patch("core.state_manager.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    manager.save()
            self.assertEqual(manager.state_file.read_text(encoding="utf-8"), original)

    def test_save_skips_unchanged_state(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.load()
            with patch("core.state_manager.os.replace") as replace:
                self.assertFalse(manager.save())
            replace.assert_not_called()

    def test_runner_halts_before_broker_when_recovery_is_required(self):
        import run_bot

        captured = {"kill_reason": None}

        class FakeStateManager:
            def __init__(self, *args, **kwargs):
                pass

            def load(self):
                error = types.SimpleNamespace(code=StateRecoveryCode.MALFORMED_JSON)
                return types.SimpleNamespace(ready=False, error=error)

        class FakeKillSwitch:
            def __init__(self, *args, **kwargs):
                pass

            def trigger(self, reason):
                captured["kill_reason"] = reason

        class FakeAlerts:
            def __init__(self, *args, **kwargs):
                pass

            def send_info(self, *args, **kwargs):
                pass

        def broker_must_not_be_constructed(*args, **kwargs):
            raise AssertionError("broker construction must follow the state recovery gate")

        def module(name, **attributes):
            fake = types.ModuleType(name)
            for attribute, value in attributes.items():
                setattr(fake, attribute, value)
            return fake

        fake_modules = {
            "secret_config": module(
                "secret_config",
                TELEGRAM_ENABLED=False,
                TELEGRAM_BOT_TOKEN="",
                MAIN_CHAT_ID="0",
                CHAT_IDS={"audcad_h4_reversion": "0"},
                ACCOUNTS={},
            ),
            "analytics.trade_logger": module("analytics.trade_logger", TradeLogger=object),
            "analytics.trade_reconciliation": module(
                "analytics.trade_reconciliation", TradeReconciliation=object
            ),
            "core.broker": module("core.broker", Broker=broker_must_not_be_constructed),
            "core.data_feed": module("core.data_feed", DataFeed=object),
            "core.order_executor": module("core.order_executor", OrderExecutor=object),
            "core.position_manager": module("core.position_manager", PositionManager=object),
            "core.state_manager": module("core.state_manager", StateManager=FakeStateManager),
            "guards.execution_guard": module("guards.execution_guard", ExecutionGuard=object),
            "guards.kill_switch": module("guards.kill_switch", KillSwitch=FakeKillSwitch),
            "guards.market_guard": module("guards.market_guard", MarketGuard=object),
            "guards.recovery_guard": module("guards.recovery_guard", RecoveryGuard=object),
            "guards.session_guard": module("guards.session_guard", SessionGuard=object),
            "monitoring.alerts": module("monitoring.alerts", Alerts=FakeAlerts),
            "monitoring.heartbeat": module("monitoring.heartbeat", Heartbeat=object),
            "risk.account_monitor": module("risk.account_monitor", AccountMonitor=object),
            "risk.risk_manager": module("risk.risk_manager", RiskManager=object),
        }
        config = types.SimpleNamespace(STRATEGY_NAME="state_test")
        validation = types.SimpleNamespace(
            strategy_metadata={"audcad_h4_reversion": {"enabled": True}}
        )
        with patch.object(run_bot, "validate_configuration", return_value=validation), \
                patch.object(run_bot, "load_strategy", return_value=(config, object)), \
                patch.dict(sys.modules, fake_modules):
            run_bot.main("audcad_h4_reversion")

        self.assertEqual(
            captured["kill_reason"], "STATE_RECOVERY_REQUIRED (malformed_json)"
        )


if __name__ == "__main__":
    unittest.main()
