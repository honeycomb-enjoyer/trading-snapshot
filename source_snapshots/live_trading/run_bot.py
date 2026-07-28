# run_bot.py
#
# Unified runner: a single entrypoint that starts any retained strategy.
# in the active portfolio by name, replacing the old copy-pasted runners.
#
# Usage:
#   python run_bot.py audcad_h4_reversion
#   python -m run_bot audcad_h4_reversion
#   python -c "import run_bot; run_bot.main('audcad_h4_reversion')"
#
# What this runner does NOT touch:
#   - strategies/<name>/strategy.py        (logic unchanged)
#   - strategies/<name>/config.py          (params unchanged)
#   - core/, guards/, analytics/, risk/, monitoring/  (unchanged)
#   - portfolio_config.py, secret_config.py, accounts.py
#
# Strategy-specific differences between the former runners are resolved
# generically at runtime, WITHOUT editing strategies/:
#   * which *Strategy class to instantiate  -> auto-discovered (the only
#     class whose name ends with "Strategy" in the strategy module)
#   * update_h4_state vs update_h1_state    -> derived from
#     strategy_config.SIGNAL_TIMEFRAME (H4 -> update_h4_state,
#     H1 -> update_h1_state), with introspection fallback
#   * get_bars(500) vs get_bars(200)        -> strategy_config.HISTORY_BARS
#     if present, else 500

import importlib
import inspect
import os
import sys
import time

# --- ensure live_trading/ root is importable regardless of how we're run ---
# When launched as `python -m run_bot`, sys.path
# may not include this directory, so the strategies.* / core.* packages
# wouldn't resolve. Insert once, idempotently.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from accounts import get_broker_clock_settings, get_risk_rules
from shared.config_validator import ConfigurationValidationError, validate_configuration
from shared.strategy_config_validator import (
    StrategyConfigValidationError,
    import_and_validate_strategy_config,
)


def load_strategy(strategy_name):
    """Import a strategy package by name and return (config_module, klass).

    Args:
        strategy_name: active package under strategies/, e.g. "audcad_h4_reversion".

    Returns:
        (strategy_config, StrategyClass) where strategy_config is the
        strategies/<name>/config module and StrategyClass is the concrete
        strategy class to instantiate.

    Raises:
        SystemExit with a clear message if the package/config/class can't
        be resolved. Failing loud at startup is intentional: no silent
        fallbacks.
    """
    pkg = f"strategies.{strategy_name}"

    # 1. config module (strategies.<name>.config) - owns SYMBOL/MAGIC/etc.
    try:
        strategy_config = importlib.import_module(f"{pkg}.config")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"[run_bot] strategy package {pkg!r} not found: {exc}. "
            f"Check the name (one of strategies/* dirs)."
        )

    # 2. strategy module (strategies.<name>.strategy) - owns the logic + class
    try:
        strategy_module = importlib.import_module(f"{pkg}.strategy")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"[run_bot] strategy module {pkg}.strategy not found: {exc}."
        )

    # 3. discover the concrete *Strategy class by convention: the (only)
    #    top-level class whose name ends with "Strategy". Avoids a hardcoded
    #    class-name table and keeps strategies/ untouched.
    strategy_classes = [
        obj
        for _, obj in inspect.getmembers(strategy_module, inspect.isclass)
        if obj.__name__.endswith("Strategy")
        and obj.__module__ == strategy_module.__name__
    ]

    if len(strategy_classes) != 1:
        names = ", ".join(c.__name__ for c in strategy_classes)
        raise SystemExit(
            f"[run_bot] expected exactly one class ending in 'Strategy' in "
            f"{pkg}.strategy, found {len(strategy_classes)}: [{names}]."
        )

    return strategy_config, strategy_classes[0]


