class KillSwitch:
    def __init__(
        self,
        alerts,
        strategy_name,
        max_desync_failures=2,
        max_broker_failures=3
    ):
        self.alerts = alerts
        self.strategy_name = strategy_name

        self.triggered = False
        self.reason = None

        self.desync_failures = 0
        self.broker_failures = 0

        self.max_desync_failures = max_desync_failures
        self.max_broker_failures = max_broker_failures

    # =====================================
    # LOG
    # =====================================
    def _log(self, message):
        if self.alerts:
            self.alerts.send_info(
                f"[KILL_SWITCH] {message}"
            )
        else:
            print(message)

    # =====================================
    # INTERNAL ALERT
    # =====================================
    def _send_alert(self):
        self.alerts.alert_system_issue(
            strategy_name=self.strategy_name,
            reason=self.reason
        )

    # =====================================
    # TRIGGER
    # =====================================
    def trigger(self, reason):
        if self.triggered:
            return

        self.triggered = True
        self.reason = reason

        self._log(
            f"KILL SWITCH TRIGGERED | reason={reason}"
        )

        self._send_alert()

    # =====================================
    # DESYNC FAILURES
    # =====================================
    def register_desync(self):
        self.desync_failures += 1

        self._log(
            f"Desync failure "
            f"{self.desync_failures}/"
            f"{self.max_desync_failures}"
        )

        if self.desync_failures >= self.max_desync_failures:
            self.trigger("REPEATED POSITION DESYNC")

    def reset_desync(self):
        if self.desync_failures > 0:
            self._log("Desync counter reset")

        self.desync_failures = 0

    # =====================================
    # BROKER FAILURES
    # =====================================
    def register_broker_failure(self):
        self.broker_failures += 1

        self._log(
            f"Broker failure "
            f"{self.broker_failures}/"
            f"{self.max_broker_failures}"
        )

        if self.broker_failures >= self.max_broker_failures:
            self.trigger("REPEATED BROKER FAILURES")

    def reset_broker_failures(self):
        if self.broker_failures > 0:
            self._log("Broker failure counter reset")

        self.broker_failures = 0

    # =====================================
    # FLATTEN ALL POSITIONS ON THE ACCOUNT
    # =====================================
    def flatten_all(self, broker, position_manager=None,
                    reason="ACCOUNT_PROTECTION", quiet=False):
        """
        Close EVERY open position on the login, regardless of symbol or
        magic.

        Used by AccountMonitor when the account breaches a DD / profit
        rule. At that point ANY open position is unwanted risk, including
        manual positions or positions from other bots on the same login.

        The legacy position manager is deliberately not used: it is scoped
        to a strategy symbol/magic.  AccountPositionService closes broker
        tickets with each position's own symbol and reports any residual
        exposure after a fresh account-wide query.
        """
        from core.account_position_service import AccountPositionService

        result = AccountPositionService(broker).flatten_account(reason)
        if result.is_flat:
            if not quiet:
                self._log(
                    f"FLATTEN COMPLETE | closed={result.closed_tickets} | "
                    f"already_closed={result.already_closed_tickets} | "
                    f"reason={reason}"
                )
            return result

        message = (
            f"INCOMPLETE ACCOUNT FLATTEN | reason={reason} | "
            f"remaining={result.remaining_tickets} | "
            f"failed={result.failed_tickets} | "
            f"verification_error={result.verification_error} | "
            f"MANUAL INTERVENTION REQUIRED"
        )
        if not quiet:
            self._log(message)
        if self.alerts and not quiet:
            self.alerts.send_critical(f"[KILL_SWITCH] {message}")
        return result

    # =====================================
    # STATE
    # =====================================
    def can_trade(self):
        return not self.triggered

    def status_snapshot(self):
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "desync_failures": self.desync_failures,
            "broker_failures": self.broker_failures
        }

    # =====================================
    # MANUAL RESET
    # =====================================
    def reset(self):
        self.triggered = False
        self.reason = None

        self.desync_failures = 0
        self.broker_failures = 0

        self._log("Kill switch manually reset")
