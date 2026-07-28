"""Order execution guarded by a durable, account-scoped intent journal."""

from __future__ import annotations

import uuid

import MetaTrader5 as mt5

from core.order_intent_store import OrderIntent, OrderIntentStore, OrderIntentStoreError


class OrderExecutor:
    def __init__(
        self, broker, position_manager, risk_manager, state_manager, trade_logger,
        alerts, config, intent_store=None,
    ):
        self.broker = broker
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.state_manager = state_manager
        self.trade_logger = trade_logger
        self.alerts = alerts
        self.config = config
        self.intent_store = intent_store or OrderIntentStore()

    @property
    def account_scope(self):
        login = getattr(self.broker, "login", None)
        return self.config.ACCOUNT if login is None else f"{self.config.ACCOUNT}::{login}"

    @property
    def strategy_id(self):
        return getattr(self.trade_logger, "strategy_id", None) or self.config.STRATEGY_NAME

    def _log(self, message, level="info", throttle_key=None, cooldown_sec=300):
        prefix = f"[OrderExecutor {self.config.STRATEGY_NAME}] {message}"
        print(prefix)
        if not self.alerts:
            return
        if level == "warning":
            if throttle_key is not None:
                self.alerts.send_throttled_warning(throttle_key, prefix, cooldown_sec)
            else:
                self.alerts.send_warning(prefix)
        elif level == "critical":
            self.alerts.send_critical(prefix)
        else:
            self.alerts.send_info(prefix)

    def execute_signal(self, signal, strategy):
        """Submit a signal once; repeated calls return its existing durable intent.

        The return value remains truthy only after a fill, preserving the legacy
        runner's success check while exposing the complete intent to callers.
        """
        if signal is None:
            return None
        signal_id = str(signal.setdefault("signal_id", uuid.uuid4().hex))
        try:
            intent, claimed = self.intent_store.claim_signal(
                account_id=self.account_scope,
                strategy_id=self.strategy_id,
                signal_id=signal_id,
                symbol=self.config.SYMBOL,
                side=signal["side"],
            )
            if not claimed:
                return self._reconcile_one(intent)
        except (KeyError, OrderIntentStoreError) as exc:
            self._log(f"Order intent unavailable; submit blocked: {exc}", "critical")
            return None

        now = self.broker.broker_now()
        if self.position_manager.has_position(self.config.MAGIC):
            return self.intent_store.transition(intent.intent_id, "CANCELLED", last_error="position already open")

        strategy.mark_order_pending(now)
        tick = self.broker.get_tick()
        if tick is None:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(intent.intent_id, "REJECTED", last_error="tick unavailable")

        expected_entry = signal["expected_entry"]
        stop_distance = signal["stop_distance"]
        tp_distance = signal.get("tp_distance")
        no_tp_model = getattr(self.config, "TAKE_PROFIT_MODEL", None) == "NONE"
        if (tp_distance is None) != no_tp_model:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(
                intent.intent_id,
                "REJECTED",
                last_error="signal take-profit does not match TAKE_PROFIT_MODEL",
            )
        if tp_distance is not None and tp_distance <= 0:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(
                intent.intent_id, "REJECTED", last_error="invalid take-profit distance"
            )
        market_price = tick.ask if signal["side"] == "BUY" else tick.bid
        custom_stop_model = getattr(self.config, "STOP_LOSS_MODEL", None) == "CUSTOM"
        stop_price = signal.get("stop_price")
        if custom_stop_model:
            if stop_price is None:
                strategy.register_rejected_order(now)
                return self.intent_store.transition(
                    intent.intent_id, "REJECTED", last_error="CUSTOM stop model requires stop_price"
                )
            sl = float(stop_price)
            valid_side = (
                signal["side"] == "BUY" and sl < market_price
            ) or (
                signal["side"] == "SELL" and sl > market_price
            )
            if not valid_side:
                strategy.register_rejected_order(now)
                return self.intent_store.transition(
                    intent.intent_id, "REJECTED", last_error="custom stop is beyond market entry"
                )
            stop_distance = abs(float(expected_entry) - sl)
        elif stop_price is not None:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(
                intent.intent_id, "REJECTED", last_error="stop_price requires STOP_LOSS_MODEL=CUSTOM"
            )
        if stop_distance <= 0:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(
                intent.intent_id, "REJECTED", last_error="invalid stop-loss distance"
            )
        max_slippage = stop_distance * self.config.MAX_SLIPPAGE_AS_STOP_FRACTION
        if abs(market_price - expected_entry) > max_slippage:
            strategy.register_skipped_signal(signal["side"])
            strategy.save_to_state(self.state_manager)
            return self.intent_store.transition(intent.intent_id, "CANCELLED", last_error="slippage limit exceeded")

        if signal["side"] == "BUY":
            if not custom_stop_model:
                sl = market_price - stop_distance
            tp = 0.0 if tp_distance is None else market_price + tp_distance
        else:
            if not custom_stop_model:
                sl = market_price + stop_distance
            tp = 0.0 if tp_distance is None else market_price - tp_distance
        size_result = self.risk_manager.calculate_position_size(entry=market_price, sl=sl)
        if size_result is None or not size_result.get("valid"):
            reason = "invalid position size" if size_result is None else size_result.get("reason", "invalid position size")
            strategy.register_rejected_order(now)
            return self.intent_store.transition(intent.intent_id, "REJECTED", last_error=str(reason))

        volume = size_result["lot"]
        margin_check = getattr(self.risk_manager, "validate_margin_for_order", None)
        if callable(margin_check):
            margin_result = margin_check(
                side=signal["side"], volume=volume, entry=market_price, sl=sl,
            )
            if not margin_result.get("valid", False):
                strategy.register_rejected_order(now)
                return self.intent_store.transition(
                    intent.intent_id, "REJECTED",
                    last_error=margin_result.get("reason", "MARGIN_GUARD_BLOCK"),
                )
        request = {
            "symbol": self.config.SYMBOL, "side": signal["side"], "volume": volume,
            "sl": sl, "tp": tp, "magic": self.config.MAGIC,
            "expected_entry": expected_entry, "market_price": market_price,
        }
        self.intent_store.set_request(intent.intent_id, volume=volume, sl=sl, tp=tp, request=request)
        intent = self.intent_store.transition(intent.intent_id, "SUBMITTING")
        try:
            result = self.broker.send_market_order(
                side=signal["side"], volume=volume, sl=sl, tp=tp, magic=self.config.MAGIC,
                comment=self.config.STRATEGY_NAME, client_reference=intent.client_reference,
            )
        except Exception as exc:
            # The call crossed the broker boundary: an exception has unknown
            # side effects and must be reconciled, never retried in-process.
            return self.intent_store.transition(intent.intent_id, "UNKNOWN", last_error=f"broker submit exception: {exc}")

        if result is None:
            return self.intent_store.transition(intent.intent_id, "UNKNOWN", last_error="order_send returned None")

        result_data = self._result_data(result)
        retcode = getattr(result, "retcode", None)
        order_id = getattr(result, "order", None) or getattr(result, "deal", None)
        accepted_retcodes = {
            getattr(mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
            getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
        }
        if retcode not in accepted_retcodes:
            strategy.register_rejected_order(now)
            return self.intent_store.transition(
                intent.intent_id, "REJECTED", retcode=retcode, broker_order_id=order_id,
                broker_result=result_data, last_error=f"broker retcode {retcode}",
            )

        accepted = self.intent_store.transition(
            intent.intent_id, "ACCEPTED", retcode=retcode, broker_order_id=order_id,
            broker_result=result_data,
        )
        try:
            position = self.broker.find_position_for_intent(
                accepted.client_reference, self.config.MAGIC
            ) if hasattr(self.broker, "find_position_for_intent") else self.broker.wait_for_position(self.config.MAGIC, timeout=6.0)
        except Exception as exc:
            return self.intent_store.transition(accepted.intent_id, "UNKNOWN", last_error=f"position lookup failed: {exc}")
        if position is None:
            return self.intent_store.transition(accepted.intent_id, "RECONCILING", last_error="accepted; position not yet visible")
        return self._mark_filled(accepted, position, strategy, size_result, expected_entry, sl, tp, result_data, retcode)

    def reconcile_pending_intents(self):
        """Re-check unresolved submits after reconnect/startup; never submits here."""
        try:
            intents = self.intent_store.list_reconcilable(self.account_scope)
            return [self._reconcile_one(intent) for intent in intents]
        except OrderIntentStoreError as exc:
            self._log(f"Order intent reconciliation unavailable: {exc}", "critical")
            return []

    def _reconcile_one(self, intent: OrderIntent) -> OrderIntent:
        if intent.status not in self.intent_store.ACTIVE_STATUSES:
            return intent
        if intent.status == "CREATED":
            return intent
        try:
            execution_match = self.broker.find_execution_for_intent(
                intent.client_reference, self.config.MAGIC
            ) if hasattr(self.broker, "find_execution_for_intent") else None
            position = self.broker.find_position_for_intent(
                intent.client_reference, self.config.MAGIC
            ) if hasattr(self.broker, "find_position_for_intent") else self.broker.get_position(self.config.MAGIC)
        except Exception as exc:
            return self.intent_store.transition(intent.intent_id, "UNKNOWN", last_error=f"reconciliation lookup failed: {exc}")
        if execution_match is not None and execution_match.source == "history":
            # The order may have filled and closed before a reconnect made the
            # position visible.  This is an exact client-reference match, not
            # a guess by magic; TradeReconciliation imports the deal details.
            volume = execution_match.volume or intent.requested_volume
            status = "PARTIALLY_FILLED" if volume and volume < (intent.requested_volume or volume) else "FILLED"
            return self.intent_store.transition(
                intent.intent_id, status,
                broker_order_id=execution_match.order_id,
                broker_position_id=execution_match.position_id,
                filled_volume=volume,
                last_error="resolved from broker history",
            )
        if position is None:
            if intent.status != "RECONCILING":
                return self.intent_store.transition(intent.intent_id, "RECONCILING", last_error="awaiting broker visibility")
            return intent
        volume = float(getattr(position, "volume", 0.0))
        requested = intent.requested_volume or volume
        status = "PARTIALLY_FILLED" if volume and volume < requested else "FILLED"
        return self.intent_store.transition(
            intent.intent_id, status, broker_position_id=getattr(position, "ticket", None), filled_volume=volume,
        )

    def _mark_filled(self, intent, position, strategy, size_result, expected_entry, sl, tp, result_data, retcode):
        actual_entry = position.price_open
        filled_volume = float(getattr(position, "volume", 0.0))
        status = "PARTIALLY_FILLED" if filled_volume and filled_volume < intent.requested_volume else "FILLED"
        intent = self.intent_store.transition(
            intent.intent_id, status, retcode=retcode, broker_position_id=position.ticket,
            filled_volume=filled_volume, broker_result=result_data,
        )
        spread = self.broker.get_spread_points()
        risk_usd = size_result.get("actual_risk_usd") or 0
        stop_distance = abs(actual_entry - sl)
        take_distance = None if tp in (None, 0) else abs(tp - actual_entry)
        target_r = (
            take_distance / stop_distance
            if take_distance is not None and stop_distance > 0
            else None
        )
        utc_now = getattr(self.broker, "utc_now", self.broker.broker_now)
        trade_id = self.trade_logger.record_trade_open(
            ticket=position.ticket, magic=self.config.MAGIC, strategy_name=self.config.STRATEGY_NAME,
            symbol=self.config.SYMBOL, side=intent.side, entry_time=utc_now(),
            expected_entry=expected_entry, actual_entry=actual_entry, entry_spread_points=spread,
            volume=filled_volume or intent.requested_volume, initial_sl=sl,
            initial_tp=None if tp in (None, 0) else tp,
            stop_distance_points=stop_distance, take_distance_points=take_distance, target_r=target_r,
            risk_usd=risk_usd, equity_at_entry=self.broker.account_equity(),
            order_id=intent.broker_order_id or result_data.get("order"),
            position_id=position.ticket,
            deal_id=result_data.get("deal"),
        )
        if self.alerts:
            self.alerts.alert_position_opened(self.config.STRATEGY_NAME, intent.side, actual_entry, sl, tp, risk_usd, filled_volume or intent.requested_volume)
        self.state_manager.set_execution_cache(position.ticket, {
            "trade_id": trade_id, "intent_id": intent.intent_id, "expected_entry_price": expected_entry,
            "requested_entry_price": (intent.request or {}).get("market_price"),
            "actual_entry_price": actual_entry, "entry_slippage": abs(
                actual_entry - ((intent.request or {}).get("market_price") or expected_entry)
            ),
            "initial_sl": sl, "entry_spread": spread,
            "planned_risk_usd": risk_usd, "risk_usd": risk_usd,
        })
        strategy.register_filled_entry(side=intent.side)
        strategy.save_to_state(self.state_manager)
        self.state_manager.set_strategy_value("breakeven_done", False)
        return intent

    @staticmethod
    def _result_data(result):
        return {
            field: getattr(result, field) for field in ("retcode", "comment", "request_id", "order", "deal", "volume")
            if getattr(result, field, None) is not None
        }
