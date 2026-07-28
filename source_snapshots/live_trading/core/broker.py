import json
import uuid
from dataclasses import dataclass
from enum import Enum

import MetaTrader5 as mt5
import time
from datetime import datetime, timedelta, timezone

from core.retry_policy import RetryPolicy
from core.broker_clock import BrokerClock


RETCODES = {
    10004: "REQUOTE",
    10006: "REJECT",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10016: "INVALID_STOPS",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10021: "PRICE_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10027: "CLIENT_DISABLES_AT",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
}
RETCODE_CONNECTION = 10031

# Retcodes that are transient - worth retrying with a fresh price.
# Everything else is treated as final (INVALID_STOPS, NO_MONEY,
# MARKET_CLOSED, etc.) and is NOT retried.
TRANSIENT_RETCODES = {
    10004,  # REQUOTE
    10021,  # PRICE_CHANGED
    10024,  # TOO_MANY_REQUESTS
    10031,  # CONNECTION
}

class PositionQueryError(RuntimeError):
    """Raised when MT5 cannot confirm the account's open positions."""


class AlreadyClosedPosition:
    """Explicit idempotent outcome for a ticket no longer open at MT5."""

    def __init__(self, ticket):
        self.ticket = int(ticket)
        self.retcode = None
        self.already_closed = True