def resolve_update_state_method(strategy, strategy_config):
    """Pick update_h4_state vs update_h1_state without editing strategies/.

    The former runners hardcoded this per strategy. We derive it from the
    signal timeframe so a single run_bot.py serves every strategy. Falls
    back to introspection if SIGNAL_TIMEFRAME isn't informative.
    """
    timeframe = str(
        getattr(strategy_config, "SIGNAL_TIMEFRAME", "")
    ).upper()

    # Prefer the timeframe-matching method when it exists.
    timeframe_method = getattr(strategy, f"update_{timeframe.lower()}_state", None)
    if callable(timeframe_method):
        return timeframe_method
    if timeframe.startswith("H4") and hasattr(strategy, "update_h4_state"):
        return strategy.update_h4_state
    if timeframe.startswith("H1") and hasattr(strategy, "update_h1_state"):
        return strategy.update_h1_state

    # Introspection fallback: use whichever update_*_state exists.
    for attr in ("update_h4_state", "update_h1_state", "update_state"):
        method = getattr(strategy, attr, None)
        if callable(method):
            return method

    raise SystemExit(
        f"[run_bot] strategy {type(strategy).__name__} exposes neither "
        f"update_h4_state nor update_h1_state."
    )


def portfolio_friday_force_close_enabled(strategy_config):
    return bool(getattr(strategy_config, "USE_PORTFOLIO_FRIDAY_FORCE_CLOSE", True))


def portfolio_entry_session_guard_enabled(strategy_config):
    return bool(getattr(strategy_config, "USE_PORTFOLIO_ENTRY_SESSION_GUARD", True))


def strategy_market_guard_settings(strategy_config):
    """Resolve portfolio guard settings with an optional strategy spread cap."""
    import portfolio_config

    settings = portfolio_config.market_guard_settings(
        strategy_config.ASSET_CLASS, strategy_config.SYMBOL
    )
    max_spread_points = getattr(strategy_config, "MAX_SPREAD_POINTS", None)
    if max_spread_points is not None:
        settings["max_spread_points"] = float(max_spread_points)
    return settings


def close_strategy_managed_position(position_manager, strategy_config):
    if getattr(strategy_config, "STRATEGY_MANAGED_EXIT_FORCE_CLOSE", False):
        return position_manager.force_close_strategy_exit(strategy_config.MAGIC)
    return position_manager.close_position(strategy_config.MAGIC, reason="SESSION_CLOSE")


def persist_strategy_state_if_dirty(strategy, state_manager):
    consume = getattr(strategy, "consume_state_dirty", None)
    if callable(consume) and consume():
        strategy.save_to_state(state_manager)


