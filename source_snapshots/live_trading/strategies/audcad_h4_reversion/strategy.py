import pandas as pd
from datetime import timedelta
from strategies.audcad_h4_reversion import config


class AUDCADH4ReversionStrategy:
    def __init__(self):
        self.range_high = None
        self.range_low = None
        self.mean_price = None
        self.atr = None
        self.last_h4_bar = None

        self.last_long_signal_bar = None
        self.last_short_signal_bar = None

        self.last_long_pre_alert_bar = None
        self.last_short_pre_alert_bar = None

        self.order_pending = False
        self.pending_since = None
        self.retry_after = None

    # ==========================================================
    # STARTUP + H4 RECALC
    # ==========================================================
    def update_h4_state(self, h4_df: pd.DataFrame):
        min_bars = max(
            config.RANGE_LOOKBACK + 5,
            config.ATR_PERIOD + 5
        )

        if len(h4_df) < min_bars:
            raise RuntimeError("Not enough H4 bars")

        latest_bar = h4_df.iloc[-1]
        self.last_h4_bar = latest_bar["timestamp"].isoformat()

        self.atr = self._compute_atr(h4_df)

        if self.atr is None or self.atr <= 0:
            return

        lookback_df = h4_df.iloc[-config.RANGE_LOOKBACK:]

        self.range_high = lookback_df["high"].max()
        self.range_low = lookback_df["low"].min()

        range_size = self.range_high - self.range_low

        if range_size <= 0:
            self.mean_price = None
            return

        self.mean_price = (
            self.range_low
            + range_size * 0.5
        )

    # ==========================================================
    # TICK SIGNAL CHECK
    # ==========================================================
    def check_entry_signal(self, tick):
        now = tick.timestamp

        if self.order_pending:
            return None

        if self.retry_after and now < self.retry_after:
            return None

        if (
            self.range_high is None
            or self.range_low is None
            or self.mean_price is None
            or self.atr is None
        ):
            return None

        # SELL on upper sweep (use BID)
        if tick.bid >= self.range_high:
            if self.last_short_signal_bar == self.last_h4_bar:
                return None
            return self._build_sell_signal()

        # BUY on lower sweep (use ASK)
        if tick.ask <= self.range_low:
            if self.last_long_signal_bar == self.last_h4_bar:
                return None
            return self._build_buy_signal()

        return None

    # ==========================================================
    # PRE-ALERT CHECK
    # ==========================================================
    def check_pre_alert(self, tick):
        if (
            self.range_high is None
            or self.range_low is None
            or self.mean_price is None
            or self.atr is None
        ):
            return None

        distance = config.PRE_ALERT_DISTANCE_POINTS

        # SELL setup
        sell_distance = min(
            abs(self.range_high - tick.bid),
            abs(self.range_high - tick.ask)
        )

        if sell_distance <= distance:
            if self.last_short_signal_bar != self.last_h4_bar:
                if self.last_short_pre_alert_bar != self.last_h4_bar:

                    self.last_short_pre_alert_bar = self.last_h4_bar

                    stop_distance = (
                        self.atr *
                        config.STOP_LOSS
                    )

                    tp_distance = (
                        (self.range_high - self.mean_price)
                        * config.TAKE_PROFIT
                    )

                    return {
                        "strategy": config.STRATEGY_NAME,
                        "side": "SELL",
                        "trigger": self.range_high,
                        "distance_points": sell_distance,
                        "expected_entry": self.range_high,
                        "stop_distance": stop_distance,
                        "tp_distance": tp_distance
                    }

        # BUY setup
        buy_distance = min(
            abs(self.range_low - tick.bid),
            abs(self.range_low - tick.ask)
        )

        if buy_distance <= distance:
            if self.last_long_signal_bar != self.last_h4_bar:
                if self.last_long_pre_alert_bar != self.last_h4_bar:

                    self.last_long_pre_alert_bar = self.last_h4_bar

                    stop_distance = (
                        self.atr *
                        config.STOP_LOSS
                    )

                    tp_distance = (
                        (self.mean_price - self.range_low)
                        * config.TAKE_PROFIT
                    )

                    return {
                        "strategy": config.STRATEGY_NAME,
                        "side": "BUY",
                        "trigger": self.range_low,
                        "distance_points": buy_distance,
                        "expected_entry": self.range_low,
                        "stop_distance": stop_distance,
                        "tp_distance": tp_distance
                    }

        return None

    # ==========================================================
    # EXECUTION CALLBACKS
    # ==========================================================
    def mark_order_pending(self, now):
        self.order_pending = True
        self.pending_since = now

    def register_filled_entry(self, side):
        self.order_pending = False
        self.pending_since = None
        self.retry_after = None

        if side == "BUY":
            self.last_long_signal_bar = self.last_h4_bar
        else:
            self.last_short_signal_bar = self.last_h4_bar

    def register_rejected_order(self, now):
        self.order_pending = False
        self.pending_since = None
        self.retry_after = (
            now +
            timedelta(seconds=config.ORDER_RETRY_COOLDOWN_SEC)
        )

    def register_skipped_signal(self, side):
        self.order_pending = False
        self.pending_since = None
        self.retry_after = None

        if side == "BUY":
            self.last_long_signal_bar = self.last_h4_bar
        else:
            self.last_short_signal_bar = self.last_h4_bar

    # ==========================================================
    # SIGNAL BUILDERS
    # ==========================================================
    def _build_buy_signal(self):
        expected_entry = self.range_low

        stop_distance = (
            self.atr *
            config.STOP_LOSS
        )

        tp_distance = (
            (self.mean_price - expected_entry)
            * config.TAKE_PROFIT
        )

        if tp_distance <= 0:
            return None

        return {
            "side": "BUY",
            "expected_entry": expected_entry,
            "stop_distance": stop_distance,
            "tp_distance": tp_distance,
        }

    def _build_sell_signal(self):
        expected_entry = self.range_high

        stop_distance = (
            self.atr *
            config.STOP_LOSS
        )

        tp_distance = (
            (expected_entry - self.mean_price)
            * config.TAKE_PROFIT
        )

        if tp_distance <= 0:
            return None

        return {
            "side": "SELL",
            "expected_entry": expected_entry,
            "stop_distance": stop_distance,
            "tp_distance": tp_distance,
        }

    # ==========================================================
    # ATR
    # ==========================================================
    def _compute_atr(self, df):
        period = config.ATR_PERIOD

        high = df["high"]
        low = df["low"]
        close = df["close"]

        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]

        if pd.isna(atr):
            return None

        return float(atr)

    # ==========================================================
    # STATE RESTORE
    # ==========================================================
    def restore_from_state(self, state_manager):
        strategy_state = state_manager.get_strategy()

        self.last_long_signal_bar = strategy_state.get(
            "last_long_signal_bar"
        )

        self.last_short_signal_bar = strategy_state.get(
            "last_short_signal_bar"
        )

    # ==========================================================
    # STATE SAVE
    # ==========================================================
    def save_to_state(self, state_manager):
        state_manager.set_strategy_value(
            "last_long_signal_bar",
            self.last_long_signal_bar
        )

        state_manager.set_strategy_value(
            "last_short_signal_bar",
            self.last_short_signal_bar
        )