"""Account-level drawdown and profit protection backed by SQLite state."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from accounts import consume_startup_reset_flag
from risk.account_state_store import AccountStateStore, AccountStateStoreError


BREACH_ALERT_COOLDOWN_SEC = 300


class AccountMonitor:
    """Evaluate account-level limits without sharing mutable JSON between bots."""

    def __init__(
        self,
        account: str,
        risk_rules: dict[str, Any],
        broker: Any,
        position_manager: Any,
        kill_switch: Any,
        alerts: Any,
        state_store: Optional[AccountStateStore] = None,
        reset_flag_consumer: Optional[Callable[[str, str], bool]] = None,
        now_fn=time.time,
    ) -> None:
        self.account = account
        self.risk_rules = risk_rules
        self.broker = broker
        self.position_manager = position_manager
        self.kill_switch = kill_switch
        self.alerts = alerts
        self._now = now_fn
        self.store = state_store or AccountStateStore(account)
        self._consume_reset_flag = reset_flag_consumer or consume_startup_reset_flag
        self._last_alert_at: dict[str, float] = {}
        self.state = self._empty_state()
        self.halt_health = "RUNNING"

        # Do not silently bootstrap when an existing DB is corrupt. Startup
        # stores the failure and check() will fail closed before any order path.
        try:
            self.state = self.store.read_state() or self._empty_state()
        except AccountStateStoreError:
            self.state = self._empty_state(halted=True)

    @property
    def max_dd_percent(self):
        return self.risk_rules.get("max_dd_percent")

    @property
    def hard_dd_percent(self):
        return self.risk_rules.get("hard_dd_percent")

    @property
    def profit_target_percent(self):
        return self.risk_rules.get("profit_target_percent")

    @property
    def profit_warning_percent(self):
        return self.risk_rules.get("profit_warning_percent")

    @property
    def check_interval_sec(self):
        return self.risk_rules.get("check_interval_sec") or 5

    def perform_startup_reset(self) -> bool:
        """Apply an explicitly requested reset before recovery or new orders.

        Position lookup deliberately uses the fail-closed account-wide broker
        method.  A symbol-filtered query or an unknown broker response can
        never be interpreted as a flat hub.
        """
        if self.risk_rules.get("reset_state_on_startup") is False:
            return True

        reset_token = self.risk_rules.get("_reset_request_token") or f"manual-{self.account}"

        try:
            positions = self.broker.list_all_positions()
        except Exception:
            return self._abort_startup_reset("cannot verify that the hub is flat")
        if positions:
            return self._abort_startup_reset("hub has open positions")

        equity = self.broker.account_equity()
        if equity is None or equity <= 0:
            return self._abort_startup_reset("broker equity is unavailable")
        balance = self.broker.account_balance()
        if balance is not None and balance <= 0:
            balance = None

        try:
            self.state, performed = self.store.reset_if_new(
                reset_token,
                "startup reset flag",
                equity,
                configured_starting_equity=self.risk_rules.get("starting_equity"),
                broker_balance=balance,
            )
        except (AccountStateStoreError, ValueError):
            return self._abort_startup_reset("state reset could not be persisted")

        try:
            consumed = self._consume_reset_flag(self.account, reset_token)
        except OSError:
            consumed = False
        if not consumed:
            return self._abort_startup_reset("could not automatically clear reset flag")
        self.risk_rules["reset_state_on_startup"] = False
        if performed:
            self._log("startup reset applied; reset flag cleared")
        return True

    def check(self) -> dict[str, Any]:
        """Update state and apply account risk policy; state failure blocks trade."""
        try:
            self.state = self.store.read_state() or self._empty_state()
        except AccountStateStoreError as error:
            return self._fail_closed(error)

        if self.state["halted"]:
            self._propagate_halt(self.state.get("halt_reason"))
            return self._recover_persisted_halt()

        equity = self.broker.account_equity()
        if equity is None or equity <= 0:
            return self._result("OK", None, equity=None)

        configured_starting = self.risk_rules.get("starting_equity")
        if configured_starting is None:
            balance = self.broker.account_balance()
            configured_starting = balance if balance and balance > 0 else equity

        try:
            self.state = self.store.record_equity(equity, configured_starting)
        except AccountStateStoreError as error:
            return self._fail_closed(error)

        peak = self.state["peak_equity"]
        starting = self.state["starting_equity"]
        dd_percent = (peak - equity) / peak * 100.0 if peak else 0.0
        profit_percent = (
            (equity - starting) / starting * 100.0 if starting else 0.0
        )
        breach = self._evaluate_breach(dd_percent, profit_percent)

        if breach is None:
            return self._result("OK", None, dd_percent, profit_percent)
        if breach == "PROFIT_WARNING":
            self._alert(breach, dd_percent, profit_percent)
            return self._result("WARN", breach, dd_percent, profit_percent)

        if not self._handle_breach(breach, dd_percent, profit_percent):
            return self._result("HALT", "STATE_STORE_UNAVAILABLE")
        return self._result("HALT", breach, dd_percent, profit_percent)

    def status_snapshot(self) -> dict[str, Any]:
        try:
            self.state = self.store.read_state() or self._empty_state()
        except AccountStateStoreError as error:
            self._fail_closed(error)
        return {
            "account": self.account,
            "peak_equity": self.state.get("peak_equity"),
            "starting_equity": self.state.get("starting_equity"),
            "last_equity": self.state.get("last_equity"),
            "halted": self.state.get("halted", False),
            "halt_reason": self.state.get("halt_reason"),
            "halt_health": self.halt_health,
            "max_dd_percent": self.max_dd_percent,
            "profit_target_percent": self.profit_target_percent,
        }

    def _evaluate_breach(self, dd_percent: float, profit_percent: float) -> Optional[str]:
        if self.hard_dd_percent is not None and dd_percent >= self.hard_dd_percent:
            return "HARD_DD_BREACH"
        if self.max_dd_percent is not None and dd_percent >= self.max_dd_percent:
            return "DD_BREACH"
        if (
            self.profit_target_percent is not None
            and profit_percent >= self.profit_target_percent
        ):
            return "PROFIT_TARGET"
        if (
            self.profit_warning_percent is not None
            and profit_percent >= self.profit_warning_percent
        ):
            return "PROFIT_WARNING"
        return None

    def _handle_breach(self, breach: str, dd_percent: float, profit_percent: float) -> bool:
        try:
            self.state, newly_halted = self.store.halt(breach)
        except AccountStateStoreError as error:
            self._fail_closed(error)
            return False

        if not newly_halted:
            self._propagate_halt(self.state.get("halt_reason"))
            return True

        self.kill_switch.trigger(f"ACCOUNT_{breach} ({self.account})")
        try:
            flatten_result = self.kill_switch.flatten_all(
                broker=self.broker,
                position_manager=self.position_manager,
                reason=f"ACCOUNT_{breach}",
            )
            self.halt_health = (
                "HALTED_FLAT" if flatten_result is not None and flatten_result.is_flat
                else "HALTED_DEGRADED"
            )
        except Exception as error:
            self.halt_health = "HALTED_DEGRADED"
            self._log(f"flatten_all raised during {breach}: {error}", critical=True)
        self._alert(breach, dd_percent, profit_percent)
        return True

    def recover_persisted_halt(self) -> dict[str, Any]:
        """Re-assert account-wide flatten after a persisted halt/restart.

        A halt is not proof that the previous process reached flatten.  Every
        survivor therefore verifies all account tickets and repeats the
        idempotent close path.  Unknown verification stays degraded.
        """
        try:
            self.state = self.store.read_state() or self._empty_state()
        except AccountStateStoreError as error:
            return self._fail_closed(error)
        if not self.state["halted"]:
            self.halt_health = "RUNNING"
            return self._result("OK", None)
        self._propagate_halt(self.state.get("halt_reason"))
        return self._recover_persisted_halt()

    def _recover_persisted_halt(self) -> dict[str, Any]:
        try:
            result = self.kill_switch.flatten_all(
                broker=self.broker,
                position_manager=self.position_manager,
                reason="ACCOUNT_HALTED_RECOVERY",
                quiet=True,
            )
        except Exception as error:
            result = None
            detail = f"flatten raised: {error}"
        else:
            if result is not None:
                detail = (
                    f"remaining={result.remaining_tickets}; "
                    f"verification_error={result.verification_error}"
                )
            else:
                detail = "flatten returned no verification result"
        if result is not None and result.is_flat:
            self.halt_health = "HALTED_FLAT"
            return self._result("HALT", self.state.get("halt_reason"))
        self.halt_health = "HALTED_DEGRADED"
        self._alert("HALT_RECOVERY_DEGRADED", None, None, reason=detail)
        return self._result("HALT_DEGRADED", self.state.get("halt_reason"))

    def _propagate_halt(self, reason: Optional[str]) -> None:
        if not self.kill_switch.triggered:
            self.kill_switch.trigger(
                f"ACCOUNT_HALTED_BY_SIBLING ({self.account}, {reason})"
            )
            self._alert("HALT_PROPAGATED", None, None, reason=reason)

    def _fail_closed(self, error: BaseException) -> dict[str, Any]:
        self.state = self._empty_state(halted=True)
        self.state["halt_reason"] = "STATE_STORE_UNAVAILABLE"
        if not self.kill_switch.triggered:
            self.kill_switch.trigger(f"ACCOUNT_STATE_STORE_UNAVAILABLE ({self.account})")
            self._log(
                f"state unavailable; new trading blocked: {error}", critical=True
            )
        return self._result("HALT", "STATE_STORE_UNAVAILABLE")

    def _abort_startup_reset(self, detail: str) -> bool:
        if not self.kill_switch.triggered:
            self.kill_switch.trigger(f"ACCOUNT_RESET_BLOCKED ({self.account})")
        self._log(f"startup reset blocked: {detail}", critical=True)
        return False

    def _alert(
        self,
        breach: str,
        dd_percent: Optional[float],
        profit_percent: Optional[float],
        *,
        reason: Optional[str] = None,
    ) -> None:
        now = self._now()
        if now - self._last_alert_at.get(breach, 0.0) < BREACH_ALERT_COOLDOWN_SEC:
            return
        self._last_alert_at[breach] = now

        if breach in {"HARD_DD_BREACH", "DD_BREACH"}:
            message = (
                f"ACCOUNT BREACH - {breach}; account={self.account}; "
                f"dd={dd_percent:.2f}%; positions are being flattened."
            )
            self._log(message, critical=True)
        elif breach == "PROFIT_TARGET":
            self._log(
                f"PROFIT TARGET - account={self.account}; profit={profit_percent:.2f}%; "
                "positions are being flattened.",
                critical=True,
            )
        elif breach == "PROFIT_WARNING":
            self._log(
                f"Profit warning - account={self.account}; profit={profit_percent:.2f}%.",
            )
        elif breach == "HALT_PROPAGATED":
            self._log(
                f"Account halted by sibling; account={self.account}; reason={reason}.",
                critical=True,
            )
        elif breach == "HALT_RECOVERY_DEGRADED":
            self._log(
                f"Persisted halt recovery is degraded; account={self.account}; {reason}; "
                "MANUAL INTERVENTION REQUIRED",
                critical=True,
            )

    def _log(self, message: str, *, critical: bool = False) -> None:
        prefix = f"[ACCOUNT_MONITOR {self.account}] {message}"
        print(prefix)
        if not self.alerts:
            return
        try:
            if critical:
                self.alerts.send_critical(prefix)
            else:
                self.alerts.send_info(prefix)
        except Exception:
            # Alerting is never part of the execution safety boundary.
            pass

    def _result(
        self,
        action: str,
        breach: Optional[str],
        dd_percent: Optional[float] = None,
        profit_percent: Optional[float] = None,
        equity: Optional[float] = None,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "type": breach,
            "account": self.account,
            "dd_percent": dd_percent,
            "profit_percent": profit_percent,
            "equity": equity if equity is not None else self.state.get("last_equity"),
            "peak_equity": self.state.get("peak_equity"),
            "starting_equity": self.state.get("starting_equity"),
            "halted": self.state.get("halted", False),
            "halt_health": self.halt_health,
        }

    def _empty_state(self, *, halted: bool = False) -> dict[str, Any]:
        return {
            "account_id": self.account,
            "starting_equity": None,
            "peak_equity": None,
            "last_equity": None,
            "halted": halted,
            "halt_reason": None,
            "last_breach_at": None,
            "last_reset_id": None,
            "version": 0,
            "updated_at": None,
        }
