from __future__ import annotations

import types
from pathlib import Path

from core import broker as broker_module
from core.broker import Broker
from core.position_manager import PositionManager
from monitoring.alerts import Alerts
from monitoring.heartbeat import Heartbeat
from run_hub import strategy_ids_for_hub
import run_hub
import hub_runtime as hub_runtime_module
from hub_runtime import HubRuntime, StrategyRuntime


class FakeMT5:
    def __init__(self):
        self.initialize_calls = 0
        self.login_calls = 0
        self.shutdown_calls = 0

    def initialize(self, **_kwargs):
        self.initialize_calls += 1
        return True

    def login(self, **_kwargs):
        self.login_calls += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1

    def account_info(self):
        return types.SimpleNamespace(login=42)

    def terminal_info(self):
        return types.SimpleNamespace()

    def symbol_info(self, _symbol):
        return types.SimpleNamespace(visible=True)


def test_symbol_views_share_one_mt5_lifecycle(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(broker_module, "mt5", fake)
    owner = Broker("GBPUSD", login=42, password="x", server="demo")
    assert owner.connect()
    gold = owner.for_symbol("XAUUSD")
    euro = owner.for_symbol("EURGBP")
    assert gold.connect()
    assert euro.connect()
    gold.shutdown()
    euro.shutdown()
    assert fake.initialize_calls == 1
    assert fake.login_calls == 1
    assert fake.shutdown_calls == 0
    owner.shutdown()
    assert fake.shutdown_calls == 1


def test_strategy_session_close_runs_before_normal_position_management():
    calls = []
    position = object()
    position_manager = types.SimpleNamespace(
        has_position=lambda _magic: True,
        get_position=lambda _magic: position,
        close_position=lambda magic, reason: calls.append((magic, reason)) or True,
    )
    runtime = StrategyRuntime(
        strategy_id="session",
        config=types.SimpleNamespace(MAGIC=49001),
        broker=types.SimpleNamespace(
            broker_now=lambda: 1,
            utc_now=lambda: __import__("datetime").datetime(2026, 1, 5, 16),
        ),
        alerts=types.SimpleNamespace(send_throttled_warning=lambda **_kwargs: None),
        state_manager=object(),
        strategy=types.SimpleNamespace(should_close_position=lambda _now, _position: True),
        update_state=object(), data_feed=object(), market_guard=object(),
        session_guard=types.SimpleNamespace(must_flatten_positions=lambda: False),
        execution_guard=types.SimpleNamespace(pending_timed_out=lambda _now: False),
        kill_switch=types.SimpleNamespace(can_trade=lambda: True),
        risk_manager=object(), position_manager=position_manager,
        order_executor=types.SimpleNamespace(reconcile_pending_intents=lambda: None),
        reconciliation=object(), trade_logger=object(), heartbeat=object(), history_bars=500,
    )
    runtime.step()
    assert calls == [(49001, "SESSION_CLOSE")]


def test_halted_strategy_still_reconciles_a_closed_cached_position():
    calls = []
    runtime = StrategyRuntime(
        strategy_id="halted",
        config=types.SimpleNamespace(MAGIC=45001),
        broker=object(), alerts=object(),
        state_manager=types.SimpleNamespace(state={"execution_cache": {"100": {}}}),
        strategy=object(), update_state=object(), data_feed=object(),
        market_guard=object(), session_guard=object(), execution_guard=object(),
        kill_switch=types.SimpleNamespace(can_trade=lambda: False),
        risk_manager=object(),
        position_manager=types.SimpleNamespace(has_position=lambda _magic: False),
        order_executor=object(),
        reconciliation=types.SimpleNamespace(reconcile=lambda: calls.append("reconcile") or 1),
        trade_logger=object(), heartbeat=object(), history_bars=500,
    )
    runtime.step()
    assert calls == ["reconcile"]


def test_confirmed_slippage_risk_warns_but_does_not_trip_kill_switch():
    calls = []
    warnings = []
    position = types.SimpleNamespace(ticket=77)
    runtime = StrategyRuntime(
        strategy_id="breakout",
        config=types.SimpleNamespace(MAGIC=45001),
        broker=types.SimpleNamespace(broker_now=lambda: 1, utc_now=lambda: 1),
        alerts=types.SimpleNamespace(
            send_throttled_warning=lambda **kwargs: warnings.append(kwargs["message"]),
        ),
        state_manager=types.SimpleNamespace(
            state={"execution_cache": {"77": {"planned_risk_usd": 25.0}}},
            get_execution_cache=lambda ticket: {"planned_risk_usd": 25.0},
        ),
        strategy=types.SimpleNamespace(should_close_position=lambda *_args: False),
        update_state=object(), data_feed=object(),
        market_guard=object(),
        session_guard=types.SimpleNamespace(must_flatten_positions=lambda: False),
        execution_guard=types.SimpleNamespace(pending_timed_out=lambda _now: False),
        kill_switch=types.SimpleNamespace(
            can_trade=lambda: True,
            reset_desync=lambda: calls.append("reset"),
            register_desync=lambda: calls.append("desync"),
        ),
        risk_manager=types.SimpleNamespace(
            validate_open_position=lambda *_args, **_kwargs: (
                True, "POSITION_RISK_ELEVATED_BY_SLIPPAGE"
            ),
            position_validation_warning=lambda _reason: "slippage warning",
        ),
        position_manager=types.SimpleNamespace(
            has_position=lambda _magic: True,
            get_position=lambda _magic: position,
            validate_position=lambda _magic: True,
            manage_break_even=lambda _magic: False,
        ),
        order_executor=types.SimpleNamespace(reconcile_pending_intents=lambda: None),
        reconciliation=object(), trade_logger=object(), heartbeat=object(), history_bars=500,
    )

    runtime.step()

    assert calls == ["reset"]
    assert warnings == ["slippage warning"]


def test_hub_strategy_selection_comes_only_from_registry():
    assert set(strategy_ids_for_hub("hub_demo")) == {
        "audcad_h4_reversion",
        "xau_h4_continuation_breakout",
        "eurgbp_h4_reversion_return_filter",
    }
    assert strategy_ids_for_hub("hub_1") == []


def test_strategy_managed_exit_bypasses_portfolio_friday_close():
    calls = []
    position = object()
    position_manager = types.SimpleNamespace(
        has_position=lambda _magic: True,
        get_position=lambda _magic: position,
        force_close_weekend=lambda magic: calls.append(("weekend", magic)),
        force_close_strategy_exit=lambda magic: calls.append(("strategy", magic)) or True,
    )
    runtime = StrategyRuntime(
        strategy_id="xau_h4_continuation_breakout",
        config=types.SimpleNamespace(
            MAGIC=53001,
            USE_PORTFOLIO_FRIDAY_FORCE_CLOSE=False,
            STRATEGY_MANAGED_EXIT_FORCE_CLOSE=True,
        ),
        broker=types.SimpleNamespace(broker_now=lambda: 1, utc_now=lambda: 2),
        alerts=types.SimpleNamespace(send_throttled_warning=lambda **_kwargs: None),
        state_manager=object(),
        strategy=types.SimpleNamespace(should_close_position=lambda *_args: True),
        update_state=object(), data_feed=object(), market_guard=object(),
        session_guard=types.SimpleNamespace(must_flatten_positions=lambda: True),
        execution_guard=types.SimpleNamespace(pending_timed_out=lambda _now: False),
        kill_switch=types.SimpleNamespace(can_trade=lambda: True),
        risk_manager=object(), position_manager=position_manager,
        order_executor=types.SimpleNamespace(reconcile_pending_intents=lambda: None),
        reconciliation=object(), trade_logger=object(), heartbeat=object(), history_bars=2500,
    )
    runtime.step()
    assert calls == [("strategy", 53001)]


def test_strategy_time_exit_failure_sends_manual_intervention_critical(monkeypatch):
    position = types.SimpleNamespace(ticket=77)
    broker = types.SimpleNamespace(
        get_position=lambda _magic: position,
        close_position=lambda _position: None,
    )
    warnings = []
    critical = []
    alerts = types.SimpleNamespace(
        send_throttled_warning=lambda *args, **kwargs: warnings.append((args, kwargs)),
        send_throttled_critical=lambda *args, **kwargs: critical.append((args, kwargs)),
    )
    manager = PositionManager(
        broker=broker,
        config=types.SimpleNamespace(STRATEGY_NAME="XAU_H4_CONTINUATION_BREAKOUT"),
        state_manager=object(),
        trade_logger=object(),
        alerts=alerts,
    )
    monkeypatch.setattr("core.position_manager.time.sleep", lambda _seconds: None)

    assert not manager.force_close_strategy_exit(53001)
    assert len(warnings) == 5
    assert len(critical) == 1
    assert "MANUAL INTERVENTION REQUIRED" in critical[0][0][1]


def test_alert_routing_is_copied_from_config():
    routing = {"critical": {"main": False, "strategy": True}}
    alerts = Alerts(routing=routing)
    routing["critical"]["main"] = True
    assert alerts.routing["critical"] == {"main": False, "strategy": True}


def test_unknown_hub_fails_before_full_config_validation(monkeypatch):
    monkeypatch.setattr(run_hub, "validate_configuration", lambda: (_ for _ in ()).throw(AssertionError("late")))
    try:
        run_hub.main(["typo_hub"])
    except SystemExit as exc:
        assert str(exc) == "unknown hub: typo_hub"
    else:
        raise AssertionError("unknown hub must fail")


def test_public_snapshot_has_no_local_launchers_or_legacy_runners():
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "runners").glob("run_*.py"))
    assert not list(root.glob("start_hub*.bat"))


