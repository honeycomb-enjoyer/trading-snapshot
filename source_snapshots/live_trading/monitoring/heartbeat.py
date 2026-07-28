# monitoring/heartbeat.py

import MetaTrader5 as mt5
import time


class Heartbeat:
    def __init__(
        self,
        broker,
        market_guard,
        session_guard,
        execution_guard,
        risk_manager,
        position_manager,
        kill_switch,
        state_manager,
        strategy_config,
        alerts,
        trade_logger,
        trade_reconciliation=None,
        account_monitor=None
    ):
        self.broker = broker
        self.market_guard = market_guard
        self.session_guard = session_guard
        self.execution_guard = execution_guard
        self.risk_manager = risk_manager
        self.position_manager = position_manager
        self.kill_switch = kill_switch
        self.state_manager = state_manager
        self.strategy_config = strategy_config
        self.alerts = alerts
        self.trade_reconciliation = trade_reconciliation
        self.trade_logger = trade_logger

        # Optional. None means monitor disabled and keeps old heartbeat output.
        self.account_monitor = account_monitor

        self.start_ts = time.time()

        def _log(self, message):
            if self.alerts:
                self.alerts.send_info(message)
            else:
                print(message)

    # =====================================
    # ACCOUNT MONITOR CHECK
    # =====================================
    def check_account_monitor(self):
        """
        Drive the per-account equity/DD/profit monitor. Intended to be
        called from the runner main loop on its own throttle (~5s), NOT
        tied to the 1200s heartbeat cadence.

        No-op if no account_monitor was wired in.
        """
        if self.account_monitor is None:
            return None

        try:
            return self.account_monitor.check()
        except Exception as e:
            if self.alerts:
                self.alerts.send_throttled_warning(
                    key="account_monitor_check_failed",
                    message=f"Account monitor check failed: {e}"
                )
            return None

    # =====================================
    # UPTIME
    # =====================================
    def _format_uptime(self):
        elapsed = int(time.time() - self.start_ts)

        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        minutes = (elapsed % 3600) // 60

        return f"{days:02d}d {hours:02d}h {minutes:02d}m"

    # =====================================
    # CONNECTION STATUS (READ ONLY)
    # =====================================
    def _connection_status(self):
        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None:
            return "FAIL"

        if account is None:
            return "FAIL"

        return "OK"

    # =====================================
    # SYNC STATUS
    # =====================================
    def _sync_status(self):
        try:
            tick = self.broker.get_tick()
            if tick is None:
                return "FAIL"

            state = self.state_manager.state
            if state is None:
                return "FAIL"

            return "OK"

        except Exception:
            return "FAIL"

    # =====================================
    # POSITION STATUS
    # =====================================
    def _position_status(self):
        try:
            has_pos = self.position_manager.has_position(
                self.strategy_config.MAGIC
            )

            if not has_pos:
                return "0 open", "OK"

            self.position_manager.validate_position(
                self.strategy_config.MAGIC
            )

            return "1 open", "Managed OK"

        except Exception:
            return "1 open", "DESYNC"

    # =====================================
    # STATS STATUS
    # =====================================
    #
    # Previously this only checked for the presence of the
    # "execution_cache" key, which is always present (DEFAULT_STATE
    # initializes it to {}). As a result heartbeat reported "Synced"
    # even when close analytics were incomplete or trades.csv had
    # missing enriched fields.
    #
    # New checks:
    #   1. execution_cache must be a dict whose every entry carries the
    #      mandatory keys {trade_id, risk_usd, actual_entry_price,
    #      expected_entry_price}. A partial cache entry means data was
    #      lost in-memory before the position closed -> FAIL.
    #   2. The last closed trade in the durable ledger must have its enriched
    #      fields populated:
    #        - numeric / filled          -> SYNCED
    #        - "N/A" sentinel (explicit) -> SYNCED (field was not computable,
    #                                        but the recovery is complete)
    #        - blank / None              -> WARNING (data loss)
    #   3. If no trade has been closed yet, the cache alone decides:
    #      an empty or fully-populated cache -> SYNCED.
    #
    # Operator-facing status deliberately has only two outcomes:
    #   SYNCED  - ledger is usable, including explicitly unavailable optional
    #             metadata on recovered historical trades;
    #   WARNING - current state/ledger reconciliation needs inspection.
    # =====================================
    def _stats_status(self):
        try:
            export_status = getattr(self.trade_logger, "export_status_snapshot", None)
            if callable(export_status) and export_status().get("status") == "WARNING":
                return "WARNING"
            if self.trade_reconciliation is not None:
                reconciliation = self.trade_reconciliation.health_snapshot()
                if reconciliation["status"] == "DEGRADED":
                    return "WARNING"
            state = self.state_manager.state
            if state is None:
                return "WARNING"

            cache = state.get("execution_cache")
            if not isinstance(cache, dict):
                return "WARNING"

            # (1) Every cached open position must carry the mandatory
            # keys. A half-populated entry means the open-trade record
            # is already incomplete -> FAIL (cannot produce a complete
            # close row later).
            mandatory = (
                "trade_id",
                "risk_usd",
                "actual_entry_price",
                "expected_entry_price",
            )
            for entry in cache.values():
                if not isinstance(entry, dict):
                    return "WARNING"
                if any(k not in entry for k in mandatory):
                    return "WARNING"

            # (2) Inspect the most recent closed ledger trade.
            last = self.trade_logger.get_last_closed_trade()

            if last is None:
                # No closes yet: cache completeness alone decides.
                # Empty cache or fully-populated entries -> Synced.
                return "SYNCED"

            enriched = ("pnl_r", "pnl_points", "trade_duration_sec")
            for field in enriched:
                value = last.get(field)
                if value is None or value == "":
                    # Blank cell = data was lost (not the N/A sentinel).
                    return "WARNING"

            return "SYNCED"

        except Exception:
            return "WARNING"

    # =====================================
    # RISK STATUS
    # =====================================
    def _risk_status(self):
        try:
            snapshot = self.risk_manager.status_snapshot()
            if (
                not snapshot["daily_locked"]
                and not snapshot["weekly_locked"]
                and not snapshot.get("margin_locked", False)
            ):
                return "OK"
            return f"LOCKED ({snapshot['lock_state']})"

        except Exception:
            return "FAIL"

    # =====================================
    # GUARDS STATUS
    # =====================================
    def _guards_status(self):
        try:
            if self.kill_switch.triggered:
                return "BLOCKED (KILL SWITCH)"
            if self.execution_guard.pending_execution:
                return "BLOCKED (PENDING ORDER)"
            if not self.market_guard.can_trade():
                operator_reason = getattr(self.market_guard, "operator_reason", None)
                reason = operator_reason() if callable(operator_reason) else "MARKET"
                return f"BLOCKED ({reason or 'MARKET'})"
            return "OK"

        except Exception:
            return "BLOCKED (CHECK FAILED)"

    # =====================================
    # OVERALL STATUS
    # =====================================
    def _overall_status(self):
        if self.kill_switch.triggered:
            return "ISSUE"

        if self._connection_status() != "OK":
            return "ISSUE"

        if self._sync_status() != "OK":
            return "ISSUE"

        _, pos_status = self._position_status()
        if pos_status == "DESYNC":
            return "ISSUE"

        return "HEALTHY"

    # =====================================
    # BUILD STATUS MESSAGE (single source)
    # =====================================
    def _build_status_message(self):
        broker_time = self.broker.broker_now()
        clock_suffix = self._clock_suffix()
        positions_count, positions_status = self._position_status()

        strategy_name = getattr(
            self.strategy_config,
            "STRATEGY_NAME",
            "UNKNOWN_STRATEGY"
        )

        strategy_id = getattr(self.trade_logger, "strategy_id", None)
        if not isinstance(strategy_id, str) or not strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")

        daily_strategy_pnl = self.trade_logger.get_daily_strategy_pnl(
            strategy_id,
            broker_time
        )

        weekly_strategy_pnl = self.trade_logger.get_weekly_strategy_pnl(
            strategy_id,
            broker_time
        )

        account_id = getattr(self.strategy_config, "ACCOUNT", "hub_demo")
        daily_account_pnl = self.trade_logger.get_daily_account_pnl(account_id, broker_time)

        weekly_account_pnl = self.trade_logger.get_weekly_account_pnl(account_id, broker_time)

        # Optional account-monitor line. Omitted entirely when
        # no monitor is wired in (keeps legacy heartbeat output unchanged).
        monitor_line = ""
        if self.account_monitor is not None:
            snap = self.account_monitor.status_snapshot()
            peak = snap.get("peak_equity")
            last = snap.get("last_equity")
            if peak and last:
                dd_pct = (peak - last) / peak * 100.0
                monitor_line = (
                    f"\nAccount equity: peak={peak:.2f} | "
                    f"now={last:.2f} | DD={dd_pct:.2f}%"
                    + (" | HALTED" if snap.get("halted") else "")
                    + "\n"
                )

        return (
            f"Bot: {strategy_name}\n"
            f"[{broker_time.strftime('%H:%M:%S')} UTC{clock_suffix} | "
            f"Uptime: {self._format_uptime()}]\n\n"

            f"Connection: {self._connection_status()} | "
            f"Sync: {self._sync_status()}\n"

            f"Positions: {positions_count} | "
            f"Managed: {positions_status} | "
            f"Stats: {self._stats_status()}\n\n"

            f"Strategy PnL:\n"
            f"Today: {daily_strategy_pnl:+.2f}$ | "
            f"Week: {weekly_strategy_pnl:+.2f}$\n\n"

            f"Account PnL:\n"
            f"Today: {daily_account_pnl:+.2f}$ | "
            f"Week: {weekly_account_pnl:+.2f}$\n\n"

            f"Risk: {self._risk_status()}\n"
            f"Guards: {self._guards_status()}\n"
            f"Overall system: {self._overall_status()}"
        )

    def build_strategy_status_message(self):
        """Compact strategy-chat heartbeat used by the hub runtime."""
        broker_time = self.broker.broker_now()
        clock_suffix = self._clock_suffix()
        positions_count, positions_status = self._position_status()
        strategy_name = getattr(self.strategy_config, "STRATEGY_NAME", "UNKNOWN_STRATEGY")
        strategy_id = getattr(self.trade_logger, "strategy_id", None)
        if not isinstance(strategy_id, str) or not strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        daily_pnl = self.trade_logger.get_daily_strategy_pnl(strategy_id, broker_time)
        weekly_pnl = self.trade_logger.get_weekly_strategy_pnl(strategy_id, broker_time)
        stats = self._stats_status()
        return (
            f"Bot: {strategy_name}\n"
            f"[{broker_time.strftime('%H:%M:%S')} UTC{clock_suffix} | Uptime: {self._format_uptime()}]\n\n"
            f"Connection: {self._connection_status()} | Sync: {self._sync_status()}\n"
            f"Positions: {positions_count} | Managed: {positions_status} | Stats: {stats}\n\n"
            f"Strategy PnL:\n"
            f"Today: {daily_pnl:+.2f}$ | Week: {weekly_pnl:+.2f}$\n\n"
            f"Risk: {self._risk_status()}\n"
            f"Guards: {self._guards_status()}"
        )

    def _clock_suffix(self):
        clock = getattr(self.broker, "clock", None)
        snapshot = clock.status_snapshot() if clock is not None else {}
        offset = snapshot.get("offset_hours")
        return "" if offset is None else f" | Broker offset: {offset:+g}h"

    # =====================================
    # TELEGRAM HEARTBEAT
    # =====================================
    def send_status_to_telegram(self):
        msg = self._build_status_message()

        self.alerts.alert_heartbeat(msg)

    # =====================================
    # PRINT
    # =====================================
    def print_status(self):
        
        if self.trade_reconciliation is not None:
            try:
                self.trade_reconciliation.reconcile()
            except Exception as e:
                self._log(f"Reconciliation failed: {e}")

        msg = self._build_status_message()

        print("")
        print(msg)

        if self.alerts.telegram_trade_alerts_only:
            self.send_status_to_telegram()