def main(strategy_name=None):
    """Start the trading bot for `strategy_name`.

    If strategy_name is None, reads sys.argv[1] (the `python run_bot.py X`
    invocation form). Validates the name against the shared registry when
    available so a typo fails before any broker connection is opened.
    """
    if strategy_name is None:
        if len(sys.argv) < 2:
            raise SystemExit(
                "[run_bot] usage: python run_bot.py <strategy_name>"
            )
        strategy_name = sys.argv[1]

    # Reject an unknown ID before looking at secrets, then validate the full
    # account/strategy/chat contract before importing any trading component.
    from shared.registry import registry
    if strategy_name not in registry:
        raise SystemExit(
            f"[run_bot] {strategy_name!r} is not in strategies.yaml. "
            f"Known: {registry.list_strategies(enabled_only=False)}"
        )
    try:
        # Imports config.py only (not strategy.py), checks its complete
        # universal contract and confirms its identity proxy matches the
        # registry. This is deliberately before secrets or Broker creation.
        import_and_validate_strategy_config(strategy_name, registry=registry)
    except StrategyConfigValidationError as exc:
        raise SystemExit(f"[run_bot] strategy config invalid:\n{exc}") from exc
    try:
        validation = validate_configuration()
    except ConfigurationValidationError as exc:
        raise SystemExit(f"[run_bot] configuration invalid:\n{exc}") from exc
    if not validation.strategy_metadata[strategy_name]["enabled"]:
        raise SystemExit(f"[run_bot] strategy {strategy_name!r} is disabled.")

    strategy_config, StrategyClass = load_strategy(strategy_name)

    # The validator parses secret_config.py without executing it. Import only
    # after the complete contract has passed, still before Broker is created.
    import secret_config
    import portfolio_config
    from analytics.trade_logger import TradeLogger
    from analytics.trade_reconciliation import TradeReconciliation
    from core.broker import Broker
    from core.retry_policy import RetryPolicy
    from core.data_feed import DataFeed
    from core.order_executor import OrderExecutor
    from core.position_manager import PositionManager
    from core.state_manager import StateManager
    from guards.execution_guard import ExecutionGuard
    from guards.kill_switch import KillSwitch
    from guards.market_guard import MarketGuard
    from guards.recovery_guard import RecoveryGuard
    from guards.session_guard import SessionGuard
    from monitoring.alerts import Alerts
    from monitoring.heartbeat import Heartbeat
    from risk.account_monitor import AccountMonitor
    from risk.risk_manager import RiskManager

    # =====================================
    # ALERTS
    # =====================================
    alerts = Alerts(
        enabled=True,
        telegram_enabled=secret_config.TELEGRAM_ENABLED,
        telegram_token=secret_config.TELEGRAM_BOT_TOKEN,

        # Main alerts chat (opened / closed / critical)
        telegram_chat_id=secret_config.MAIN_CHAT_ID,

        # Strategy-specific debug chat
        strategy_chat_id=secret_config.CHAT_IDS[strategy_name]
    )

    alerts.send_info(f"START {strategy_config.STRATEGY_NAME} BOT (run_bot)")

    # Recovery is deliberately checked before broker connection and before any
    # strategy signal processing. A malformed/ambiguous state remains at its
    # canonical path, so an automatic restart cannot trade with new defaults.
    state_manager = StateManager(
        strategy_config.STRATEGY_NAME,
        alerts=alerts,
    )
    state_result = state_manager.load()
    if not state_result.ready:
        recovery_kill_switch = KillSwitch(
            alerts=alerts,
            strategy_name=strategy_config.STRATEGY_NAME,
        )
        recovery_kill_switch.trigger(
            f"STATE_RECOVERY_REQUIRED ({state_result.error.code.value})"
        )
        return

    # =====================================
    # CORE
    # =====================================
    account = secret_config.ACCOUNTS[strategy_config.ACCOUNT]

    broker = Broker(
        symbol=strategy_config.SYMBOL,
        deviation_points=portfolio_config.ORDER_DEVIATION_POINTS,
        mt5_path=account["mt5_path"],
        login=account["login"],
        password=account["password"],
        server=account["server"],
        alerts=alerts,
        retry_policy=RetryPolicy.from_config(portfolio_config),
        clock_settings=get_broker_clock_settings(strategy_config.ACCOUNT),
    )

    if not broker.connect():
        alerts.send_critical("Broker connection failed")
        return

    broker.calibrate_clock()

    # =====================================
    # GUARDS
    # =====================================
    market_settings = strategy_market_guard_settings(strategy_config)
    market_guard = MarketGuard(
        broker=broker,
        **market_settings,
    )

    session_guard = SessionGuard(
        broker=broker,
        config=portfolio_config
    )

    execution_guard = ExecutionGuard(portfolio_config)

    kill_switch = KillSwitch(
        alerts=alerts,
        strategy_name=strategy_config.STRATEGY_NAME
    )

    # =====================================
    # ANALYTICS
    # =====================================
    trade_logger = TradeLogger(
        account_id=strategy_config.ACCOUNT,
        strategy_id=strategy_name,
        strategy_name=strategy_config.STRATEGY_NAME,
        broker_account_login=account["login"],
    )

    trade_reconciliation = TradeReconciliation(
        broker=broker,
        trade_logger=trade_logger,
        state_manager=state_manager,
        alerts=alerts,
        strategy_config=strategy_config,
        bootstrap_days=portfolio_config.HISTORY_RECONCILIATION_BOOTSTRAP_DAYS,
        overlap_sec=portfolio_config.HISTORY_RECONCILIATION_OVERLAP_SEC,
    )

    # =====================================
    # POSITION MANAGER
    # =====================================
    position_manager = PositionManager(
        broker=broker,
        config=strategy_config,
        state_manager=state_manager,
        trade_logger=trade_logger,
        alerts=alerts
    )

    # =====================================
    # ACCOUNT MONITOR
    # =====================================
    account_monitor = AccountMonitor(
        account=strategy_config.ACCOUNT,
        risk_rules=get_risk_rules(strategy_config.ACCOUNT),
        broker=broker,
        position_manager=position_manager,
        kill_switch=kill_switch,
        alerts=alerts
    )

    # =====================================
    # RISK
    # =====================================
    risk_manager = RiskManager(
        broker=broker,
        strategy_config=strategy_config,
        portfolio_config=portfolio_config,
        trade_logger=trade_logger,
        alerts=alerts,
        account_monitor=account_monitor,
    )

    # =====================================
    # ORDER EXECUTOR
    # =====================================
    order_executor = OrderExecutor(
        broker=broker,
        position_manager=position_manager,
        risk_manager=risk_manager,
        state_manager=state_manager,
        trade_logger=trade_logger,
        config=strategy_config,
        alerts=alerts
    )

    # =====================================
    # RECOVERY GUARD
    # =====================================
    recovery_guard = RecoveryGuard(
        broker=broker,
        position_manager=position_manager,
        state_manager=state_manager,
        kill_switch=kill_switch,
        alerts=alerts,
        strategy_config=strategy_config,
    )

    # =====================================
    # MONITORING
    # =====================================
    heartbeat = Heartbeat(
        broker=broker,
        market_guard=market_guard,
        session_guard=session_guard,
        execution_guard=execution_guard,
        risk_manager=risk_manager,
        position_manager=position_manager,
        kill_switch=kill_switch,
        state_manager=state_manager,
        strategy_config=strategy_config,
        alerts=alerts,
        trade_logger=trade_logger,
        trade_reconciliation=trade_reconciliation
    )

    heartbeat.account_monitor = account_monitor

    # This is deliberately after the broker connection and account-wide
    # position query, but before recovery, strategy state processing, or any
    # order-capable loop.  A failed reset leaves the kill switch engaged.
    if not account_monitor.perform_startup_reset():
        return

    # A persisted halt may have been written immediately before a crash.
    # Verify/flatten account-wide tickets before any strategy recovery path.
    account_monitor.recover_persisted_halt()

    if not recovery_guard.run_startup_recovery():
        return

    trade_reconciliation.reconcile()

    # =====================================
    # STRATEGY
    # =====================================
    data_feed = DataFeed(
        symbol=strategy_config.SYMBOL,
        timeframe=strategy_config.SIGNAL_TIMEFRAME,
        clock=broker.clock,
    )
    strategy = StrategyClass()
    strategy.restore_from_state(state_manager)

    history_bars = getattr(strategy_config, "HISTORY_BARS", 500)
    update_state = resolve_update_state_method(strategy, strategy_config)
    bars = data_feed.get_bars(history_bars)
    if bars is None:
        alerts.send_critical("Cannot load HTF bars")
        return
    update_state(bars)
    strategy.save_to_state(state_manager)
    data_feed.sync_last_bar()

    alerts.send_info("Bot ready")

    heartbeat.print_status()
    last_heartbeat = time.time()
    last_account_check = 0.0  # force an immediate first check
    idle_sleep = portfolio_config.LOOP_IDLE_SLEEP_SEC

    # =====================================
    # MAIN LOOP
    # =====================================
    while True:
        try:
            now_ts = time.time()

            if now_ts - last_heartbeat >= portfolio_config.HEARTBEAT_INTERVAL_SEC:
                heartbeat.print_status()
                last_heartbeat = now_ts

            # =================================
            # ACCOUNT MONITOR
            # =================================
            if now_ts - last_account_check >= account_monitor.check_interval_sec:
                heartbeat.check_account_monitor()
                last_account_check = now_ts

            if not broker.ensure_connection():
                kill_switch.register_broker_failure()
                time.sleep(idle_sleep)
                continue

            kill_switch.reset_broker_failures()

            # This only reconciles previously persisted intents.  It never
            # re-submits an order, so reconnect cannot create a duplicate.
            order_executor.reconcile_pending_intents()

            if not kill_switch.can_trade():
                time.sleep(idle_sleep)
                continue

            broker_now = broker.broker_now()

            if execution_guard.pending_timed_out(broker_now):
                alerts.send_terminal_throttled(
                    key=f"{strategy_config.STRATEGY_NAME}_pending_timeout",
                    message="Pending execution timeout"
                )
                execution_guard.reset()

            # Friday flatten must not depend on receiving a new market tick.
            if (
                portfolio_friday_force_close_enabled(strategy_config)
                and session_guard.must_flatten_positions()
            ):
                if position_manager.has_position(strategy_config.MAGIC):
                    position_manager.force_close_weekend(strategy_config.MAGIC)

            # Position management and reconciliation remain alive during a
            # strategy daily/weekly lock.  They must run before the entry
            # gate, which intentionally disables pre-alert, signal, and order
            # paths below.
            if position_manager.has_position(strategy_config.MAGIC):
                try:
                    position = position_manager.get_position(strategy_config.MAGIC)
                    should_close = getattr(strategy, "should_close_position", None)
                    if callable(should_close) and should_close(broker.utc_now(), position):
                        if not close_strategy_managed_position(
                            position_manager, strategy_config
                        ):
                            alerts.send_throttled_warning(
                                key=f"{strategy_config.STRATEGY_NAME}_session_close_failed",
                                message="Session close failed; runtime will retry",
                            )
                        time.sleep(idle_sleep)
                        continue
                    execution_context = state_manager.get_execution_cache(position.ticket)
                    valid, reason = risk_manager.validate_open_position(
                        position, execution_context=execution_context,
                    )
                    if not valid:
                        raise RuntimeError(reason)
                    if reason:
                        alerts.send_throttled_warning(
                            key=f"{strategy_name}_{reason}",
                            message=risk_manager.position_validation_warning(reason),
                        )
                    position_manager.validate_position(strategy_config.MAGIC)
                    kill_switch.reset_desync()
                    position_manager.manage_break_even(strategy_config.MAGIC)
                except Exception as e:
                    alerts.send_throttled_warning(
                        key=f"{strategy_config.STRATEGY_NAME}_position_validation_failed",
                        message=f"Position validation failed: {e}"
                    )
                    kill_switch.register_desync()
                time.sleep(idle_sleep)
                continue

            if state_manager.state["execution_cache"]:
                recovered = trade_reconciliation.reconcile()
                if recovered > 0:
                    time.sleep(idle_sleep)
                    continue

            # This is the only stateful strategy-lock observation.  A lock
            # transition emits one event/alert; repeated parked iterations are
            # quiet and never invoke strategy pre-alert or signal methods.
            allowed, reason = risk_manager.can_open_new_trade()
            if not allowed:
                time.sleep(idle_sleep)
                continue

            tick = data_feed.get_tick()
            market_guard.update(tick)

            if not market_guard.can_trade():
                time.sleep(idle_sleep)
                continue

            # =================================
            # PRE-SIGNAL ALERT
            # =================================
            pre_alert = strategy.check_pre_alert(tick)

            if pre_alert is not None:
                alerts.alert_pre_signal(
                    strategy_name=strategy_config.STRATEGY_NAME,
                    side=pre_alert["side"],
                    trigger=pre_alert["trigger"],
                    distance_points=pre_alert["distance_points"],
                    expected_entry=pre_alert["expected_entry"],
                    stop_distance=pre_alert["stop_distance"],
                    tp_distance=pre_alert["tp_distance"],
                    risk_usd=strategy_config.RISK_PER_TRADE_USD
                )

            if (
                portfolio_entry_session_guard_enabled(strategy_config)
                and not session_guard.trading_allowed()
            ):
                time.sleep(idle_sleep)
                continue

            if data_feed.is_new_bar():
                bars = data_feed.get_bars(history_bars)

                if bars is not None:
                    update_state(bars)
                    print("HTF state updated")
                if hasattr(tick, "bar_timestamp"):
                    tick.bar_timestamp = data_feed.last_seen_bar
                    tick.bar_open = data_feed.last_seen_bar_open

            if not execution_guard.can_send_order(broker_now):
                time.sleep(idle_sleep)
                continue

            signal = strategy.check_entry_signal(tick)
            persist_strategy_state_if_dirty(strategy, state_manager)

            if signal is None:
                time.sleep(idle_sleep)
                continue

            execution_guard.mark_order_sent(broker_now)

            position = order_executor.execute_signal(
                signal,
                strategy
            )

            if position is not None:
                execution_guard.mark_fill_success()
            else:
                execution_guard.mark_order_failed()

            time.sleep(idle_sleep)

        except Exception as e:
            alerts.send_throttled_warning(
                key=f"{strategy_config.STRATEGY_NAME}_runner_exception",
                message=f"Runner exception: {e}"
            )
            time.sleep(idle_sleep)


if __name__ == "__main__":
    main()