class ConnectionState(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


@dataclass(frozen=True)
class IntentExecutionMatch:
    """An unambiguous broker-side match for a durable client reference."""

    position_id: int | None
    order_id: int | None
    deal_id: int | None
    volume: float
    source: str


class Broker:
    def __init__(
        self,
        symbol,
        deviation_points=20,
        mt5_path=None,
        login=None,
        password=None,
        server=None,
        alerts=None,
        retry_policy=None,
        monotonic_fn=None,
        connection_owner=None,
        clock=None,
        clock_settings=None,
    ):
        self.symbol = symbol
        self.deviation_points = deviation_points

        self.mt5_path = mt5_path
        self.login = login
        self.password = password
        self.server = server
        self.alerts = alerts
        self.retry_policy = retry_policy or RetryPolicy()
        self._monotonic = monotonic_fn or time.monotonic
        self.connection_state = ConnectionState.DISCONNECTED
        self._reconnect_failures = 0
        self._next_reconnect_at = 0.0
        self._reconnect_required = False
        self._connection_owner = connection_owner
        owner = connection_owner or self
        self.clock = (
            clock
            or getattr(owner, "clock", None)
            or BrokerClock(**(clock_settings or {}))
        )

        # Filling modes are symbol-specific.  An account-level flatten may
        # close XAU, FX and indices through a Broker created for one symbol.
        self._cached_filling_modes = {}

    def for_symbol(self, symbol, *, alerts=None):
        """Return a symbol-scoped broker view sharing this MT5 lifecycle.

        MT5 exposes one process-global terminal session.  A hub therefore owns
        one connected ``Broker`` and gives strategies views which retain the
        existing symbol-oriented API without calling initialize/login/shutdown.
        """
        owner = self._connection_owner or self
        return Broker(
            symbol=symbol,
            deviation_points=self.deviation_points,
            mt5_path=self.mt5_path,
            login=self.login,
            password=self.password,
            server=self.server,
            alerts=alerts if alerts is not None else self.alerts,
            retry_policy=self.retry_policy,
            monotonic_fn=self._monotonic,
            connection_owner=owner,
            clock=owner.clock,
        )

    # =====================================
    # ALERTS HELPER
    # =====================================

    def _log(self, message, critical=False):
        print(message)

        if self.alerts is None:
            return

        if critical:
            self.alerts.send_critical(message)
        else:
            self.alerts.send_info(message)

    def _event(self, event, *, critical=False, **fields):
        """Emit one machine-readable broker event without credentials."""
        payload = {
            "event": event,
            "account": self.login,
            "symbol": self.symbol,
            **{key: value for key, value in fields.items() if value is not None},
        }
        self._log(json.dumps(payload, default=str, sort_keys=True), critical=critical)

    # =====================================
    # IDEMPOTENCY HELPERS
    # =====================================

    def _generate_client_ref(self):
        """
        Short unique id for idempotency tracking.
        8 hex chars (4 bytes of entropy) is plenty to distinguish
        concurrent signals across all bots. Fits MT5 comment (31 chars).
        """
        return uuid.uuid4().hex[:8]

    # =====================================
    # INTERNAL TERMINAL INIT
    # =====================================
    def _initialize_terminal(self):
        kwargs = {}

        if self.mt5_path is not None:
            kwargs["path"] = self.mt5_path

        if not mt5.initialize(**kwargs):
            return False

        if self.login is not None:
            authorized = mt5.login(
                login=self.login,
                password=self.password,
                server=self.server
            )

            if not authorized:
                return False

        account = mt5.account_info()

        if account is None:
            return False

        if self.login is not None:
            if account.login != self.login:
                print(
                    f"Wrong account connected: "
                    f"{account.login}, expected {self.login}"
                )
                return False

        return True

    # =====================================
    # CONNECTION
    # =====================================
    def connect(self):
        if self._connection_owner is not None:
            if not self._connection_owner.ensure_connection():
                return False
            symbol_info = mt5.symbol_info(self.symbol)
            if symbol_info is None:
                raise RuntimeError(f"Unknown symbol: {self.symbol}")
            if not symbol_info.visible:
                mt5.symbol_select(self.symbol, True)
            self.connection_state = self._connection_owner.connection_state
            return True
        if not self._initialize_terminal():
            self.connection_state = ConnectionState.DISCONNECTED
            self._next_reconnect_at = self._monotonic()
            self._event("broker_connect_failed", critical=True)
            return False

        account = mt5.account_info()
        symbol_info = mt5.symbol_info(self.symbol)

        if symbol_info is None:
            raise RuntimeError(f"Unknown symbol: {self.symbol}")

        if not symbol_info.visible:
            mt5.symbol_select(self.symbol, True)

        self.connection_state = ConnectionState.CONNECTED
        self._reconnect_failures = 0
        self._next_reconnect_at = 0.0
        self._reconnect_required = False
        self._event("broker_connected", account=account.login)

        return True

    def shutdown(self):
        if self._connection_owner is not None:
            return
        mt5.shutdown()

    def _connection_is_healthy(self):
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        # terminal_info can still exist while the terminal has lost its
        # connection to the trade server.  In that state MT5 returns 10031
        # from order_send, so account information alone is not enough.
        terminal_connected = getattr(terminal, "connected", True)
        if terminal is not None and terminal_connected and account is not None:
            if self.login is None or account.login == self.login:
                return True
        return False

    def _require_reconnect_after_trade_connection_loss(self, request_id=None):
        """Force the hub owner through its normal reconnect state machine.

        An order submission is never retried here: even a lost response could
        have reached the broker.  MT5 retcode 10031 is different because it
        explicitly confirms that there was no trade-server connection.  We
        record the rejected intent, block further sends, and let the main loop
        reinitialize the terminal before the strategy can create a new intent.
        """
        owner = self._connection_owner or self
        owner._reconnect_required = True
        owner.connection_state = ConnectionState.DISCONNECTED
        owner._next_reconnect_at = owner._monotonic()
        self.connection_state = ConnectionState.DISCONNECTED
        self._event("broker_trade_connection_lost", request_id=request_id)

    def can_submit_new_orders(self):
        """Unknown/circuit-open connection state is always no-new-orders."""
        return self.connection_state == ConnectionState.CONNECTED and self._connection_is_healthy()

    def ensure_connection(self, now=None):
        """Advance reconnect state once; never sleep or loop internally."""
        if self._connection_owner is not None:
            connected = self._connection_owner.ensure_connection(now=now)
            self.connection_state = self._connection_owner.connection_state
            return connected
        now = self._monotonic() if now is None else now
        if not self._reconnect_required and self._connection_is_healthy():
            if self.connection_state != ConnectionState.CONNECTED:
                account = mt5.account_info()
                self._event("broker_reconnected", account=getattr(account, "login", None))
            self.connection_state = ConnectionState.CONNECTED
            self._reconnect_failures = 0
            self._next_reconnect_at = 0.0
            return True

        if self.connection_state == ConnectionState.CONNECTED:
            self._event("broker_connection_lost", critical=True)
            self.connection_state = ConnectionState.DISCONNECTED
            self._next_reconnect_at = now
        if now < self._next_reconnect_at:
            return False
        if self.connection_state == ConnectionState.CIRCUIT_OPEN:
            self._event("broker_circuit_half_open")
            self.connection_state = ConnectionState.DISCONNECTED
            self._reconnect_failures = 0

        self.connection_state = ConnectionState.RECONNECTING
        mt5.shutdown()
        started = self._monotonic()
        if self._initialize_terminal():
            self.connection_state = ConnectionState.CONNECTED
            self._reconnect_failures = 0
            self._next_reconnect_at = 0.0
            self._reconnect_required = False
            self._event("broker_reconnected", latency_ms=round((self._monotonic() - started) * 1000, 2))
            return True

        self._reconnect_failures += 1
        if self._reconnect_failures >= self.retry_policy.reconnect_attempts:
            self.connection_state = ConnectionState.CIRCUIT_OPEN
            self._next_reconnect_at = now + self.retry_policy.reconnect_circuit_cooldown_sec
            self._event(
                "broker_reconnect_exhausted",
                critical=True,
                attempts=self._reconnect_failures,
                retry_at=self._next_reconnect_at,
            )
            return False
        delay = self.retry_policy.reconnect_delay(self._reconnect_failures)
        self.connection_state = ConnectionState.DISCONNECTED
        self._next_reconnect_at = now + delay
        self._event(
            "broker_reconnect_scheduled",
            attempts=self._reconnect_failures,
            delay_sec=delay,
            latency_ms=round((self._monotonic() - started) * 1000, 2),
        )
        return False

    # =====================================
    # HELPERS
    # =====================================
    def digits(self):
        info = mt5.symbol_info(self.symbol)
        return info.digits

    def point(self):
        info = mt5.symbol_info(self.symbol)
        return info.point

    def normalize_price(self, price):
        return round(price, self.digits())

    def detect_filling_mode(self, symbol=None):
        symbol = symbol or self.symbol

        if symbol in self._cached_filling_modes:
            return self._cached_filling_modes[symbol]

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return mt5.ORDER_FILLING_IOC

        candidates = [
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN
        ]

        names = {
            mt5.ORDER_FILLING_IOC: "IOC",
            mt5.ORDER_FILLING_FOK: "FOK",
            mt5.ORDER_FILLING_RETURN: "RETURN"
        }

        for fill in candidates:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": 0.01,
                "type": mt5.ORDER_TYPE_BUY,
                "price": tick.ask,
                "deviation": self.deviation_points,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fill
            }

            check = mt5.order_check(request)

            if check is not None:
                # Некоторые брокеры возвращают 0, некоторые DONE
                if check.retcode in (0, mt5.TRADE_RETCODE_DONE):
                    self._cached_filling_modes[symbol] = fill
                    self._log(
                        f"Detected filling mode for "
                        f"{symbol}: {names[fill]}"
                    )
                    return fill

        self._log(
            f"Could not detect filling mode for {symbol}, fallback IOC"
        )
        return mt5.ORDER_FILLING_IOC

    def is_market_open(self):
        tick = self.get_tick()

        if tick is None:
            return False

        if tick.bid == 0 or tick.ask == 0:
            return False

        return True

    # =====================================
    # MARKET DATA
    # =====================================
    def get_tick(self):
        return mt5.symbol_info_tick(self.symbol)

    def get_bid(self):
        tick = self.get_tick()
        return None if tick is None else tick.bid

    def get_ask(self):
        tick = self.get_tick()
        return None if tick is None else tick.ask

    def get_spread_points(self):
        tick = self.get_tick()
        if tick is None:
            return None

        return (tick.ask - tick.bid) / self.point()

    def broker_now(self):
        """Canonical UTC clock as a naive datetime for legacy callers."""
        return self.clock.utc_now().replace(tzinfo=None)

    def utc_now(self):
        """Return real system UTC for strategy session boundaries."""
        return self.clock.utc_now()

    def calibrate_clock(self):
        tick = self.get_tick()
        if tick is None:
            raise RuntimeError("Cannot calibrate broker clock without a tick")
        return self.clock.observe(tick.time)

    def utc_from_broker_epoch(self, raw_epoch):
        return self.clock.normalize_epoch(raw_epoch)

    def history_query_bounds(self, start_utc, end_utc, *, padding_sec=2 * 60 * 60):
        """Encode canonical UTC bounds for an MT5 server-wall-clock history API."""
        if self.clock.offset_seconds is None:
            raise RuntimeError("broker clock is not calibrated")

        def aware(value):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        offset = timedelta(seconds=self.clock.offset_seconds)
        padding = timedelta(seconds=padding_sec)
        return aware(start_utc) + offset - padding, aware(end_utc) + offset + padding

    def get_symbol_info(self):
        return mt5.symbol_info(self.symbol)

    # =====================================
    # POSITION SIZING HELPERS
    # =====================================
    def estimate_profit_per_lot(self, open_price, close_price):
        """
        Profit/loss magnitude (in account currency) for 1 lot moving
        from `open_price` to `close_price`, computed by MT5 itself.

        MT5.order_calc_profit knows the account's contract size and
        currency conversion, so it returns a correct value on ANY
        broker - including broker setups where trade_tick_value arrives
        "per unit" (e.g. per ounce for XAU) WITHOUT contract_size
        baked in, which breaks the manual tick-based lot formula.
        Direction is inferred from the price relationship so the sign
        is correct regardless of which side the trade is on:
          close < open  -> modelled as a long (BUY) reaching its stop
          close >= open -> modelled as a short (SELL) reaching its stop
        In both cases MT5 returns a negative number (a loss at the
        stop), so we return the absolute magnitude.

        Returns:
            float >= 0  - profit magnitude for 1 lot, or
            None        - MT5 could not compute it (caller must fall
                          back to the manual tick-based formula).
        """
        if open_price is None or close_price is None:
            return None

        if close_price < open_price:
            order_type = mt5.ORDER_TYPE_BUY    # stop below entry -> long
        else:
            order_type = mt5.ORDER_TYPE_SELL   # stop above entry -> short

        try:
            profit = mt5.order_calc_profit(
                order_type,
                self.symbol,
                1.0,            # 1 lot - caller divides risk by this
                open_price,
                close_price
            )
        except Exception as exc:
            self._log(
                f"order_calc_profit raised | {self.symbol} | {exc}"
            )
            return None

        if profit is None:
            return None

        # Magnitude of the move's PnL for 1 lot (risk at the stop).
        return abs(profit)

    # =====================================
    # ACCOUNT
    # =====================================
    def account_equity(self):
        account = mt5.account_info()

        if account is None:
            return None

        return account.equity

    def account_balance(self):
        """
        Account balance (realized PnL, no floating).
        Used by AccountMonitor to record starting_equity on first start.
        """
        account = mt5.account_info()

        if account is None:
            return None

        return account.balance

    def account_margin_snapshot(self):
        """Return the broker's current account margin facts as plain data."""
        account = mt5.account_info()
        if account is None:
            return None
        return {
            "balance": getattr(account, "balance", None),
            "equity": getattr(account, "equity", None),
            "margin": getattr(account, "margin", None),
            "margin_free": getattr(account, "margin_free", None),
            "margin_level": getattr(account, "margin_level", None),
            "margin_so_call": getattr(account, "margin_so_call", None),
            "margin_so_so": getattr(account, "margin_so_so", None),
        }

    def estimate_order_margin(self, side, volume, price, symbol=None):
        """Ask MT5 for incremental margin of one proposed market order."""
        if side not in {"BUY", "SELL"}:
            return None
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        try:
            margin = mt5.order_calc_margin(
                order_type, symbol or self.symbol, float(volume), float(price)
            )
        except Exception as exc:
            self._log(f"order_calc_margin raised | {symbol or self.symbol} | {exc}")
            return None
        return None if margin is None else abs(float(margin))

    def estimate_order_profit(self, side, symbol, volume, open_price, close_price):
        """Return signed MT5 PnL for a hypothetical order price move."""
        if side not in {"BUY", "SELL"}:
            return None
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        try:
            profit = mt5.order_calc_profit(
                order_type, symbol, float(volume), float(open_price), float(close_price)
            )
        except Exception as exc:
            self._log(f"order_calc_profit raised | {symbol} | {exc}")
            return None
        return None if profit is None else float(profit)

    # =====================================
    # POSITION HELPERS
    # =====================================
    def get_positions(self):
        positions = mt5.positions_get(symbol=self.symbol)

        if positions is None:
            err = mt5.last_error()
            self._log(
                f"positions_get failed | symbol={self.symbol} | error={err}"
            )
            return []

        return list(positions)

    def get_all_positions(self):
        """
        ALL open positions on the LOGIN, regardless of symbol/magic.
        Used by kill_switch.flatten_all() for account-level protection
        when an account breaches DD/profit rules every
        position on that account must be closed, not just the bot's own.

        NOTE: differs from get_positions() which is filtered by symbol.
        """
        positions = mt5.positions_get()

        if positions is None:
            err = mt5.last_error()
            self._log(
                f"positions_get (all) failed | error={err}"
            )
            return []

        return list(positions)

    def list_all_positions(self):
        """
        Return every open position for the connected account.

        Unlike the legacy ``get_all_positions`` helper, a broker error is
        never converted into an empty list: treating an unknown answer as
        "flat" could release an account while exposure still exists.
        """
        positions = mt5.positions_get()
        if positions is None:
            err = mt5.last_error()
            self._log(
                f"positions_get (all) failed | error={err}", critical=True
            )
            raise PositionQueryError(
                f"positions_get(all) failed: {err}"
            )
        return list(positions)

    def get_position_by_ticket(self, ticket):
        """Look up an open position by ticket without any symbol filter."""
        positions = mt5.positions_get(ticket=int(ticket))
        if positions is None:
            err = mt5.last_error()
            self._log(
                f"positions_get failed | ticket={ticket} | error={err}",
                critical=True,
            )
            raise PositionQueryError(
                f"positions_get(ticket={ticket}) failed: {err}"
            )
        return positions[0] if positions else None

    def has_open_position(self, magic=None):
        positions = self.get_positions()

        if magic is None:
            return len(positions) > 0

        return any(p.magic == magic for p in positions)

    def get_position(self, magic=None):
        positions = self.get_positions()

        if magic is None:
            return positions[0] if positions else None

        for p in positions:
            if p.magic == magic:
                return p

        return None

    def wait_for_position(self, magic, timeout=3.0):
        start = time.time()

        while time.time() - start < timeout:
            pos = self.get_position(magic)

            if pos is not None:
                return pos

            time.sleep(self.retry_policy.position_visibility_poll_sec)

        return None
    
    def decode_close_reason(self, deal_data):
        if deal_data is None:
            return "EXTERNAL_CLOSE"

        reason = deal_data.get("reason")
        comment = (deal_data.get("comment") or "").upper()

        if "TP" in comment:
            return "TP"

        if "SL" in comment:
            return "SL"

        if reason == mt5.DEAL_REASON_TP:
            return "TP"

        if reason == mt5.DEAL_REASON_SL:
            return "SL"

        if reason == mt5.DEAL_REASON_SO:
            return "STOPOUT"

        if reason == mt5.DEAL_REASON_CLIENT:
            return "MANUAL_CLOSE"

        if reason == mt5.DEAL_REASON_MOBILE:
            return "MANUAL_CLOSE"

        if reason == mt5.DEAL_REASON_WEB:
            return "MANUAL_CLOSE"

        return "EXTERNAL_CLOSE"

    def get_deal_profit(self, ticket):
        # IMPORTANT:
        # Do NOT use broker_now() here.
        # During broker lag tick.time may freeze and hide recent deals.

        end = self.utc_now()
        start = end - timedelta(days=7)
        query_start, query_end = self.history_query_bounds(start, end)

        deals = None

        # =====================================
        # RETRY LAYER (fix MT5 lag)
        # =====================================
        for attempt in range(1, self.retry_policy.history_attempts + 1):
            deals = mt5.history_deals_get(query_start, query_end)

            if deals is not None and len(deals) > 0:
                break

            if attempt < self.retry_policy.history_attempts:
                time.sleep(self.retry_policy.history_backoff_sec)

        if deals is None:
            self._log(
                f"history_deals_get returned None | {self.symbol}"
            )
            return None

        if len(deals) == 0:
            self._log(
                f"history_deals_get returned empty | {self.symbol}"
            )
            return None

        # =====================================
        # SEARCH
        # =====================================
        for deal in reversed(deals):
            if deal.position_id == int(ticket):
                if deal.entry == mt5.DEAL_ENTRY_OUT:
                    return {
                        "profit": deal.profit,
                        "price": deal.price,
                        "time": deal.time,
                        "reason": deal.reason,
                        "comment": deal.comment,
                        "deal_type": deal.type
                    }

        return None
    
    # =====================================
    # ORDER SEND WITH RETRY
    # =====================================
    def _send_close_with_retry(self, request, side, position_ticket, label="Close",
                               price_symbol=None):
        """
        Retry a close request after verifying that the original position still
        exists.  Open submissions deliberately do not use this path: their
        outcome is owned by OrderIntentStore and must be reconciled first.

        On transient retcodes (REQUOTE, PRICE_CHANGED, TOO_MANY_REQUESTS,
        CONNECTION) we refresh the price from the latest tick and retry,
        up to the configured operation retry limit.

        On final retcodes (NO_MONEY, INVALID_STOPS, MARKET_CLOSED, ...)
        we return immediately - retrying would not help.

        `side` ("BUY"/"SELL") is used to re-read the correct price
        (ask for BUY, bid for SELL) on each retry.

        A ticket missing before a retry is an explicit successful close.  An
        unreadable position state blocks the retry instead of assuming flat.
        """
        last_result = None
        price_symbol = price_symbol or self.symbol

        request_id = request.get("comment") or f"{label}:{position_ticket}"
        for attempt in range(1, self.retry_policy.operation_attempts + 1):
            if attempt > 1:
                try:
                    existing = self.get_position_by_ticket(position_ticket)
                except PositionQueryError as exc:
                    self._log(f"{label} | retry blocked: {exc}", critical=True)
                    return None
                if existing is None:
                    self._event("broker_operation_already_applied", request_id=request_id, ticket=position_ticket)
                    return AlreadyClosedPosition(position_ticket)
                # A prior close may have partially filled. Closing the current
                # remainder preserves the full-close intent without reusing a
                # stale volume from before the uncertain broker response.
                request["volume"] = existing.volume

            started = self._monotonic()
            result = mt5.order_send(request)
            latency_ms = round((self._monotonic() - started) * 1000, 2)

            # A close is retryable only after we verified the ticket still
            # exists above. Open submissions deliberately do not use this path.
            if result is None:
                self._event("broker_operation_unknown", critical=True, request_id=request_id,
                            attempt=attempt, latency_ms=latency_ms)
                last_result = None

            elif result.retcode == mt5.TRADE_RETCODE_DONE:
                self._event("broker_operation_succeeded", request_id=request_id,
                            retcode=result.retcode, attempt=attempt, latency_ms=latency_ms)
                return result

            elif result.retcode in TRANSIENT_RETCODES:
                self._event("broker_operation_transient", critical=True, request_id=request_id,
                            retcode=result.retcode, attempt=attempt, latency_ms=latency_ms)
                last_result = result

            else:
                msg = f"{label} retcode | {price_symbol} | {RETCODES.get(result.retcode, result.retcode)}"
                self._event("broker_operation_final", critical=True, request_id=request_id,
                            retcode=result.retcode, attempt=attempt, latency_ms=latency_ms)

                if self.alerts:
                    self.alerts.send_throttled_warning(
                        key=f"{label}_retcode_{price_symbol}_{result.retcode}",
                        message=msg,
                        cooldown_sec=300
                    )
                return result

            # Prepare retry: refresh price in the request from a new tick.
            if attempt < self.retry_policy.operation_attempts:
                tick = mt5.symbol_info_tick(price_symbol)

                if tick is not None:
                    symbol_info = mt5.symbol_info(price_symbol)
                    digits = (
                        symbol_info.digits
                        if symbol_info is not None else None
                    )
                    price = tick.ask if side == "BUY" else tick.bid
                    request["price"] = (
                        round(price, digits)
                        if digits is not None else price
                    )

                time.sleep(self.retry_policy.operation_backoff_sec)

        # All retries exhausted.
        last_rc = (
            RETCODES.get(last_result.retcode)
            if last_result is not None else "None"
        )
        self._log(
            f"{label} | FAILED after {self.retry_policy.operation_attempts} attempts | "
            f"last_retcode={last_rc}",
            critical=True
        )

        return last_result  # may be None if broker never responded

    # =====================================
    # ORDER SEND
    # =====================================
    def send_market_order(
        self,
        side,
        volume,
        sl,
        tp,
        magic,
        comment="",
        expected_entry=None,
        client_reference=None,
    ):
        if not self.can_submit_new_orders():
            self._event("broker_open_submit_blocked", critical=True, request_id=client_reference)
            return None

        tick = self.get_tick()

        if tick is None:
            return None

        order_type = (
            mt5.ORDER_TYPE_BUY
            if side == "BUY"
            else mt5.ORDER_TYPE_SELL
        )

        price = tick.ask if side == "BUY" else tick.bid
        price = self.normalize_price(price)

        # The durable OrderIntentStore allocates this reference before the
        # non-idempotent broker call.  A random fallback is retained only for
        # legacy direct callers, which do not get retry protection here.
        client_ref = client_reference or self._generate_client_ref()

        # Durable references are the only part of an open-order comment used
        # for idempotency.  Send them alone so broker-specific truncation does
        # not consume the token after a strategy-name prefix.
        if client_reference:
            full_comment = client_ref
        elif comment:
            full_comment = f"{comment}|{client_ref}"
        else:
            full_comment = client_ref

        # MT5 truncates comment at 31 chars - keep the ref, drop prefix
        # if needed (the ref is the part that matters for idempotency).
        if len(full_comment) > 31:
            full_comment = client_ref

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation_points,
            "magic": magic,
            "comment": full_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.detect_filling_mode(),
        }

        # Do not blindly retry an open submission.  A lost response can mean
        # the broker accepted it; OrderExecutor records UNKNOWN and reconciles
        # the durable intent before any future submit.
        started = self._monotonic()
        result = mt5.order_send(request)
        if getattr(result, "retcode", None) == RETCODE_CONNECTION:
            self._require_reconnect_after_trade_connection_loss(client_ref)
        self._event(
            "broker_open_submit_result",
            critical=result is None,
            request_id=client_ref,
            retcode=getattr(result, "retcode", None),
            latency_ms=round((self._monotonic() - started) * 1000, 2),
        )
        return result

    def find_position_for_intent(self, client_reference, magic):
        """Find an open position for a durable intent without treating errors as flat."""
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            raise PositionQueryError(f"positions_get(intent) failed: {mt5.last_error()}")
        candidates = [position for position in positions if position.magic == magic]
        exact = [
            position for position in candidates
            if client_reference and client_reference in (getattr(position, "comment", "") or "")
        ]
        if len(exact) == 1:
            return exact[0]
        # Some MT5 servers replace position comments.  With the current
        # one-position-per-magic contract, one matching position is still a
        # safe recovery match; multiple matches remain ambiguous/fail-closed.
        if len(candidates) == 1:
            self._log(
                f"Intent reconciliation used unique-magic fallback | ref={client_reference} | "
                f"ticket={candidates[0].ticket}",
            )
            return candidates[0]
        return None

    def find_execution_for_intent(self, client_reference, magic):
        """Look up a durable intent in open positions, then exact broker history.

        There is intentionally no history fallback by magic. A server may
        rewrite comments, but guessing among multiple historical positions is
        unsafe and would undermine the one-position-per-magic contract.
        """
        position = self.find_position_for_intent(client_reference, magic)
        if position is not None:
            return IntentExecutionMatch(
                position_id=getattr(position, "ticket", None),
                order_id=None,
                deal_id=None,
                volume=float(getattr(position, "volume", 0.0)),
                source="position",
            )

        end = self.utc_now()
        start = end - timedelta(seconds=self.retry_policy.intent_history_lookback_sec)
        query_start, query_end = self.history_query_bounds(start, end)
        orders = mt5.history_orders_get(query_start, query_end)
        deals = mt5.history_deals_get(query_start, query_end)
        if orders is None or deals is None:
            raise PositionQueryError(f"intent history lookup failed: {mt5.last_error()}")

        matching_orders = [
            order for order in orders
            if getattr(order, "magic", None) == magic
            and client_reference in (getattr(order, "comment", "") or "")
        ]
        matching_deals = [
            deal for deal in deals
            if getattr(deal, "magic", None) == magic
            and client_reference in (getattr(deal, "comment", "") or "")
        ]
        if not matching_orders and not matching_deals:
            return None
        position_ids = {
            getattr(item, "position_id", None)
            for item in [*matching_orders, *matching_deals]
            if getattr(item, "position_id", None) not in (None, 0)
        }
        if len(position_ids) > 1:
            raise PositionQueryError("ambiguous intent history match")
        latest_deal = max(matching_deals, key=lambda deal: getattr(deal, "time", 0), default=None)
        latest_order = max(matching_orders, key=lambda order: getattr(order, "time_done", 0), default=None)
        return IntentExecutionMatch(
            position_id=next(iter(position_ids), None),
            order_id=getattr(latest_order, "ticket", None) or getattr(latest_deal, "order", None),
            deal_id=getattr(latest_deal, "ticket", None),
            volume=float(getattr(latest_deal, "volume", 0.0)) if latest_deal else 0.0,
            source="history",
        )


    # =====================================
    # MODIFY SL
    # =====================================
    def modify_sl(self, position, new_sl):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "sl": self.normalize_price(new_sl),
            "tp": position.tp
        }

        request_id = f"modify_sl:{position.ticket}"
        last_result = None
        for attempt in range(1, self.retry_policy.operation_attempts + 1):
            if attempt > 1:
                try:
                    current = self.get_position_by_ticket(position.ticket)
                except PositionQueryError as exc:
                    self._event("broker_modify_retry_blocked", critical=True,
                                request_id=request_id, ticket=position.ticket,
                                reason=str(exc))
                    return None
                if current is None:
                    self._event("broker_modify_blocked_position_closed", critical=True,
                                request_id=request_id, ticket=position.ticket)
                    return None
                if self.normalize_price(getattr(current, "sl", 0.0)) == request["sl"]:
                    self._event("broker_modify_already_applied", request_id=request_id,
                                ticket=position.ticket)
                    return current
            started = self._monotonic()
            result = mt5.order_send(request)
            latency_ms = round((self._monotonic() - started) * 1000, 2)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                self._event("broker_modify_succeeded", request_id=request_id,
                            retcode=result.retcode, attempt=attempt, latency_ms=latency_ms)
                return result
            retcode = getattr(result, "retcode", None)
            if result is not None and retcode not in TRANSIENT_RETCODES:
                self._event("broker_modify_final", critical=True, request_id=request_id,
                            retcode=retcode, attempt=attempt, latency_ms=latency_ms)
                return result
            last_result = result
            self._event("broker_modify_transient", critical=True, request_id=request_id,
                        retcode=retcode, attempt=attempt, latency_ms=latency_ms)
            if attempt < self.retry_policy.operation_attempts:
                time.sleep(self.retry_policy.operation_backoff_sec)
        self._event("broker_modify_exhausted", critical=True, request_id=request_id,
                    retcode=getattr(last_result, "retcode", None))
        return last_result

    # =====================================
    # CLOSE POSITION
    # =====================================
    def close_position(self, position):
        """Close the provided position using its own symbol's tick/mode."""
        symbol = position.symbol
        tick = mt5.symbol_info_tick(symbol)

        if tick is None:
            return None

        symbol_info = mt5.symbol_info(symbol)
        digits = symbol_info.digits if symbol_info is not None else None

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position.volume,
            "type": order_type,
            "position": position.ticket,
            "price": round(price, digits) if digits is not None else price,
            "deviation": self.deviation_points,
            "magic": position.magic,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.detect_filling_mode(symbol),
        }

        close_side = "SELL" if position.type == mt5.POSITION_TYPE_BUY else "BUY"

        return self._send_close_with_retry(
            request, side=close_side, position_ticket=position.ticket,
            label="Close", price_symbol=symbol,
        )

    def close_position_by_ticket(self, ticket):
        """
        Close an account position by its broker ticket.

        A missing ticket is a successful idempotent outcome: another retry
        (or a manual broker-side close) may have completed the close already.
        """
        position = self.get_position_by_ticket(ticket)
        if position is None:
            self._log(f"Close skipped | ticket={ticket} already closed")
            return AlreadyClosedPosition(ticket)
        return self.close_position(position)