def test_strategy_heartbeat_matches_compact_contract(monkeypatch):
    heartbeat = Heartbeat(
        broker=types.SimpleNamespace(
            broker_now=lambda: __import__("datetime").datetime(2026, 7, 11, 2, 56, 51),
            clock=types.SimpleNamespace(status_snapshot=lambda: {"offset_hours": 3}),
        ),
        market_guard=object(), session_guard=object(), execution_guard=object(),
        risk_manager=object(), position_manager=object(), kill_switch=object(),
        state_manager=object(),
        strategy_config=types.SimpleNamespace(STRATEGY_NAME="AUDCAD_H4_REVERSION"),
        alerts=object(),
        trade_logger=types.SimpleNamespace(
            strategy_id="audcad_h4_reversion",
            get_daily_strategy_pnl=lambda *_: 0.0,
            get_weekly_strategy_pnl=lambda *_: 8.87,
        ),
    )
    monkeypatch.setattr(heartbeat, "_format_uptime", lambda: "00d 00h 00m")
    monkeypatch.setattr(heartbeat, "_connection_status", lambda: "OK")
    monkeypatch.setattr(heartbeat, "_sync_status", lambda: "OK")
    monkeypatch.setattr(heartbeat, "_position_status", lambda: ("0 open", "OK"))
    monkeypatch.setattr(heartbeat, "_stats_status", lambda: "SYNCED")
    monkeypatch.setattr(heartbeat, "_risk_status", lambda: "OK")
    monkeypatch.setattr(heartbeat, "_guards_status", lambda: "OK")
    message = heartbeat.build_strategy_status_message()
    assert message == (
        "Bot: AUDCAD_H4_REVERSION\n"
        "[02:56:51 UTC | Broker offset: +3h | Uptime: 00d 00h 00m]\n\n"
        "Connection: OK | Sync: OK\n"
        "Positions: 0 open | Managed: OK | Stats: SYNCED\n\n"
        "Strategy PnL:\nToday: +0.00$ | Week: +8.87$\n\n"
        "Risk: OK\nGuards: OK"
    )


