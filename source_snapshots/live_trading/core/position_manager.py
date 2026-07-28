import time
import MetaTrader5 as mt5
from datetime import datetime


class PositionManager:
    def __init__(
        self,
        broker,
        config,
        state_manager,
        trade_logger,
        alerts
    ):
        self.broker = broker
        self.config = config
        self.state_manager = state_manager
        self.trade_logger = trade_logger
        self.alerts = alerts

    # =====================================
    # ALERTS HELPER
    # =====================================

    def _log(self, message, level="info"):
        prefix = f"[PositionManager {self.config.STRATEGY_NAME}] {message}"

        print(prefix)

        if not self.alerts:
            return

        if level == "warning":
            self.alerts.send_warning(prefix)
        elif level == "critical":
            self.alerts.send_critical(prefix)
        else:
            self.alerts.send_info(prefix)

    # =====================================
    # BASIC POSITION ACCESS
    # =====================================
    def has_position(self, magic=None):
        return self.broker.has_open_position(magic)

    def get_position(self, magic=None):
        return self.broker.get_position(magic)

    # =====================================
    # RECOVERY GUARD SUPPORT
    # =====================================
    def recover_position_management(self, magic=None):
        position = self.get_position(magic)

        if position is None:
            return False

        try:
            self.validate_position(magic)

            strategy_state = self.state_manager.get_strategy()

            if "breakeven_done" not in strategy_state:
                strategy_state["breakeven_done"] = False
                self.state_manager.save()

            self._log(
                f"Recovery successful | ticket={position.ticket}"
            )
            return True

        except Exception as e:
            self._log(f"Recovery failed: {e}", level="warning")
            return False

    # =====================================
    # CLOSE + ANALYTICS
    # =====================================
    def close_position(self, magic=None, reason="MANUAL"):
        position = self.get_position(magic)

        if position is None:
            return False

        ticket = position.ticket

        result = self.broker.close_position(position)

        if result is None:
            return False

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False

        tick = self.broker.get_tick()
        exit_price = None

        if tick is not None:
            if position.type == mt5.POSITION_TYPE_BUY:
                exit_price = tick.bid
            else:
                exit_price = tick.ask

        self._record_close_analytics(position, exit_price, reason)

        self._log(
            f"Position closed | ticket={ticket} | reason={reason}"
        )

        return True

    # =====================================
    # POST-CLOSE ANALYTICS (shared by close_position and _close_with_retry)
    # =====================================
    def _record_close_analytics(self, position, exit_price, reason):
        """
        Record trade close to analytics, clear execution cache,
        reset breakeven flag, fire closed-alert.

        Does NOT call broker.close_position - assumes the position
        is already closed (or about to be) by the caller.
        """
        ticket = position.ticket

        execution_cache = self.state_manager.get_execution_cache(ticket)
        r_multiple = None
        trade_id = None

        if execution_cache:
            trade_id = execution_cache.get("trade_id")
            risk_usd = execution_cache.get("risk_usd")

            if risk_usd is not None and risk_usd > 0:
                r_multiple = position.profit / risk_usd

        if trade_id is not None:
            # Enrich with pnl_points / pnl_r / duration so analytics
            # records the same fields as handle_external_position_close.
            # r_multiple_close starts as None (not 0.0) so that
            # a missing risk_usd is distinguishable from a genuine 0.0R
            # (breakeven) close. trade_logger writes None -> "N/A".
            r_multiple_close = None
            pnl_points = None
            trade_duration_sec = None

            trade = self.trade_logger.get_trade(trade_id)

            if trade:
                side = trade.get("side")
                entry_time_str = trade.get("entry_time")

                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str)
                        trade_duration_sec = (
                            self.broker.broker_now() - entry_time
                        ).total_seconds()
                    except Exception:
                        pass

                actual_entry = (
                    execution_cache.get("actual_entry_price")
                    if execution_cache else None
                )

                if actual_entry is not None and exit_price is not None:
                    if side == "BUY":
                        pnl_points = exit_price - actual_entry
                    elif side == "SELL":
                        pnl_points = actual_entry - exit_price

            if risk_usd and risk_usd > 0:
                r_multiple_close = position.profit / risk_usd

            self.trade_logger.record_trade_close(
                trade_id=trade_id,
                exit_time=self.broker.broker_now(),
                exit_price=exit_price,
                pnl_usd=position.profit,
                pnl_points=pnl_points,
                pnl_r=r_multiple_close,
                trade_duration_sec=trade_duration_sec,
                close_reason=reason
            )

        self.state_manager.clear_execution_cache(ticket)

        strategy_state = self.state_manager.get_strategy()
        strategy_state["breakeven_done"] = False
        self.state_manager.save()

        if r_multiple is None:
            r_multiple = 0.0

        self.alerts.alert_position_closed(
            strategy_name=self.config.STRATEGY_NAME,
            pnl_usd=position.profit,
            r_multiple=r_multiple,
            reason=reason
        )

    # =====================================
    # RETRY-CAPABLE CLOSE (for force-close scenarios)
    # =====================================
    def _close_with_retry(self, magic=None, reason="FORCE_CLOSE",
                          max_attempts=5, delay_sec=1.0,
                          critical_throttle_key=None):
        """
        Close position with retry on transient broker failures.
        Used by force_close_weekend / force_close_news where giving
        up means a rule violation.

        Returns True if position was closed (or already gone),
        False if all attempts failed.
        """
        last_ticket = None

        for attempt in range(1, max_attempts + 1):
            # Position may have already closed (SL/TP/manual).
            position = self.get_position(magic)

            if position is None:
                if attempt == 1:
                    self._log(
                        f"No position to close | reason={reason}"
                    )
                else:
                    self._log(
                        f"Position closed after attempt {attempt - 1} | "
                        f"ticket={last_ticket} | reason={reason}"
                    )
                return True

            last_ticket = position.ticket

            result = self.broker.close_position(position)

            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                # Success - record analytics WITHOUT re-closing on broker
                # (the position is already gone). Get a best-effort exit
                # price from the current tick.
                tick = self.broker.get_tick()
                exit_price = None

                if tick is not None:
                    if position.type == mt5.POSITION_TYPE_BUY:
                        exit_price = tick.bid
                    else:
                        exit_price = tick.ask

                self._record_close_analytics(position, exit_price, reason)

                self._log(
                    f"Force-close succeeded on attempt {attempt} | "
                    f"ticket={last_ticket} | reason={reason}"
                )
                return True

            # Failure on this attempt
            retcode = result.retcode if result is not None else "None"

            attempt_message = (
                f"Close attempt {attempt}/{max_attempts} | "
                f"ticket={last_ticket} | retcode={retcode} | "
                f"reason={reason}"
            )
            if critical_throttle_key and self.alerts:
                prefix = f"[PositionManager {self.config.STRATEGY_NAME}] {attempt_message}"
                print(prefix)
                self.alerts.send_throttled_warning(
                    f"{critical_throttle_key}_attempt",
                    prefix,
                    cooldown_sec=300,
                )
            else:
                self._log(attempt_message, level="warning")

            # Wait before retry (skip on last attempt)
            if attempt < max_attempts:
                time.sleep(delay_sec)

        # All attempts exhausted - escalate
        failure = (
            f"[PositionManager {self.config.STRATEGY_NAME}] "
            f"FORCE-CLOSE FAILED after {max_attempts} attempts | "
            f"ticket={last_ticket} | reason={reason} | "
            f"MANUAL INTERVENTION REQUIRED"
        )
        print(failure)
        if self.alerts:
            if critical_throttle_key and hasattr(self.alerts, "send_throttled_critical"):
                self.alerts.send_throttled_critical(
                    critical_throttle_key,
                    failure,
                    cooldown_sec=300,
                )
            else:
                self.alerts.send_critical(failure)

        return False

    # =====================================
    # POSITION SNAPSHOT
    # =====================================
    def get_position_snapshot(self, magic=None):
        position = self.get_position(magic)

        if position is None:
            return None

        side = (
            "BUY"
            if position.type == mt5.POSITION_TYPE_BUY
            else "SELL"
        )

        return {
            "ticket": position.ticket,
            "side": side,
            "entry_price": position.price_open,
            "sl": position.sl,
            "tp": position.tp,
            "volume": position.volume,
            "profit": position.profit
        }

    # =====================================
    # SAFETY VALIDATION
    # =====================================
    def validate_position(self, magic=None):
        position = self.get_position(magic)

        if position is None:
            return True

        if position.sl is None or position.sl == 0:
            raise RuntimeError(
                f"CRITICAL: Position {position.ticket} has no SL"
            )

        tp_required = getattr(self.config, "TAKE_PROFIT_MODEL", None) != "NONE"
        if tp_required and (position.tp is None or position.tp == 0):
            raise RuntimeError(
                f"CRITICAL: Position {position.ticket} has no TP"
            )

        return True

    # =====================================
    # BREAK EVEN
    # =====================================
    def manage_break_even(self, magic=None):
        strategy_state = self.state_manager.get_strategy()
        position = self.get_position(magic)

        if position is None:
            strategy_state["breakeven_done"] = False
            self.state_manager.save()
            return False

        if not self.config.USE_BREAK_EVEN:
            return False

        if strategy_state.get("breakeven_done", False):
            return False

        tick = self.broker.get_tick()
        if tick is None:
            return False

        side = (
            "BUY"
            if position.type == mt5.POSITION_TYPE_BUY
            else "SELL"
        )

        entry = position.price_open
        sl = position.sl

        if side == "BUY":
            current_price = tick.bid
            risk = entry - sl
            move = current_price - entry
        else:
            current_price = tick.ask
            risk = sl - entry
            move = entry - current_price

        if risk <= 0:
            return False

        # =====================================
        # BE MODE SUPPORT
        # =====================================
        if self.config.BREAK_EVEN_MODEL == "R_MULTIPLE":
            trigger_distance = (
                risk * self.config.BREAK_EVEN_TRIGGER
            )

            offset = (
                risk * self.config.BREAK_EVEN_OFFSET
            )

        elif self.config.BREAK_EVEN_MODEL == "FIXED_PRICE":
            trigger_distance = (
                self.config.BREAK_EVEN_TRIGGER
            )

            offset = (
                self.config.BREAK_EVEN_OFFSET
            )

        else:
            raise RuntimeError(
                f"Unsupported BE model: "
                f"{self.config.BREAK_EVEN_MODEL}"
            )

        if move < trigger_distance:
            return False

        if side == "BUY":
            new_sl = entry + offset
        else:
            new_sl = entry - offset

        result = self.broker.modify_sl(position, new_sl)

        if result is None:
            return False

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self._log(
                f"BE modify failed: {result.retcode}",
                level="warning"
            )
            return False

        strategy_state["breakeven_done"] = True
        self.state_manager.save()

        self._log(
            f"Break-even activated | "
            f"ticket={position.ticket} | "
            f"new SL={round(new_sl, 2)}"
        )
        self.alerts.alert_break_even(
            strategy_name=self.config.STRATEGY_NAME,
            side=side,
            ticket=position.ticket,
            entry=entry,
            new_sl=new_sl,
        )

        return True
    

    def handle_external_position_close(self):
        execution_cache = self.state_manager.state["execution_cache"]

        if not execution_cache:
            return False

        # =====================================
        # OPEN POSITIONS
        # =====================================
        open_positions = self.broker.get_positions()
        open_tickets = {str(p.ticket) for p in open_positions}

        # =====================================
        # CACHED POSITIONS
        # =====================================
        cached_tickets = set(execution_cache.keys())

        # Positions disappeared from broker
        closed_tickets = cached_tickets - open_tickets

        if not closed_tickets:
            return False

        handled_any = False

        for ticket in closed_tickets:
            cache = execution_cache.get(ticket)

            if cache is None:
                continue

            trade_id = cache.get("trade_id")
            risk_usd = cache.get("risk_usd", 0)
            actual_entry = cache.get("actual_entry_price")

            # =====================================
            # WAIT UNTIL CLOSE DEAL APPEARS IN MT5
            # =====================================
            deal_data = self.broker.get_deal_profit(ticket)

            if deal_data is None:
                self.alerts.send_terminal_throttled(
                    key=f"missing_close_{ticket}",
                    message=(
                        f"[PositionManager {self.config.STRATEGY_NAME}] "
                        f"Close deal not found yet | ticket={ticket}"
                    ),
                    cooldown_sec=300
                )
                continue

            pnl_usd = deal_data["profit"]
            exit_price = deal_data["price"]
            normalize_epoch = getattr(self.broker, "utc_from_broker_epoch", None)
            exit_time = (
                normalize_epoch(deal_data["time"])
                if callable(normalize_epoch)
                else datetime.fromtimestamp(deal_data["time"])
            )
            deal_reason = deal_data.get("reason")
            close_reason = self.broker.decode_close_reason(
                deal_data
            )

            if deal_reason == mt5.DEAL_REASON_TP:
                close_reason = "TP"

            elif deal_reason == mt5.DEAL_REASON_SL:
                close_reason = "SL"

            elif deal_reason == mt5.DEAL_REASON_SO:
                close_reason = "STOPOUT"

            elif deal_reason == mt5.DEAL_REASON_CLIENT:
                close_reason = "MANUAL_CLOSE"

            elif deal_reason == mt5.DEAL_REASON_EXPERT:
                close_reason = "MANUAL_CLOSE"

            # r_multiple starts as None (not 0.0) so a missing
            # risk_usd is distinguishable from a genuine 0.0R close.
            # trade_logger writes None -> "N/A"; the alert below falls
            # back to 0.0 explicitly.
            r_multiple = None
            pnl_points = None
            trade_duration_sec = None
            side = None

            # =====================================
            # LOAD TRADE SIDE
            # =====================================
            if trade_id:
                trade = self.trade_logger.get_trade(trade_id)

                if trade:
                    side = trade.get("side")

                    entry_time_str = trade.get("entry_time")

                    if entry_time_str:
                        entry_time = datetime.fromisoformat(entry_time_str)

                        trade_duration_sec = (
                            exit_time - entry_time
                        ).total_seconds()
            # =====================================
            # CALCULATE R
            # =====================================
            if risk_usd and risk_usd > 0:
                r_multiple = pnl_usd / risk_usd

            # =====================================
            # CALCULATE PNL POINTS
            # =====================================
            if actual_entry is not None and exit_price is not None:
                if side == "BUY":
                    pnl_points = exit_price - actual_entry
                elif side == "SELL":
                    pnl_points = actual_entry - exit_price

            # =====================================
            # ANALYTICS
            # =====================================
            if trade_id:
                self.trade_logger.record_trade_close(
                    trade_id=trade_id,
                    exit_time=exit_time,
                    exit_price=exit_price,
                    close_reason=close_reason,
                    pnl_usd=pnl_usd,
                    pnl_points=pnl_points,
                    pnl_r=r_multiple,
                    trade_duration_sec=trade_duration_sec
                )

            # =====================================
            # CACHE CLEANUP
            # =====================================
            self.state_manager.clear_execution_cache(ticket)

            # =====================================
            # ALERTS
            # =====================================
            self.alerts.alert_position_closed(
                strategy_name=self.config.STRATEGY_NAME,
                pnl_usd=pnl_usd,
                r_multiple=(r_multiple if r_multiple is not None else 0.0),
                reason=close_reason
            )

            self._log(
                f"External close handled | "
                f"ticket={ticket} | "
                f"PnL={pnl_usd}"
            )

            handled_any = True

        # =====================================
        # RESET STRATEGY STATE
        # =====================================
        if handled_any:
            strategy_state = self.state_manager.get_strategy()
            strategy_state["breakeven_done"] = False
            self.state_manager.save()

        return handled_any
    
    
    # =====================================
    # FUTURE TRAILING
    # =====================================
    def manage_trailing_stop(self, magic=None):
        return False

    # =====================================
    # UNIVERSAL PROTECTION
    # =====================================
    def manage_protection(self, magic=None):
        self.manage_break_even(magic)
        self.manage_trailing_stop(magic)

    # =====================================
    # WEEKEND CLOSE (retry-capable)
    # =====================================
    def force_close_weekend(self, magic=None):
        return self._close_with_retry(
            magic=magic,
            reason="WEEKEND_CLOSE",
            max_attempts=5,
            delay_sec=1.0
        )

    def force_close_strategy_exit(self, magic=None):
        """Fail loudly when a strategy-owned time exit cannot be completed."""
        return self._close_with_retry(
            magic=magic,
            reason="STRATEGY_TIME_EXIT",
            max_attempts=5,
            delay_sec=1.0,
            critical_throttle_key=f"{self.config.STRATEGY_NAME}_time_exit_failed",
        )

    # =====================================
    # NEWS CLOSE (retry-capable)
    # =====================================
    def force_close_news(self, magic=None):
        return self._close_with_retry(
            magic=magic,
            reason="NEWS_CLOSE",
            max_attempts=5,
            delay_sec=1.0
        )
