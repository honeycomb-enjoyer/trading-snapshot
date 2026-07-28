"""Brokerless regression tests for P0-T08 risk and market/session guards."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portfolio_config
from guards.market_guard import MarketGuard
from guards.session_guard import SessionGuard
from risk.risk_manager import RiskManager


class FakeSymbolInfo:
    point = 0.00001
    trade_tick_size = 0.00001
    trade_tick_value = 0.0003
    volume_min = 0.01
    volume_step = 0.01
    volume_max = 100.0


class FakeBroker:
    symbol = "EURUSD"

    def __init__(self, positions=None, now=None):
        self.positions = list(positions or [])
        self._now = now or datetime(2026, 7, 10, 22, 0)
        self.tick = None

    def broker_now(self):
        return self._now

    def get_tick(self):
        return self.tick

    def get_symbol_info(self):
        return FakeSymbolInfo()

    def list_all_positions(self):
        return list(self.positions)

    def estimate_profit_per_lot(self, open_price, close_price):
        return abs(open_price - close_price) * 30


class FailingPositionBroker(FakeBroker):
    def list_all_positions(self):
        raise RuntimeError("MT5 unavailable")


class FakeLogger:
    strategy_id = "test"

    def get_daily_strategy_pnl(self, *_args):
        return 0.0

    def get_weekly_strategy_pnl(self, *_args):
        return 0.0


class FakeAlerts:
    def __init__(self):
        self.warnings = []

    def send_warning(self, message):
        self.warnings.append(message)


class FakeAccountMonitor:
    def __init__(self, halted=False):
        self.halted = halted

    def status_snapshot(self):
        return {"halted": self.halted}


def strategy_config():
    return SimpleNamespace(
        STRATEGY_NAME="TEST",
        SYMBOL="EURUSD",
        RISK_BUFFER=1.0,
        ALLOW_UNDERSIZED_LOT=False,
        MAX_LOT=None,
        RISK_PER_TRADE_USD=30.0,
        DAILY_SL_LIMIT_USD=None,
        WEEKLY_SL_LIMIT_USD=None,
    )


def manager(
    *, broker=None, monitor=None, alerts=None, maximum=25,
    position_tolerance_r=0.0, slippage_tolerance_r=0.0,
    max_margin_utilization=None, margin_stress_multiplier=2.0,
    margin_estimate_buffer=1.0,
):
    config = SimpleNamespace(
        MAX_OPEN_POSITIONS=maximum,
        POSITION_RISK_VALIDATION_TOLERANCE_R=position_tolerance_r,
        POSITION_RISK_SLIPPAGE_TOLERANCE_R=slippage_tolerance_r,
        MAX_MARGIN_UTILIZATION=max_margin_utilization,
        MARGIN_STRESS_STOP_MULTIPLIER=margin_stress_multiplier,
        MARGIN_ESTIMATE_BUFFER=margin_estimate_buffer,
    )
    return RiskManager(
        broker=broker or FakeBroker(),
        strategy_config=strategy_config(),
        portfolio_config=config,
        trade_logger=FakeLogger(),
        account_monitor=monitor,
        alerts=alerts,
    )


class RiskGuardTests(unittest.TestCase):
    def test_max_open_positions_is_account_wide_and_has_reason_code(self):
        risk = manager(broker=FakeBroker(positions=[object(), object()]), maximum=2)
        allowed, reason = risk.can_open_new_trade()
        self.assertFalse(allowed)
        self.assertEqual(reason, "MAX_OPEN_POSITIONS_REACHED")

    def test_unknown_aggregate_exposure_fails_closed(self):
        risk = manager(broker=FailingPositionBroker())
        allowed, reason = risk.can_open_new_trade()
        self.assertFalse(allowed)
        self.assertEqual(reason, "POSITION_QUERY_UNAVAILABLE")

    def test_halted_account_blocks_and_routes_alert(self):
        alerts = FakeAlerts()
        risk = manager(monitor=FakeAccountMonitor(halted=True), alerts=alerts)
        allowed, reason = risk.can_open_new_trade()
        self.assertFalse(allowed)
        self.assertEqual(reason, "ACCOUNT_HALTED")
        self.assertEqual(alerts.warnings, ["[RISK GUARD] BLOCK (ACCOUNT_HALTED)"])

    def test_position_validation_checks_identity_side_protection_and_risk(self):
        risk = manager()
        valid = SimpleNamespace(
            symbol="EURUSD", type=0, price_open=100.0, sl=99.0, tp=101.0, volume=1.0,
        )
        self.assertEqual(risk.validate_open_position(valid), (True, None))
        self.assertEqual(
            risk.validate_open_position(SimpleNamespace(**{**valid.__dict__, "symbol": "XAUUSD"})),
            (False, "POSITION_SYMBOL_MISMATCH"),
        )
        self.assertEqual(
            risk.validate_open_position(SimpleNamespace(**{**valid.__dict__, "type": 99})),
            (False, "POSITION_SIDE_INVALID"),
        )
        self.assertEqual(
            risk.validate_open_position(SimpleNamespace(**{**valid.__dict__, "sl": 0.0})),
            (False, "POSITION_SL_TP_REQUIRED"),
        )
        self.assertEqual(
            risk.validate_open_position(SimpleNamespace(**{**valid.__dict__, "tp": 99.0})),
            (False, "POSITION_SL_TP_DIRECTION_INVALID"),
        )
        self.assertEqual(
            risk.validate_open_position(SimpleNamespace(**{**valid.__dict__, "volume": 2.0})),
            (False, "POSITION_RISK_EXCEEDS_LIMIT"),
        )

    def test_position_without_tp_is_valid_only_for_explicit_none_model(self):
        risk = manager()
        risk.strategy_config.TAKE_PROFIT_MODEL = "NONE"
        position = SimpleNamespace(
            symbol="EURUSD", type=0, price_open=100.0, sl=99.0, tp=0.0, volume=1.0,
        )
        self.assertEqual(risk.validate_open_position(position), (True, None))

    def test_position_risk_validation_allows_only_configured_operational_noise(self):
        position = SimpleNamespace(
            symbol="EURUSD", type=0, price_open=100.0, sl=99.0,
            tp=101.0, volume=1.04,
        )
        self.assertEqual(
            manager(position_tolerance_r=0.05).validate_open_position(position),
            (True, None),
        )
        self.assertEqual(
            manager(position_tolerance_r=0.03).validate_open_position(position),
            (False, "POSITION_RISK_EXCEEDS_LIMIT"),
        )

    def test_confirmed_entry_slippage_keeps_protected_position_managed(self):
        risk = manager(position_tolerance_r=0.05, slippage_tolerance_r=0.50)
        risk.strategy_config.RISK_PER_TRADE_USD = 25.0
        position = SimpleNamespace(
            ticket=77, symbol="EURUSD", type=0, price_open=100.0,
            sl=99.0, tp=101.0, volume=1.0,
        )
        context = {
            "requested_entry_price": 99.8333333333,
            "expected_entry_price": 99.8333333333,
            "actual_entry_price": 100.0,
            "entry_slippage": 0.1666666667,
            "initial_sl": 99.0,
            "planned_risk_usd": 25.0,
        }

        self.assertEqual(
            risk.validate_open_position(position, execution_context=context),
            (True, "POSITION_RISK_ELEVATED_BY_SLIPPAGE"),
        )
        self.assertIn("management remains active", risk.position_validation_warning(
            "POSITION_RISK_ELEVATED_BY_SLIPPAGE"
        ))

    def test_slippage_exception_rejects_widened_sl_or_excessive_risk(self):
        risk = manager(position_tolerance_r=0.05, slippage_tolerance_r=0.50)
        risk.strategy_config.RISK_PER_TRADE_USD = 25.0
        context = {
            "requested_entry_price": 99.8333333333,
            "actual_entry_price": 100.0,
            "entry_slippage": 0.1666666667,
            "initial_sl": 99.0,
            "planned_risk_usd": 25.0,
        }
        widened = SimpleNamespace(
            ticket=77, symbol="EURUSD", type=0, price_open=100.0,
            sl=98.5, tp=101.0, volume=1.0,
        )
        oversized = SimpleNamespace(
            ticket=77, symbol="EURUSD", type=0, price_open=100.0,
            sl=99.0, tp=101.0, volume=2.0,
        )

        self.assertEqual(
            risk.validate_open_position(widened, execution_context=context),
            (False, "POSITION_RISK_EXCEEDS_LIMIT"),
        )
        self.assertEqual(
            risk.validate_open_position(oversized, execution_context=context),
            (False, "POSITION_RISK_EXCEEDS_LIMIT"),
        )

    def test_margin_stress_guard_accounts_for_all_positions_and_auto_leverage_buffer(self):
        class MarginBroker(FakeBroker):
            def __init__(self, *, order_margin):
                super().__init__(positions=[SimpleNamespace(
                    symbol="EURUSD", type=0, price_open=100.0,
                    sl=99.0, volume=1.0,
                )])
                self.order_margin = order_margin

            def account_margin_snapshot(self):
                return {"balance": 1000.0, "equity": 1000.0, "margin": 100.0}

            def estimate_order_margin(self, *_args):
                return self.order_margin

            def estimate_order_profit(self, _side, _symbol, volume, open_price, close_price):
                return (close_price - open_price) * 30.0 * volume

        safe = manager(
            broker=MarginBroker(order_margin=100.0), max_margin_utilization=0.50,
            margin_stress_multiplier=2.0, margin_estimate_buffer=1.25,
        ).validate_margin_for_order(side="BUY", volume=1.0, entry=100.0, sl=99.0)
        blocked = manager(
            broker=MarginBroker(order_margin=400.0), max_margin_utilization=0.50,
            margin_stress_multiplier=2.0, margin_estimate_buffer=1.25,
        ).validate_margin_for_order(side="BUY", volume=1.0, entry=100.0, sl=99.0)

        self.assertTrue(safe["valid"])
        self.assertFalse(blocked["valid"])
        self.assertEqual(blocked["reason"], "MARGIN_STRESS_LIMIT")

    def test_margin_stress_guard_blocks_new_entry_when_existing_position_has_no_sl(self):
        class MarginBroker(FakeBroker):
            def __init__(self):
                super().__init__(positions=[SimpleNamespace(
                    symbol="EURUSD", type=0, price_open=100.0, sl=0.0, volume=1.0,
                )])

            def account_margin_snapshot(self):
                return {"balance": 1000.0, "equity": 1000.0, "margin": 100.0}

            def estimate_order_margin(self, *_args):
                return 100.0

            def estimate_order_profit(self, _side, _symbol, volume, open_price, close_price):
                return (close_price - open_price) * 30.0 * volume

        result = manager(
            broker=MarginBroker(), max_margin_utilization=0.50,
        ).validate_margin_for_order(side="BUY", volume=1.0, entry=100.0, sl=99.0)
        self.assertEqual(result["reason"], "MARGIN_STRESS_UNPROTECTED_POSITION")


class MarketAndSessionGuardTests(unittest.TestCase):
    def test_symbol_settings_are_asset_specific(self):
        fx = portfolio_config.market_guard_settings("FX", "EURUSD")
        metal = portfolio_config.market_guard_settings("METAL", "XAUUSD")
        self.assertLess(fx["max_spread_points"], metal["max_spread_points"])
        self.assertLess(fx["max_tick_jump_points"], metal["max_tick_jump_points"])

    def test_market_asset_class_is_explicit_and_unknown_class_fails(self):
        # A symbol spelling never decides the category: this is deliberately
        # explicit so suffixes and indices are operator-configurable.
        assert portfolio_config.market_guard_settings("FX", "XAUUSD") == \
            portfolio_config.MARKET_GUARD_DEFAULT
        with self.assertRaisesRegex(ValueError, "asset_class"):
            portfolio_config.market_guard_settings("CRYPTO", "BTCUSD")

    def test_market_reason_codes_cover_no_tick_stale_tick_spread_and_jump(self):
        broker = FakeBroker()
        clock = [100.0]
        guard = MarketGuard(broker, 10, 10, 10, time_fn=lambda: clock[0])
        guard.update(None)
        self.assertEqual(guard.reason(), "MARKET_NO_TICK")
        self.assertEqual(guard.operator_reason(), "NO TICK")
        guard.update(SimpleNamespace(bid=1.0, ask=1.00001, time=80.0))
        self.assertEqual(guard.reason(), "MARKET_TICK_AGE_LIMIT")
        self.assertEqual(guard.operator_reason(), "STALE TICK")
        guard.update(SimpleNamespace(bid=1.0, ask=1.0002, time=100.0))
        self.assertEqual(guard.reason(), "MARKET_SPREAD_LIMIT")
        self.assertEqual(guard.operator_reason(), "SPREAD")
        guard.update(SimpleNamespace(bid=1.0, ask=1.00001, time=100.0))
        self.assertTrue(guard.can_trade())
        self.assertIsNone(guard.operator_reason())
        guard.update(SimpleNamespace(bid=1.0002, ask=1.00021, time=100.0))
        self.assertEqual(guard.reason(), "MARKET_TICK_JUMP_LIMIT")
        self.assertEqual(guard.operator_reason(), "PRICE JUMP")

    def test_price_jump_recovers_after_second_consistent_tick(self):
        broker = FakeBroker()
        clock = [100.0]
        guard = MarketGuard(broker, 50, 30, 10, time_fn=lambda: clock[0])
        guard.update(SimpleNamespace(bid=1.00000, ask=1.00001, time=100.0))

        clock[0] = 101.0
        guard.update(SimpleNamespace(bid=1.00100, ask=1.00101, time=101.0))
        self.assertEqual(guard.reason(), "MARKET_TICK_JUMP_LIMIT")
        self.assertEqual(guard.last_bid, 1.00000)

        clock[0] = 102.0
        guard.update(SimpleNamespace(bid=1.00102, ask=1.00103, time=102.0))
        self.assertTrue(guard.can_trade())
        self.assertEqual(guard.last_bid, 1.00102)
        self.assertIsNone(guard.jump_candidate_bid)

    def test_single_jump_outlier_does_not_replace_baseline(self):
        broker = FakeBroker()
        clock = [100.0]
        guard = MarketGuard(broker, 50, 30, 10, time_fn=lambda: clock[0])
        guard.update(SimpleNamespace(bid=1.00000, ask=1.00001, time=100.0))
        clock[0] = 101.0
        guard.update(SimpleNamespace(bid=1.01000, ask=1.01001, time=101.0))
        self.assertFalse(guard.can_trade())

        clock[0] = 102.0
        guard.update(SimpleNamespace(bid=1.00002, ask=1.00003, time=102.0))
        self.assertTrue(guard.can_trade())
        self.assertEqual(guard.last_bid, 1.00002)

    def test_friday_cutoff_and_flatten_do_not_require_a_tick(self):
        config = SimpleNamespace(
            SESSION_TIMEZONE="UTC",
            FRIDAY_NO_TRADE_HOUR=23,
            FRIDAY_NO_TRADE_MINUTE=0,
            FRIDAY_FORCE_CLOSE_HOUR=23,
            FRIDAY_FORCE_CLOSE_MINUTE=30,
        )
        broker = FakeBroker(now=datetime(2026, 7, 10, 23, 30))
        session = SessionGuard(broker, config)
        self.assertEqual(session.reason(), "SESSION_FRIDAY_NO_TRADE")
        self.assertTrue(session.must_flatten_positions())


if __name__ == "__main__":
    unittest.main()