def test_strategy_heartbeat_reports_short_market_guard_reason():
    heartbeat = Heartbeat(
        broker=object(),
        market_guard=types.SimpleNamespace(
            can_trade=lambda: False, operator_reason=lambda: "SPREAD",
        ),
        session_guard=object(),
        execution_guard=types.SimpleNamespace(pending_execution=False),
        risk_manager=object(), position_manager=object(),
        kill_switch=types.SimpleNamespace(triggered=False),
        state_manager=object(), strategy_config=object(), alerts=object(),
        trade_logger=object(),
    )
    assert heartbeat._guards_status() == "BLOCKED (SPREAD)"


def test_hub_heartbeat_reports_realized_pnl_and_active_strategies(monkeypatch):
    broker_now = __import__("datetime").datetime(2026, 7, 11, 2, 56, 51)
    logger = types.SimpleNamespace(
        get_daily_hub_pnl=lambda *_: 0.0,
        get_weekly_hub_pnl=lambda *_: 8.81,
    )
    heartbeat = types.SimpleNamespace(
        _sync_status=lambda: "OK", _position_status=lambda: ("0 open", "OK"),
        _stats_status=lambda: "SYNCED", _guards_status=lambda: "OK",
    )
    runtime = HubRuntime("hub_demo", ["audcad", "xau"])
    runtime.started_at = 1000
    runtime.broker = types.SimpleNamespace(
        broker_now=lambda: broker_now, can_submit_new_orders=lambda: True,
        list_all_positions=lambda: [],
        clock=types.SimpleNamespace(status_snapshot=lambda: {"offset_hours": 3}),
    )
    runtime.account_monitor = types.SimpleNamespace(status_snapshot=lambda: {"halted": False})
    runtime.workers = [
        types.SimpleNamespace(config=types.SimpleNamespace(STRATEGY_NAME="AUDCAD_H4_REVERSION", MAGIC=46001), heartbeat=heartbeat, trade_logger=logger),
        types.SimpleNamespace(config=types.SimpleNamespace(STRATEGY_NAME="XAU_H4_CONTINUATION_BREAKOUT", MAGIC=53001), heartbeat=heartbeat, trade_logger=logger),
    ]
    monkeypatch.setattr(hub_runtime_module.time, "time", lambda: 1000)
    assert runtime.build_hub_status_message() == (
        "[02:56:51 UTC | Broker offset: +3h | Uptime: 00d 00h 00m]\n\n"
        "Hub_demo\nActive strategies:\n >AUDCAD_H4_REVERSION\n >XAU_H4_CONTINUATION_BREAKOUT\n\n"
        "Connection: OK | Sync: OK\n"
        "Positions: 0 open | Managed: OK | Stats: SYNCED\n\n"
        "Hub PnL:\nToday: +0.00$ | Week: +8.81$\n\n"
        "Risk: OK\nGuards: OK"
    )


