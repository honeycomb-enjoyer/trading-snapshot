"""One-process runtime for every enabled strategy assigned to one hub."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from accounts import get_broker_clock_settings, get_risk_rules
from run_bot import (
    close_strategy_managed_position,
    load_strategy,
    portfolio_entry_session_guard_enabled,
    portfolio_friday_force_close_enabled,
    persist_strategy_state_if_dirty,
    resolve_update_state_method,
    strategy_market_guard_settings,
)


@dataclass
class StrategyRuntime:
    strategy_id: str
    config: object
    broker: object
    alerts: object
    state_manager: object
    strategy: object
    update_state: object
    data_feed: object
    market_guard: object
    session_guard: object
    execution_guard: object
    kill_switch: object
    risk_manager: object
    position_manager: object
    order_executor: object
    reconciliation: object
    trade_logger: object
    heartbeat: object
    history_bars: int
    last_error: str | None = None

    def step(self, account_halted: bool = False) -> None:
        """Run one non-blocking legacy-compatible strategy iteration."""
        if account_halted or not self.kill_switch.can_trade():
            # A halt blocks new trading/management but must not block the
            # read-only history recovery that records an SL/TP/manual close.
            if (
                self.state_manager.state.get("execution_cache")
                and not self.position_manager.has_position(self.config.MAGIC)
            ):
                self.reconciliation.reconcile()
            return
        broker_now = self.broker.broker_now()
        self.order_executor.reconcile_pending_intents()

        if self.execution_guard.pending_timed_out(broker_now):
            self.alerts.send_terminal_throttled(
                key=f"{self.strategy_id}_pending_timeout",
                message="Pending execution timeout",
            )
            self.execution_guard.reset()

        if (
            portfolio_friday_force_close_enabled(self.config)
            and self.session_guard.must_flatten_positions()
        ):
            if self.position_manager.has_position(self.config.MAGIC):
                self.position_manager.force_close_weekend(self.config.MAGIC)

        if self.position_manager.has_position(self.config.MAGIC):
            try:
                position = self.position_manager.get_position(self.config.MAGIC)
                should_close = getattr(self.strategy, "should_close_position", None)
                if callable(should_close) and should_close(self.broker.utc_now(), position):
                    if not close_strategy_managed_position(
                        self.position_manager, self.config
                    ):
                        self.alerts.send_throttled_warning(
                            key=f"{self.strategy_id}_session_close_failed",
                            message="Session close failed; runtime will retry",
                        )
                    return
                execution_context = self.state_manager.get_execution_cache(position.ticket)
                valid, reason = self.risk_manager.validate_open_position(
                    position, execution_context=execution_context,
                )
                if not valid:
                    raise RuntimeError(reason)
                if reason:
                    self.alerts.send_throttled_warning(
                        key=f"{self.strategy_id}_{reason}",
                        message=self.risk_manager.position_validation_warning(reason),
                    )
                self.position_manager.validate_position(self.config.MAGIC)
                self.kill_switch.reset_desync()
                self.position_manager.manage_break_even(self.config.MAGIC)
            except Exception as exc:
                self.alerts.send_throttled_warning(
                    key=f"{self.strategy_id}_position_validation_failed",
                    message=f"Position validation failed: {exc}",
                )
                self.kill_switch.register_desync()
            return

        if self.state_manager.state["execution_cache"]:
            if self.reconciliation.reconcile() > 0:
                return

        allowed, _reason = self.risk_manager.can_open_new_trade()
        if not allowed:
            return
        tick = self.data_feed.get_tick()
        if tick is None:
            return
        self.market_guard.update(tick)
        if not self.market_guard.can_trade():
            return

        pre_alert = self.strategy.check_pre_alert(tick)
        if pre_alert is not None:
            self.alerts.alert_pre_signal(
                strategy_name=self.config.STRATEGY_NAME,
                side=pre_alert["side"], trigger=pre_alert["trigger"],
                distance_points=pre_alert["distance_points"],
                expected_entry=pre_alert["expected_entry"],
                stop_distance=pre_alert["stop_distance"],
                tp_distance=pre_alert["tp_distance"],
                risk_usd=self.config.RISK_PER_TRADE_USD,
            )
        if (
            portfolio_entry_session_guard_enabled(self.config)
            and not self.session_guard.trading_allowed()
        ):
            return
        if self.data_feed.is_new_bar():
            bars = self.data_feed.get_bars(self.history_bars)
            if bars is not None:
                self.update_state(bars)
                self.strategy.save_to_state(self.state_manager)
            if hasattr(tick, "bar_timestamp"):
                tick.bar_timestamp = self.data_feed.last_seen_bar
                tick.bar_open = self.data_feed.last_seen_bar_open
        if not self.execution_guard.can_send_order(broker_now):
            return
        signal = self.strategy.check_entry_signal(tick)
        persist_strategy_state_if_dirty(self.strategy, self.state_manager)
        if signal is None:
            return
        self.execution_guard.mark_order_sent(broker_now)
        position = self.order_executor.execute_signal(signal, self.strategy)
        if position is not None:
            self.execution_guard.mark_fill_success()
        else:
            self.execution_guard.mark_order_failed()

    def status_line(self) -> str:
        risk = self.risk_manager.status_snapshot()
        position = "OPEN" if self.position_manager.has_position(self.config.MAGIC) else "FLAT"
        state = risk.get("lock_state") if risk.get("daily_locked") or risk.get("weekly_locked") else "ACTIVE"
        if self.kill_switch.triggered:
            state = "HALTED"
        if self.last_error:
            state = "ERROR"
        return f"{self.strategy_id}: {state} | {position} | {self.config.SYMBOL}"


class HubInstanceLock:
    """Small OS-backed single-instance guard; no operator-generated IDs."""

    def __init__(self, hub_id: str, directory: str | Path = "runtime"):
        self.path = Path(directory) / f"{hub_id}.lock"
        self.handle = None

    def acquire(self) -> None:
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"hub {self.path.stem} is already running") from exc

    def release(self) -> None:
        if self.handle is None:
            return
        import msvcrt

        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None


class HubRuntime:
    def __init__(self, hub_id: str, strategy_ids: list[str], *, shadow: bool = False):
        self.hub_id = hub_id
        self.strategy_ids = list(strategy_ids)
        self.shadow = shadow
        self.workers: list[StrategyRuntime] = []
        self.broker = None
        self.account_monitor = None
        self.hub_alerts = None
        self.lock = HubInstanceLock(hub_id)
        self.started_at = time.time()
        self.last_heartbeat = 0.0
        self.last_account_check = 0.0
        self.last_health_publish = 0.0

    def start(self) -> None:
        if not self.strategy_ids:
            raise RuntimeError(f"hub {self.hub_id} has no enabled strategies")
        self.lock.acquire()
        try:
            self._compose()
        except Exception:
            self.stop()
            raise

    def _compose(self) -> None:
        import portfolio_config
        import secret_config
        from analytics.trade_logger import TradeLogger
        from analytics.trade_reconciliation import TradeReconciliation
        from core.broker import Broker
        from core.data_feed import DataFeed
        from core.order_executor import OrderExecutor
        from core.position_manager import PositionManager
        from core.retry_policy import RetryPolicy
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

        account = secret_config.ACCOUNTS[self.hub_id]
        first_config, _ = load_strategy(self.strategy_ids[0])
        self.hub_alerts = Alerts(
            enabled=True, telegram_enabled=secret_config.TELEGRAM_ENABLED,
            telegram_token=secret_config.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=secret_config.MAIN_CHAT_ID,
            strategy_chat_id=None,
            routing=portfolio_config.ALERT_ROUTING,
        )
        self.broker = Broker(
            symbol=first_config.SYMBOL,
            deviation_points=portfolio_config.ORDER_DEVIATION_POINTS,
            mt5_path=account["mt5_path"], login=account["login"],
            password=account["password"], server=account["server"],
            alerts=self.hub_alerts,
            retry_policy=RetryPolicy.from_config(portfolio_config),
            clock_settings=get_broker_clock_settings(self.hub_id),
        )
        if not self.broker.connect():
            raise RuntimeError("broker connection failed")
        self.broker.calibrate_clock()

        hub_kill_switch = KillSwitch(self.hub_alerts, self.hub_id)
        self.account_monitor = AccountMonitor(
            account=self.hub_id, risk_rules=get_risk_rules(self.hub_id),
            broker=self.broker, position_manager=None,
            kill_switch=hub_kill_switch, alerts=self.hub_alerts,
        )
        if not self.account_monitor.perform_startup_reset():
            raise RuntimeError("startup reset blocked")
        self.account_monitor.recover_persisted_halt()

        for strategy_id in self.strategy_ids:
            config, strategy_class = load_strategy(strategy_id)
            alerts = Alerts(
                enabled=True, telegram_enabled=secret_config.TELEGRAM_ENABLED,
                telegram_token=secret_config.TELEGRAM_BOT_TOKEN,
                telegram_chat_id=secret_config.MAIN_CHAT_ID,
                strategy_chat_id=secret_config.CHAT_IDS[strategy_id],
                routing=portfolio_config.ALERT_ROUTING,
            )
            broker = self.broker.for_symbol(config.SYMBOL, alerts=alerts)
            if not broker.connect():
                raise RuntimeError(f"cannot prepare symbol {config.SYMBOL}")
            state_manager = StateManager(config.STRATEGY_NAME, alerts=alerts)
            state_result = state_manager.load()
            if not state_result.ready:
                raise RuntimeError(f"{strategy_id}: state recovery required ({state_result.error.code.value})")
            trade_logger = TradeLogger(
                account_id=self.hub_id, strategy_id=strategy_id,
                strategy_name=config.STRATEGY_NAME,
                broker_account_login=account["login"],
            )
            kill_switch = KillSwitch(alerts, config.STRATEGY_NAME)
            position_manager = PositionManager(broker, config, state_manager, trade_logger, alerts)
            risk_manager = RiskManager(
                broker=broker, strategy_config=config,
                portfolio_config=portfolio_config, trade_logger=trade_logger,
                alerts=alerts, account_monitor=self.account_monitor,
            )
            order_executor = OrderExecutor(
                broker=broker, position_manager=position_manager,
                risk_manager=risk_manager, state_manager=state_manager,
                trade_logger=trade_logger, alerts=alerts, config=config,
            )
            reconciliation = TradeReconciliation(
                broker=broker, trade_logger=trade_logger,
                state_manager=state_manager, alerts=alerts,
                strategy_config=config,
                bootstrap_days=portfolio_config.HISTORY_RECONCILIATION_BOOTSTRAP_DAYS,
                overlap_sec=portfolio_config.HISTORY_RECONCILIATION_OVERLAP_SEC,
            )
            market_guard = MarketGuard(
                broker=broker,
                **strategy_market_guard_settings(config),
            )
            session_guard = SessionGuard(broker=broker, config=portfolio_config)
            execution_guard = ExecutionGuard(portfolio_config)
            recovery_guard = RecoveryGuard(
                broker, position_manager, state_manager, kill_switch, alerts, config,
            )
            if not recovery_guard.run_startup_recovery():
                raise RuntimeError(f"{strategy_id}: startup recovery failed")
            reconciliation.reconcile()
            data_feed = DataFeed(
                config.SYMBOL,
                config.SIGNAL_TIMEFRAME,
                clock=broker.clock,
            )
            strategy = strategy_class()
            strategy.restore_from_state(state_manager)
            update_state = resolve_update_state_method(strategy, config)
            history_bars = getattr(config, "HISTORY_BARS", 500)
            bars = data_feed.get_bars(history_bars)
            if bars is None:
                raise RuntimeError(f"{strategy_id}: cannot load closed bars")
            update_state(bars)
            strategy.save_to_state(state_manager)
            data_feed.sync_last_bar()
            heartbeat = Heartbeat(
                broker, market_guard, session_guard, execution_guard,
                risk_manager, position_manager, kill_switch, state_manager,
                config, alerts, trade_logger, reconciliation, self.account_monitor,
            )
            self.workers.append(StrategyRuntime(
                strategy_id, config, broker, alerts, state_manager, strategy,
                update_state, data_feed, market_guard, session_guard,
                execution_guard, kill_switch, risk_manager, position_manager,
                order_executor, reconciliation, trade_logger, heartbeat,
                history_bars,
            ))
        self._publish_health()
        self.send_heartbeat()
        self.last_heartbeat = time.time()

    def run_forever(self) -> None:
        import portfolio_config

        self.start()
        try:
            while True:
                now = time.time()
                if not self.broker.ensure_connection():
                    time.sleep(portfolio_config.LOOP_IDLE_SLEEP_SEC)
                    continue
                if now - self.last_account_check >= self.account_monitor.check_interval_sec:
                    self.account_monitor.check()
                    self.last_account_check = now
                halted = bool(self.account_monitor.state.get("halted", False))
                for worker in self.workers:
                    try:
                        if not self.shadow:
                            worker.step(account_halted=halted)
                            worker.last_error = None
                    except Exception as exc:
                        worker.last_error = str(exc)
                        worker.alerts.send_throttled_warning(
                            key=f"{worker.strategy_id}_worker_exception",
                            message=f"Worker exception: {exc}",
                        )
                if now - self.last_heartbeat >= portfolio_config.HEARTBEAT_INTERVAL_SEC:
                    self.send_heartbeat()
                    self.last_heartbeat = now
                if now - self.last_health_publish >= 5.0:
                    self._publish_health()
                    self.last_health_publish = now
                time.sleep(portfolio_config.LOOP_IDLE_SLEEP_SEC)
        finally:
            self.stop()

    def health_snapshot(self) -> dict:
        account = self.account_monitor.status_snapshot() if self.account_monitor else {}
        broker_clock = (
            self.broker.clock.status_snapshot()
            if self.broker is not None and getattr(self.broker, "clock", None) is not None
            else None
        )
        return {
            "hub_id": self.hub_id,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "broker_connected": bool(self.broker and self.broker.can_submit_new_orders()),
            "broker_clock": broker_clock,
            "account_halted": bool(account.get("halted")),
            "shadow": self.shadow,
            "strategies": {worker.strategy_id: worker.status_line() for worker in self.workers},
        }

    def send_heartbeat(self) -> None:
        for worker in self.workers:
            try:
                worker.alerts.alert_heartbeat(worker.heartbeat.build_strategy_status_message())
            except Exception as exc:
                worker.alerts.send_throttled_warning(
                    key=f"{worker.strategy_id}_heartbeat_failed",
                    message=f"Heartbeat build failed: {exc}",
                )
        try:
            self.hub_alerts.send_main_message(self.build_hub_status_message())
        except Exception as exc:
            self.hub_alerts.send_throttled_warning(
                key=f"{self.hub_id}_heartbeat_failed",
                message=f"Hub heartbeat build failed: {exc}",
            )

    def build_hub_status_message(self) -> str:
        broker_time = self.broker.broker_now()
        clock = getattr(self.broker, "clock", None)
        clock_snapshot = clock.status_snapshot() if clock is not None else {}
        offset = clock_snapshot.get("offset_hours")
        clock_suffix = "" if offset is None else f" | Broker offset: {offset:+g}h"
        elapsed = int(time.time() - self.started_at)
        uptime = f"{elapsed // 86400:02d}d {(elapsed % 86400) // 3600:02d}h {(elapsed % 3600) // 60:02d}m"
        active = "\n".join(f" >{worker.config.STRATEGY_NAME}" for worker in self.workers)
        connection = "OK" if self.broker.can_submit_new_orders() else "FAIL"
        sync_values = [worker.heartbeat._sync_status() for worker in self.workers]
        sync = "OK" if all(value == "OK" for value in sync_values) else "FAIL"
        try:
            positions = self.broker.list_all_positions()
            positions_count = len(positions)
            known_magics = {worker.config.MAGIC for worker in self.workers}
            all_known = all(getattr(position, "magic", None) in known_magics for position in positions)
            workers_synced = all(worker.heartbeat._position_status()[1] != "DESYNC" for worker in self.workers)
            managed = "OK" if all_known and workers_synced else "DESYNC"
        except Exception:
            positions_count, managed = "?", "DESYNC"
        stats_values = [worker.heartbeat._stats_status() for worker in self.workers]
        stats = "WARNING" if "WARNING" in stats_values else "SYNCED"
        logger = self.workers[0].trade_logger
        daily_pnl = logger.get_daily_hub_pnl(broker_time)
        weekly_pnl = logger.get_weekly_hub_pnl(broker_time)
        account = self.account_monitor.status_snapshot()
        margin_locked = False
        for worker in self.workers:
            risk_manager = getattr(worker, "risk_manager", None)
            status_snapshot = getattr(risk_manager, "status_snapshot", None)
            if callable(status_snapshot) and status_snapshot().get("margin_locked", False):
                margin_locked = True
                break
        risk = "HALTED" if account.get("halted") else "LOCKED (MARGIN)" if margin_locked else "OK"
        guard_values = [worker.heartbeat._guards_status() for worker in self.workers]
        blocked_guards = [value for value in guard_values if value != "OK"]
        if not blocked_guards:
            guards = "OK"
        else:
            reasons = {
                value.removeprefix("BLOCKED (").removesuffix(")")
                if value.startswith("BLOCKED (") else "CHECK FAILED"
                for value in blocked_guards
            }
            reason = next(iter(reasons)) if len(reasons) == 1 else "MULTIPLE"
            guards = f"BLOCKED ({reason})"
        return (
            f"[{broker_time.strftime('%H:%M:%S')} UTC{clock_suffix} | Uptime: {uptime}]\n\n"
            f"{self.hub_id.capitalize()}\n"
            f"Active strategies:\n{active}\n\n"
            f"Connection: {connection} | Sync: {sync}\n"
            f"Positions: {positions_count} open | Managed: {managed} | Stats: {stats}\n\n"
            f"Hub PnL:\n"
            f"Today: {daily_pnl:+.2f}$ | Week: {weekly_pnl:+.2f}$\n\n"
            f"Risk: {risk}\n"
            f"Guards: {guards}"
        )

    def _publish_health(self) -> None:
        if not self.account_monitor:
            return
        path = Path("runtime") / f"{self.hub_id}.health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.health_snapshot(), indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def stop(self) -> None:
        if self.broker is not None:
            self.broker.shutdown()
            self.broker = None
        self.lock.release()
