from __future__ import annotations

from datetime import timedelta
import math

import pandas as pd

from strategies.xau_h4_continuation_breakout import config


class XAUH4ContinuationBreakoutStrategy:
    def __init__(self):
        self.range_high = None
        self.range_low = None
        self.atr = None
        self.last_h4_bar = None
        self.completed_week_returns = {}

        self.last_long_signal_bar = None
        self.last_short_signal_bar = None

        self.order_pending = False
        self.pending_since = None
        self.retry_after = None
        self._state_dirty = False

    # ==========================================================
    # STARTUP + H4 RECALC
    # ==========================================================
    def update_h4_state(self, h4_df: pd.DataFrame):
        min_bars = max(config.LOOKBACK + 5, config.ATR_PERIOD + 5)
        if len(h4_df) < min_bars:
            raise RuntimeError("Not enough H4 bars")
        if "timestamp" not in h4_df:
            raise RuntimeError("Return filter requires a timestamp column")

        frame = h4_df.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        self._validate_base_interval(frame["timestamp"])

        latest_bar = frame.iloc[-1]
        self.last_h4_bar = latest_bar["timestamp"].isoformat()
        self.atr = self._compute_atr(frame)
        if self.atr is None or self.atr <= 0:
            return

        lookback_df = frame.iloc[-config.LOOKBACK:]
        self.range_high = float(lookback_df["high"].max())
        self.range_low = float(lookback_df["low"].min())
        if self.range_high <= self.range_low:
            self.range_high = None
            self.range_low = None
            return
        self.completed_week_returns = self._build_completed_returns(frame)

    # ==========================================================
    # TICK SIGNAL CHECK
    # ==========================================================
    def check_entry_signal(self, tick):
        now = tick.timestamp
        if self.order_pending or (self.retry_after and now < self.retry_after):
            return None
        if not self._state_ready():
            return None

        buy_breakout = tick.ask >= self.range_high
        sell_breakout = tick.bid <= self.range_low

        # OHLC backtest skips a bar that breaks both boundaries because it
        # cannot know which side triggered first. A simultaneous live touch is
        # treated the same way and the current H4 bar is marked as consumed.
        if buy_breakout and sell_breakout:
            self.last_long_signal_bar = self.last_h4_bar
            self.last_short_signal_bar = self.last_h4_bar
            self._state_dirty = True
            return None

        allowed_side = self._allowed_side(self._tick_utc_timestamp(tick))
        if (
            buy_breakout
            and self._direction_allows("BUY")
            and allowed_side in ("BUY", "BOTH")
        ):
            if self.last_long_signal_bar == self.last_h4_bar:
                return None
            return self._build_buy_signal()
        if (
            sell_breakout
            and self._direction_allows("SELL")
            and allowed_side in ("SELL", "BOTH")
        ):
            if self.last_short_signal_bar == self.last_h4_bar:
                return None
            return self._build_sell_signal()
        return None

    # ==========================================================
    # PRE-ALERT CHECK
    # ==========================================================
    def check_pre_alert(self, tick):
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
        self.retry_after = now + timedelta(seconds=config.ORDER_RETRY_COOLDOWN_SEC)

    def register_skipped_signal(self, side):
        self.order_pending = False
        self.pending_since = None
        self.retry_after = None
        if side == "BUY":
            self.last_long_signal_bar = self.last_h4_bar
        else:
            self.last_short_signal_bar = self.last_h4_bar

    def consume_state_dirty(self):
        dirty = self._state_dirty
        self._state_dirty = False
        return dirty

    # ==========================================================
    # SIGNAL BUILDERS
    # ==========================================================
    def _build_buy_signal(self):
        expected_entry = self.range_high
        stop_distance = self.atr * config.STOP_LOSS
        if stop_distance <= 0:
            return None
        return {
            "side": "BUY",
            "expected_entry": expected_entry,
            "stop_distance": stop_distance,
            "tp_distance": stop_distance * config.TAKE_PROFIT,
        }

    def _build_sell_signal(self):
        expected_entry = self.range_low
        stop_distance = self.atr * config.STOP_LOSS
        if stop_distance <= 0:
            return None
        return {
            "side": "SELL",
            "expected_entry": expected_entry,
            "stop_distance": stop_distance,
            "tp_distance": stop_distance * config.TAKE_PROFIT,
        }

    # ==========================================================
    # RETURN FILTER
    # ==========================================================
    def _build_completed_returns(self, frame):
        timestamps = frame["timestamp"]
        keys = self._filter_keys(timestamps)
        grouped = pd.DataFrame({
            "key": keys,
            "timestamp": timestamps,
            "open": frame["open"].to_numpy(dtype=float),
            "close": frame["close"].to_numpy(dtype=float),
        }).groupby("key", sort=False).agg(
            first_timestamp=("timestamp", "first"),
            open=("open", "first"),
            close=("close", "last"),
        )

        completed_return = (grouped["close"] / grouped["open"]).map(math.log)
        if not self._is_expected_group_start(grouped.iloc[0]["first_timestamp"]):
            completed_return.iloc[0] = float("nan")
        return {key: float(value) for key, value in completed_return.items()}

    def _allowed_side(self, utc_now):
        if not config.USE_RETURN_FILTER:
            return "BOTH"
        current_key = self._filter_key(utc_now)
        previous_keys = [key for key in self.completed_week_returns if key < current_key]
        if not previous_keys:
            return None
        value = self.completed_week_returns[max(previous_keys)]
        if not math.isfinite(value) or value == 0:
            return None
        continuation_side = "BUY" if value > 0 else "SELL"
        if config.RETURN_FILTER_MODE == "continuation":
            return continuation_side
        return "SELL" if continuation_side == "BUY" else "BUY"

    def _filter_keys(self, timestamps):
        local_wall = timestamps.dt.tz_convert(config.FILTER_TIMEZONE).dt.tz_localize(None)
        shifted = local_wall - pd.Timedelta(hours=config.FILTER_ROLLOVER_HOUR)
        if config.RETURN_FILTER_TIMEFRAME == "D1":
            return shifted.dt.floor("D")
        return shifted.dt.to_period("W-SAT").dt.start_time

    def _filter_key(self, timestamp):
        series = pd.Series([self._as_utc_timestamp(timestamp)])
        return self._filter_keys(series).iloc[0]

    def _is_expected_group_start(self, timestamp):
        local = self._as_utc_timestamp(timestamp).tz_convert(config.FILTER_TIMEZONE)
        if local.hour != config.FILTER_ROLLOVER_HOUR or local.minute != 0:
            return False
        return config.RETURN_FILTER_TIMEFRAME == "D1" or local.dayofweek == 6

    def _validate_base_interval(self, timestamps):
        differences = timestamps.diff().dropna()
        positive = differences[differences > pd.Timedelta(0)]
        if positive.empty:
            raise RuntimeError("Return filter requires at least two timestamped bars")
        filter_interval = pd.Timedelta(days=1) if config.RETURN_FILTER_TIMEFRAME == "D1" else pd.Timedelta(weeks=1)
        if positive.min() >= filter_interval:
            raise RuntimeError("return_filter_timeframe must be higher than the strategy data timeframe")

    # ==========================================================
    # ATR + HELPERS
    # ==========================================================
    def _compute_atr(self, df):
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(config.ATR_PERIOD).mean().iloc[-1]
        return None if pd.isna(atr) else float(atr)

    def _state_ready(self):
        return all(value is not None for value in (
            self.range_high, self.range_low, self.atr,
        ))

    @staticmethod
    def _direction_allows(side):
        direction = str(config.DIRECTION).lower()
        if direction == "both":
            return True
        if direction == "long":
            return side == "BUY"
        if direction == "short":
            return side == "SELL"
        raise RuntimeError(f"Unsupported DIRECTION: {config.DIRECTION}")

    def _tick_utc_timestamp(self, tick):
        return self._as_utc_timestamp(getattr(tick, "utc_timestamp", tick.timestamp))

    @staticmethod
    def _as_utc_timestamp(value):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            return timestamp.tz_localize("UTC")
        return timestamp.tz_convert("UTC")

    # ==========================================================
    # STATE RESTORE + SAVE
    # ==========================================================
    def restore_from_state(self, state_manager):
        strategy_state = state_manager.get_strategy()
        self.last_long_signal_bar = strategy_state.get("last_long_signal_bar")
        self.last_short_signal_bar = strategy_state.get("last_short_signal_bar")

    def save_to_state(self, state_manager):
        state_manager.set_strategy_value("last_long_signal_bar", self.last_long_signal_bar)
        state_manager.set_strategy_value("last_short_signal_bar", self.last_short_signal_bar)