def test_hub_heartbeat_collapses_any_stats_problem_to_warning(monkeypatch):
    runtime = HubRuntime("hub_1", ["alpha", "beta"])
    runtime.started_at = 1000
    runtime.broker = types.SimpleNamespace(
        broker_now=lambda: __import__("datetime").datetime(2026, 7, 11, 2, 56, 51),
        can_submit_new_orders=lambda: True,
        list_all_positions=lambda: [],
        clock=types.SimpleNamespace(status_snapshot=lambda: {}),
    )
    runtime.account_monitor = types.SimpleNamespace(status_snapshot=lambda: {"halted": False})
    logger = types.SimpleNamespace(
        get_daily_hub_pnl=lambda *_: 0.0,
        get_weekly_hub_pnl=lambda *_: 0.0,
    )
    runtime.workers = [
        types.SimpleNamespace(
            config=types.SimpleNamespace(STRATEGY_NAME=name, MAGIC=index),
            heartbeat=types.SimpleNamespace(
                _sync_status=lambda: "OK", _position_status=lambda: ("0 open", "OK"),
                _stats_status=lambda value=status: value, _guards_status=lambda: "OK",
            ),
            trade_logger=logger,
        )
        for index, (name, status) in enumerate((("ALPHA", "SYNCED"), ("BETA", "WARNING")), start=1)
    ]
    monkeypatch.setattr(hub_runtime_module.time, "time", lambda: 1000)
    assert "Stats: WARNING" in runtime.build_hub_status_message()


def test_hub_heartbeat_reports_shared_guard_reason_and_collapses_mixed_reasons(monkeypatch):
    runtime = HubRuntime("hub_1", ["alpha", "beta"])
    runtime.started_at = 1000
    runtime.broker = types.SimpleNamespace(
        broker_now=lambda: __import__("datetime").datetime(2026, 7, 11, 2, 56, 51),
        can_submit_new_orders=lambda: True, list_all_positions=lambda: [],
        clock=types.SimpleNamespace(status_snapshot=lambda: {}),
    )
    runtime.account_monitor = types.SimpleNamespace(status_snapshot=lambda: {"halted": False})
    logger = types.SimpleNamespace(
        get_daily_hub_pnl=lambda *_: 0.0, get_weekly_hub_pnl=lambda *_: 0.0,
    )

    def workers(reasons):
        return [
            types.SimpleNamespace(
                config=types.SimpleNamespace(STRATEGY_NAME=f"BOT_{index}", MAGIC=index),
                heartbeat=types.SimpleNamespace(
                    _sync_status=lambda: "OK", _position_status=lambda: ("0 open", "OK"),
                    _stats_status=lambda: "SYNCED",
                    _guards_status=lambda value=reason: value,
                ),
                trade_logger=logger,
            )
            for index, reason in enumerate(reasons, start=1)
        ]

    monkeypatch.setattr(hub_runtime_module.time, "time", lambda: 1000)
    runtime.workers = workers(["BLOCKED (SPREAD)", "BLOCKED (SPREAD)"])
    assert "Guards: BLOCKED (SPREAD)" in runtime.build_hub_status_message()
    runtime.workers = workers(["BLOCKED (SPREAD)", "BLOCKED (STALE TICK)"])
    assert "Guards: BLOCKED (MULTIPLE)" in runtime.build_hub_status_message()
