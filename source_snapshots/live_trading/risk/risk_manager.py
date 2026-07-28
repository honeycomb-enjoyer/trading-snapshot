# risk/risk_manager.py

from risk.position_sizer import PositionSizer
import math


class RiskManager:
    def __init__(
        self,
        broker,
        strategy_config,
        portfolio_config,
        trade_logger,
        alerts=None,
        account_monitor=None,
    ):
        self.broker = broker
        self.strategy_config = strategy_config
        self.portfolio_config = portfolio_config
        self.trade_logger = trade_logger
        # The registry key is the only storage/query identity.  The display
        # label on strategy_config is intentionally never used for ledger PnL.
        self.strategy_id = getattr(trade_logger, "strategy_id", None)
        self._lock_state = "RUNNING"
        self._announced_block_reasons = set()
        self._last_position_risk = None

        # Alerts are optional. Runners still construct RiskManager without
        # them (signature stays backward compatible). When provided,
        # SL-guard warnings also go to Telegram; the [SL GUARD] print runs
        # unconditionally (on the block path in can_open_new_trade) so the
        # reason is always in the console/log file regardless of cooldowns
        # or wiring.
        self.alerts = alerts
        self.account_monitor = account_monitor

        self.position_sizer = PositionSizer(
            broker=broker,
            risk_buffer=strategy_config.RISK_BUFFER,
            allow_undersized_lot=strategy_config.ALLOW_UNDERSIZED_LOT,
            # Optional per-symbol lot ceiling. Strategies may define
            # MAX_LOT as a hard safety net (None = no ceiling).
            max_lot=getattr(strategy_config, "MAX_LOT", None),
        )

    # =====================================
    # CAN OPEN TRADE?
    # =====================================
    def can_open_new_trade(self):
        account_guard = self._check_account_halt()
        if account_guard is not None:
            self._emit_block_warning(account_guard)
            return False, account_guard

        positions_guard = self._check_open_positions_limit()
        if positions_guard is not None:
            self._emit_block_warning(positions_guard)
            return False, positions_guard

        margin_guard = self._check_current_margin_utility()
        if margin_guard is not None:
            self._emit_block_warning(margin_guard)
            return False, margin_guard

        try:
            daily_pnl, weekly_pnl = self._strategy_pnls()
        except RuntimeError as error:
            reason = str(error)
            self._emit_block_warning(reason)
            return False, reason

        daily_limit = self.strategy_config.DAILY_SL_LIMIT_USD
        weekly_limit = self.strategy_config.WEEKLY_SL_LIMIT_USD
        risk_per_trade = getattr(
            self.strategy_config, "RISK_PER_TRADE_USD", None
        )

        daily_guard = self._check_sl_guard(
            pnl=daily_pnl,
            limit=daily_limit,
            risk_per_trade=risk_per_trade,
            period="daily"
        )
        if daily_guard["blocked"]:
            self._transition_strategy_lock(daily_guard)
            return False, daily_guard["reason"]

        weekly_guard = self._check_sl_guard(
            pnl=weekly_pnl,
            limit=weekly_limit,
            risk_per_trade=risk_per_trade,
            period="weekly"
        )
        if weekly_guard["blocked"]:
            self._transition_strategy_lock(weekly_guard)
            return False, weekly_guard["reason"]

        self._lock_state = "RUNNING"
        self._announced_block_reasons.clear()
        return True, None

    def _strategy_pnls(self):
        if not isinstance(self.strategy_id, str) or not self.strategy_id:
            # Falling back to STRATEGY_NAME would recreate the identity bug:
            # it is a display label and may differ in case or wording.
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        broker_now = self.broker.broker_now()
        return (
            self.trade_logger.get_daily_strategy_pnl(self.strategy_id, broker_now),
            self.trade_logger.get_weekly_strategy_pnl(self.strategy_id, broker_now),
        )

    def _check_account_halt(self):
        """Read the shared halt state immediately before every new order."""
        if self.account_monitor is None:
            return None
        try:
            status = self.account_monitor.status_snapshot()
        except Exception:
            return "ACCOUNT_STATE_UNAVAILABLE"
        if status.get("halted"):
            return "ACCOUNT_HALTED"
        return None

    def _check_open_positions_limit(self):
        """Enforce the account-wide position cap; unknown exposure blocks."""
        if self.portfolio_config is None:
            return None
        limit = getattr(self.portfolio_config, "MAX_OPEN_POSITIONS", None)
        if limit is None:
            return None
        if not isinstance(limit, int) or limit < 0:
            return "CONFIG_INVALID_MAX_OPEN_POSITIONS"
        if not hasattr(self.broker, "list_all_positions"):
            return "POSITION_QUERY_UNAVAILABLE"
        try:
            positions = self.broker.list_all_positions()
        except Exception:
            return "POSITION_QUERY_UNAVAILABLE"
        if len(positions) >= limit:
            return "MAX_OPEN_POSITIONS_REACHED"
        return None

    def _margin_policy(self):
        if self.portfolio_config is None:
            return None
        maximum = getattr(self.portfolio_config, "MAX_MARGIN_UTILIZATION", None)
        if maximum is None:
            return None
        stress = getattr(self.portfolio_config, "MARGIN_STRESS_STOP_MULTIPLIER", None)
        buffer = getattr(self.portfolio_config, "MARGIN_ESTIMATE_BUFFER", None)
        values = (maximum, stress, buffer)
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ) or maximum >= 1 or buffer < 1:
            return {"valid": False, "reason": "CONFIG_INVALID_MARGIN_POLICY"}
        return {
            "valid": True,
            "max_utilization": float(maximum),
            "stress_multiplier": float(stress),
            "estimate_buffer": float(buffer),
        }

    def _check_current_margin_utility(self):
        policy = self._margin_policy()
        if policy is None:
            return None
        if not policy["valid"]:
            return policy["reason"]
        snapshot_fn = getattr(self.broker, "account_margin_snapshot", None)
        if not callable(snapshot_fn):
            return "MARGIN_DATA_UNAVAILABLE"
        snapshot = snapshot_fn()
        if not isinstance(snapshot, dict):
            return "MARGIN_DATA_UNAVAILABLE"
        equity = snapshot.get("equity")
        margin = snapshot.get("margin")
        if not self._finite_nonnegative(equity, positive=True) or not self._finite_nonnegative(margin):
            return "MARGIN_DATA_INVALID"
        utilization = float(margin) / float(equity)
        if utilization > policy["max_utilization"]:
            return "MARGIN_UTILIZATION_LIMIT"
        return None

    def _emit_block_warning(self, reason):
        if reason in self._announced_block_reasons:
            return
        self._announced_block_reasons.add(reason)
        message = f"[RISK GUARD] BLOCK ({reason})"
        print(message)
        if self.alerts is None:
            return
        try:
            if hasattr(self.alerts, "send_throttled_warning"):
                self.alerts.send_throttled_warning(
                    key=f"risk_guard_{reason}", message=message,
                )
            else:
                self.alerts.send_warning(message)
        except Exception as error:
            print(f"[RISK GUARD] alert delivery failed: {error}")

    def _transition_strategy_lock(self, guard):
        """Announce a daily/weekly lock exactly once per transition.

        This method belongs only to the order-capable path.  Status and
        heartbeat code use ``status_snapshot`` and must remain read-only.
        """
        state = "DAILY_LOCKED" if guard["period"] == "daily" else "WEEKLY_LOCKED"
        if self._lock_state == state:
            return
        self._lock_state = state
        self._emit_sl_warning(guard, state)

    def validate_open_position(self, position, execution_context=None):
        """Validate broker position invariants without changing strategy logic.

        The result is machine-readable so the runner can route a desync as a
        safety event.  A break-even SL on the profitable side remains valid:
        its remaining downside is zero.
        """
        if position is None:
            return True, None
        if getattr(position, "symbol", None) != self.strategy_config.SYMBOL:
            return False, "POSITION_SYMBOL_MISMATCH"

        raw_side = getattr(position, "type", None)
        side_map = {0: "BUY", 1: "SELL", "BUY": "BUY", "SELL": "SELL"}
        side = side_map.get(raw_side)
        if side is None:
            return False, "POSITION_SIDE_INVALID"

        entry = getattr(position, "price_open", None)
        sl = getattr(position, "sl", None)
        tp = getattr(position, "tp", None)
        volume = getattr(position, "volume", None)
        tp_required = getattr(self.strategy_config, "TAKE_PROFIT_MODEL", None) != "NONE"
        values = (entry, sl, volume) if not tp_required else (entry, sl, tp, volume)
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            return False, "POSITION_INVALID_NUMERIC_FIELD"
        if entry <= 0 or sl <= 0 or volume <= 0 or (tp_required and tp <= 0):
            return False, "POSITION_SL_TP_REQUIRED"
        if tp_required:
            protection_direction_valid = (
                tp > entry and sl < tp
                if side == "BUY"
                else tp < entry and sl > tp
            )
        else:
            protection_direction_valid = sl < entry if side == "BUY" else sl > entry
        if not protection_direction_valid:
            return False, "POSITION_SL_TP_DIRECTION_INVALID"

        downside_distance = max(entry - sl, 0.0) if side == "BUY" else max(sl - entry, 0.0)
        if downside_distance == 0:
            return True, None
        try:
            symbol_info = self.broker.get_symbol_info()
            risk_per_lot, _, _ = self.position_sizer._risk_per_lot(
                entry, sl, symbol_info.trade_tick_size,
                symbol_info.trade_tick_value, symbol_info,
            )
        except Exception:
            return False, "POSITION_RISK_DISTANCE_UNAVAILABLE"
        if risk_per_lot <= 0:
            return False, "POSITION_RISK_DISTANCE_INVALID"
        max_risk = getattr(self.strategy_config, "RISK_PER_TRADE_USD", None)
        if not isinstance(max_risk, (int, float)) or max_risk <= 0:
            return False, "POSITION_RISK_LIMIT_INVALID"
        tolerance_r = getattr(
            self.portfolio_config, "POSITION_RISK_VALIDATION_TOLERANCE_R", 0.0
        ) if self.portfolio_config is not None else 0.0
        if (
            not isinstance(tolerance_r, (int, float))
            or not math.isfinite(tolerance_r)
            or tolerance_r < 0
        ):
            return False, "POSITION_RISK_TOLERANCE_INVALID"
        actual_risk = risk_per_lot * volume
        self._last_position_risk = {
            "actual_risk_usd": actual_risk,
            "configured_risk_usd": float(max_risk),
            "planned_risk_usd": None,
        }
        if actual_risk > max_risk * (1 + tolerance_r + 1e-6):
            if self._risk_excess_is_entry_slippage(
                position=position,
                side=side,
                actual_risk=actual_risk,
                max_risk=float(max_risk),
                base_tolerance_r=float(tolerance_r),
                execution_context=execution_context,
                symbol_info=symbol_info,
            ):
                return True, "POSITION_RISK_ELEVATED_BY_SLIPPAGE"
            return False, "POSITION_RISK_EXCEEDS_LIMIT"
        return True, None

    def _risk_excess_is_entry_slippage(
        self, *, position, side, actual_risk, max_risk, base_tolerance_r,
        execution_context, symbol_info,
    ):
        if not isinstance(execution_context, dict):
            return False
        slippage_tolerance_r = getattr(
            self.portfolio_config, "POSITION_RISK_SLIPPAGE_TOLERANCE_R", 0.0
        ) if self.portfolio_config is not None else 0.0
        if (
            not isinstance(slippage_tolerance_r, (int, float))
            or isinstance(slippage_tolerance_r, bool)
            or not math.isfinite(slippage_tolerance_r)
            or slippage_tolerance_r < 0
        ):
            return False

        reference_entry = execution_context.get("requested_entry_price")
        if reference_entry is None:
            reference_entry = execution_context.get("expected_entry_price")
        cached_actual = execution_context.get("actual_entry_price")
        initial_sl = execution_context.get("initial_sl")
        planned_risk = execution_context.get("planned_risk_usd")
        if planned_risk is None:
            planned_risk = execution_context.get("risk_usd")
        recorded_slippage = execution_context.get("entry_slippage")
        numeric = (
            reference_entry, cached_actual, initial_sl, planned_risk,
            recorded_slippage,
        )
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in numeric
        ):
            return False

        point = getattr(symbol_info, "point", None)
        if not self._finite_nonnegative(point, positive=True):
            point = getattr(symbol_info, "trade_tick_size", None)
        if not self._finite_nonnegative(point, positive=True):
            return False
        price_tolerance = float(point) * 2
        entry = float(position.price_open)
        if not math.isclose(entry, float(cached_actual), abs_tol=price_tolerance):
            return False
        if not math.isclose(float(position.sl), float(initial_sl), abs_tol=price_tolerance):
            # A widened/manual SL is a genuine protection change, not entry slippage.
            return False
        observed_slippage = abs(entry - float(reference_entry))
        if observed_slippage <= 0 or not math.isclose(
            observed_slippage, float(recorded_slippage), abs_tol=price_tolerance
        ):
            return False
        adverse = (
            entry > float(reference_entry) + price_tolerance
            if side == "BUY"
            else entry < float(reference_entry) - price_tolerance
        )
        if not adverse:
            return False
        if float(planned_risk) > max_risk * (1 + base_tolerance_r + 1e-6):
            return False
        if actual_risk > max_risk * (
            1 + base_tolerance_r + float(slippage_tolerance_r) + 1e-6
        ):
            return False
        self._last_position_risk["planned_risk_usd"] = float(planned_risk)
        return True

    def position_validation_warning(self, reason):
        if reason != "POSITION_RISK_ELEVATED_BY_SLIPPAGE":
            return reason
        risk = self._last_position_risk or {}
        return (
            "Position risk elevated by confirmed entry slippage; management remains active | "
            f"planned=${risk.get('planned_risk_usd', 0):.2f} | "
            f"actual=${risk.get('actual_risk_usd', 0):.2f} | "
            f"configured=${risk.get('configured_risk_usd', 0):.2f}"
        )

    def validate_margin_for_order(self, *, side, volume, entry, sl):
        """Fail closed when a new order would crowd margin near 2x stops."""
        policy = self._margin_policy()
        if policy is None:
            return {"valid": True, "reason": None, "disabled": True}
        if not policy["valid"]:
            self._emit_block_warning(policy["reason"])
            return policy

        snapshot_fn = getattr(self.broker, "account_margin_snapshot", None)
        margin_fn = getattr(self.broker, "estimate_order_margin", None)
        profit_fn = getattr(self.broker, "estimate_order_profit", None)
        if not all(callable(item) for item in (snapshot_fn, margin_fn, profit_fn)):
            result = {"valid": False, "reason": "MARGIN_DATA_UNAVAILABLE"}
            self._emit_block_warning(result["reason"])
            return result
        snapshot = snapshot_fn()
        if not isinstance(snapshot, dict):
            result = {"valid": False, "reason": "MARGIN_DATA_UNAVAILABLE"}
            self._emit_block_warning(result["reason"])
            return result
        balance = snapshot.get("balance")
        current_margin = snapshot.get("margin")
        if not self._finite_nonnegative(balance, positive=True) or not self._finite_nonnegative(current_margin):
            result = {"valid": False, "reason": "MARGIN_DATA_INVALID"}
            self._emit_block_warning(result["reason"])
            return result

        additional_margin = margin_fn(side, volume, entry, self.strategy_config.SYMBOL)
        if not self._finite_nonnegative(additional_margin):
            result = {"valid": False, "reason": "MARGIN_ESTIMATE_UNAVAILABLE"}
            self._emit_block_warning(result["reason"])
            return result

        try:
            positions = self.broker.list_all_positions()
        except Exception:
            positions = None
        if positions is None:
            result = {"valid": False, "reason": "POSITION_QUERY_UNAVAILABLE"}
            self._emit_block_warning(result["reason"])
            return result

        stress_profit = self._order_stress_profit(
            side=side, symbol=self.strategy_config.SYMBOL, volume=volume,
            entry=entry, sl=sl, multiplier=policy["stress_multiplier"],
        )
        if stress_profit is None:
            result = {"valid": False, "reason": "MARGIN_STRESS_UNAVAILABLE"}
            self._emit_block_warning(result["reason"])
            return result
        for position in positions:
            position_profit = self._position_stress_profit(
                position, policy["stress_multiplier"]
            )
            if position_profit is None:
                result = {
                    "valid": False,
                    "reason": "MARGIN_STRESS_UNPROTECTED_POSITION",
                }
                self._emit_block_warning(result["reason"])
                return result
            stress_profit += position_profit

        stressed_equity = float(balance) + float(stress_profit)
        projected_margin = (
            float(current_margin) + float(additional_margin)
        ) * policy["estimate_buffer"]
        if stressed_equity <= 0:
            result = {
                "valid": False, "reason": "MARGIN_STRESS_EQUITY_NONPOSITIVE",
                "stressed_equity": stressed_equity,
                "projected_margin": projected_margin,
            }
            self._emit_block_warning(result["reason"])
            return result
        utilization = projected_margin / stressed_equity
        result = {
            "valid": utilization <= policy["max_utilization"],
            "reason": None if utilization <= policy["max_utilization"] else "MARGIN_STRESS_LIMIT",
            "stressed_equity": round(stressed_equity, 2),
            "projected_margin": round(projected_margin, 2),
            "projected_utilization": round(utilization, 6),
            "max_utilization": policy["max_utilization"],
            "stress_multiplier": policy["stress_multiplier"],
        }
        if not result["valid"]:
            self._emit_block_warning(result["reason"])
        return result

    def _order_stress_profit(self, *, side, symbol, volume, entry, sl, multiplier):
        if side not in {"BUY", "SELL"}:
            return None
        if any(
            not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (volume, entry, sl)
        ) or volume <= 0 or entry <= 0 or sl <= 0:
            return None
        downside = max(entry - sl, 0.0) if side == "BUY" else max(sl - entry, 0.0)
        stress_price = entry - downside * multiplier if side == "BUY" else entry + downside * multiplier
        if stress_price <= 0:
            return None
        profit = self.broker.estimate_order_profit(
            side, symbol, volume, entry, stress_price
        )
        return None if profit is None else min(float(profit), 0.0)

    def _position_stress_profit(self, position, multiplier):
        side_map = {0: "BUY", 1: "SELL", "BUY": "BUY", "SELL": "SELL"}
        side = side_map.get(getattr(position, "type", None))
        symbol = getattr(position, "symbol", None)
        entry = getattr(position, "price_open", None)
        sl = getattr(position, "sl", None)
        volume = getattr(position, "volume", None)
        if side is None or not isinstance(symbol, str) or not symbol:
            return None
        return self._order_stress_profit(
            side=side, symbol=symbol, volume=volume, entry=entry, sl=sl,
            multiplier=multiplier,
        )

    @staticmethod
    def _finite_nonnegative(value, *, positive=False):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and (value > 0 if positive else value >= 0)
        )

    # =====================================
    # SL LIMIT GUARD
    # -------------------------------------
    # Pure check - no side effects. Shared by can_open_new_trade() (which
    # emits the warning) and status_snapshot() (which must NOT emit, since
    # heartbeat calls it every few seconds and would spam Telegram/console).
    #
    # The OLD logic only blocked when realized loss already hit the limit:
    #     abs(realized) >= limit
    # That allows opening a trade whose own stop would breach the limit
    # (e.g. realized=$98.79, limit=$100, risk_per_trade=$30 -> projected
    # $128.79 > $100). We now block when the PROJECTED loss (realized +
    # risk of the new trade) exceeds the limit.
    #
    # Returns a dict describing the decision:
    #   {
    #     "blocked": bool,
    #     "reason": str|None,         # "DAILY_LOSS_LIMIT_PROJECTED" / ...
    #     "period": "daily"|"weekly",
    #     "realized_loss": float,
    #     "potential": float,         # 0.0 on fallback path
    #     "projected": float,
    #     "limit": float,
    #     "fallback": bool            # True if RISK_PER_TRADE_USD was unset
    #   }
    # =====================================
    def _check_sl_guard(self, pnl, limit, risk_per_trade, period):
        period_tag = "DAILY" if period == "daily" else "WEEKLY"

        base = {
            "blocked": False,
            "reason": None,
            "period": period,
            "realized_loss": 0.0,
            "potential": 0.0,
            "projected": 0.0,
            "limit": 0.0,
            "tolerance": 0.0,
            "effective_limit": 0.0,
            "fallback": False,
        }

        # No limit configured -> nothing to enforce.
        if limit is None:
            return base

        limit_value = abs(limit)
        realized_loss = abs(min(pnl, 0.0))

        base["limit"] = limit_value
        base["realized_loss"] = realized_loss
        tolerance_name = (
            "DAILY_PROJECTED_LOSS_TOLERANCE_R"
            if period == "daily"
            else "WEEKLY_PROJECTED_LOSS_TOLERANCE_R"
        )
        tolerance_r = getattr(self.portfolio_config, tolerance_name, 0.0) \
            if self.portfolio_config is not None else 0.0
        if (
            not isinstance(tolerance_r, (int, float))
            or not math.isfinite(tolerance_r)
            or tolerance_r < 0
        ):
            base["blocked"] = True
            base["reason"] = f"CONFIG_INVALID_{period_tag}_LOSS_TOLERANCE"
            return base

        # If we don't know the per-trade risk, fall back to the legacy
        # realized-only check. Fallback flag lets the warning explain the
        # weaker guarantee (no lookahead protection).
        if risk_per_trade is None or risk_per_trade <= 0:
            base["fallback"] = True
            base["projected"] = realized_loss
            base["effective_limit"] = limit_value
            if realized_loss >= limit_value:
                base["blocked"] = True
                base["reason"] = f"{period_tag}_LOSS_LIMIT"
            return base

        projected_loss = realized_loss + risk_per_trade
        tolerance = float(risk_per_trade) * float(tolerance_r)
        effective_limit = limit_value + tolerance
        base["potential"] = float(risk_per_trade)
        base["projected"] = projected_loss
        base["tolerance"] = tolerance
        base["effective_limit"] = effective_limit

        # Strict ">" guards against float noise exactly on the boundary
        # (get_daily/weekly_strategy_pnl returns round(..., 2)).
        if projected_loss > effective_limit:
            base["blocked"] = True
            base["reason"] = f"{period_tag}_LOSS_LIMIT_PROJECTED"

        return base

    # =====================================
    # SL GUARD WARNING
    # -------------------------------------
    # Called only from can_open_new_trade() on the block path - never from
    # status_snapshot(). Always prints [SL GUARD] (matches the [LOTSIZE]
    # convention from position_sizer.py) so the reason survives in the log
    # file even when alerts is not wired or the Telegram cooldown (300s)
    # swallows repeats. Telegram route is best-effort and never raises.
    # =====================================
    def _emit_sl_warning(self, guard, state):
        period_tag = "DAILY" if guard["period"] == "daily" else "WEEKLY"
        fallback_tag = " [fallback: RISK_PER_TRADE_USD unset]" if guard["fallback"] else ""

        msg = (
            f"[SL GUARD] {period_tag} [RISK_EVENT] transition=RUNNING->{state} | "
            f"BLOCK ({guard['reason']}){fallback_tag} | "
            f"realized=${guard['realized_loss']:.2f} "
            f"+ potential=${guard['potential']:.2f} "
            f"= projected=${guard['projected']:.2f} "
            f"> allowed=${guard['effective_limit']:.2f} "
            f"(limit=${guard['limit']:.2f} + tolerance=${guard['tolerance']:.2f})"
        )

        print(msg)

        if self.alerts is not None:
            try:
                if hasattr(self.alerts, "send_throttled_warning"):
                    self.alerts.send_throttled_warning(
                        key=f"strategy_lock_{self.strategy_id}_{state}", message=msg,
                    )
                else:
                    self.alerts.send_warning(msg)
            except Exception as e:
                print(f"[SL GUARD] {period_tag} | alerts.send_warning failed: {e}")

    # =====================================
    # POSITION SIZE
    # =====================================
    def calculate_position_size(self, entry, sl):
        return self.position_sizer.calculate_lot(
            entry_price=entry,
            stop_price=sl,
            risk_usd=self.strategy_config.RISK_PER_TRADE_USD
        )

    # =====================================
    # STATUS SNAPSHOT
    # =====================================
    def status_snapshot(self):
        daily_pnl, weekly_pnl = self._strategy_pnls()

        daily_limit = self.strategy_config.DAILY_SL_LIMIT_USD
        weekly_limit = self.strategy_config.WEEKLY_SL_LIMIT_USD
        risk_per_trade = getattr(
            self.strategy_config, "RISK_PER_TRADE_USD", None
        )

        # Mirrors can_open_new_trade() via the same pure guard (no warning
        # emitted here - see _check_sl_guard docstring), so the heartbeat
        # "locked" flag cannot disagree with the actual block decision.
        daily_locked = self._check_sl_guard(
            pnl=daily_pnl,
            limit=daily_limit,
            risk_per_trade=risk_per_trade,
            period="daily"
        )["blocked"]

        weekly_locked = self._check_sl_guard(
            pnl=weekly_pnl,
            limit=weekly_limit,
            risk_per_trade=risk_per_trade,
            period="weekly"
        )["blocked"]

        margin_reason = self._check_current_margin_utility()
        margin_locked = margin_reason is not None
        margin_utilization = None
        snapshot_fn = getattr(self.broker, "account_margin_snapshot", None)
        if callable(snapshot_fn):
            try:
                margin_snapshot = snapshot_fn()
                if isinstance(margin_snapshot, dict):
                    equity = margin_snapshot.get("equity")
                    margin = margin_snapshot.get("margin")
                    if self._finite_nonnegative(equity, positive=True) and self._finite_nonnegative(margin):
                        margin_utilization = float(margin) / float(equity)
            except Exception:
                pass

        return {
            "daily_realized_pnl": round(daily_pnl, 2),
            "weekly_realized_pnl": round(weekly_pnl, 2),
            "daily_locked": daily_locked,
            "weekly_locked": weekly_locked,
            "margin_locked": margin_locked,
            "margin_reason": margin_reason,
            "margin_utilization": margin_utilization,
            "lock_state": (
                "DAILY_LOCKED" if daily_locked else
                "WEEKLY_LOCKED" if weekly_locked else
                "MARGIN_LOCKED" if margin_locked else "RUNNING"
            ),
        }
