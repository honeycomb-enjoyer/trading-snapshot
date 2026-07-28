# guards/recovery_guard.py

class RecoveryGuard:
    def __init__(
        self,
        broker,
        position_manager,
        state_manager,
        kill_switch,
        alerts,
        strategy_config
    ):
        self.broker = broker
        self.position_manager = position_manager
        self.state_manager = state_manager
        self.kill_switch = kill_switch
        self.alerts = alerts
        self.strategy_config = strategy_config

    # =====================================
    # STARTUP RECOVERY CHECK
    # =====================================
    def run_startup_recovery(self):
        magic = self.strategy_config.MAGIC

        position = self.position_manager.get_position(magic)

        if position is None:
            return True

        ticket = position.ticket

        self.alerts.send_warning(
            f"[{self.strategy_config.STRATEGY_NAME}]\n"
            f"RECOVERY MODE\n\n"
            f"Detected existing position\n"
            f"Ticket: {ticket}"
        )

        # ===============================
        # HARD POSITION VALIDATION
        # ===============================
        sl_missing = position.sl is None or position.sl == 0
        tp_missing = position.tp is None or position.tp == 0

        if sl_missing or tp_missing:
            self.alerts.send_warning(
                f"[{self.strategy_config.STRATEGY_NAME}]\n"
                f"POSITION INCOMPLETE\n\n"
                f"SL missing: {sl_missing}\n"
                f"TP missing: {tp_missing}\n"
                f"Attempting recovery..."
            )

            recovered = self.position_manager.recover_position_management(
                magic
            )

            if not recovered:
                self.kill_switch.trigger(
                    "Recovery failed: cannot restore SL/TP"
                )

                self.alerts.send_critical(
                    f"[{self.strategy_config.STRATEGY_NAME}]\n"
                    f"RECOVERY FAILED\n\n"
                    f"Could not restore position management"
                )
                return False

            self.alerts.send_info(
                f"[{self.strategy_config.STRATEGY_NAME}]\n"
                f"RECOVERY SUCCESS\n\n"
                f"SL/TP restored"
            )

        # ===============================
        # EXECUTION CACHE CHECK
        # ===============================
        execution_cache = self.state_manager.get_execution_cache(ticket)

        if execution_cache is None:
            self.alerts.send_warning(
                f"[{self.strategy_config.STRATEGY_NAME}]\n"
                f"MISSING EXECUTION CACHE\n\n"
                f"Ticket: {ticket}\n"
                f"Continuing with reconstructed state"
            )
        else:
            required_keys = [
                "expected_entry_price",
                "actual_entry_price",
                "risk_usd"
            ]

            missing_keys = [
                key for key in required_keys
                if key not in execution_cache
            ]

            if missing_keys:
                self.alerts.send_warning(
                    f"[{self.strategy_config.STRATEGY_NAME}]\n"
                    f"PARTIAL CACHE\n\n"
                    f"Missing: {missing_keys}"
                )

        # ===============================
        # BREAKEVEN FLAG CHECK
        # ===============================
        strategy_state = self.state_manager.get_strategy()

        if "breakeven_done" not in strategy_state:
            strategy_state["breakeven_done"] = False
            self.state_manager.save()

        self.alerts.send_info(
            f"[{self.strategy_config.STRATEGY_NAME}]\n"
            f"RECOVERY COMPLETE\n\n"
            f"Ticket: {ticket}\n"
            f"System synchronized"
        )

        return True