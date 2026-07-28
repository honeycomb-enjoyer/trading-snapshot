"""Intrabar range breakout gated by a completed higher-timeframe return."""

from __future__ import annotations

import numpy as np
import pandas as pd


class ContinuationBreakout:
    """Break the previous N-bar high/low with ATR risk and fixed reward/risk."""

    FILTER_TIMEZONE = "America/New_York"
    FILTER_ROLLOVER_HOUR = 17
    VALID_DIRECTIONS = frozenset({"both", "long", "short"})
    VALID_FILTER_TIMEFRAMES = frozenset({"D1", "W1"})
    VALID_FILTER_MODES = frozenset({"continuation", "reversion"})

    def __init__(
        self,
        lookback=20,
        atr_period=20,
        sl_atr=1.5,
        rr=3.0,
        direction="both",
        use_return_filter=False,
        return_filter_timeframe="D1",
        return_filter_mode="continuation",
    ):
        self.lookback = int(lookback)
        self.atr_period = int(atr_period)
        self.sl_atr = float(sl_atr)
        self.rr = float(rr)
        self.direction = str(direction).lower()
        self.use_return_filter = bool(use_return_filter)
        self.return_filter_timeframe = str(return_filter_timeframe).upper()
        self.return_filter_mode = str(return_filter_mode).lower()

        if self.lookback <= 0 or self.atr_period <= 0:
            raise ValueError("lookback and atr_period must be positive")
        if self.sl_atr <= 0 or self.rr <= 0:
            raise ValueError("sl_atr and rr must be positive")
        if self.direction not in self.VALID_DIRECTIONS:
            raise ValueError("direction must be 'both', 'long', or 'short'")
        if self.return_filter_timeframe not in self.VALID_FILTER_TIMEFRAMES:
            raise ValueError("return_filter_timeframe must be 'D1' or 'W1'")
        if self.return_filter_mode not in self.VALID_FILTER_MODES:
            raise ValueError(
                "return_filter_mode must be 'continuation' or 'reversion'"
            )

    def bind_data(self, df):
        self.high = df["high"].to_numpy(dtype=float)
        self.low = df["low"].to_numpy(dtype=float)
        atr_column = f"atr_{self.atr_period}"
        if atr_column not in df:
            raise RuntimeError(f"Missing ATR column: {atr_column}")
        self.atr = df[atr_column].to_numpy(dtype=float)
        self.return_filter_side = None
        if not self.use_return_filter:
            return
        if "timestamp" not in df:
            raise RuntimeError("Return filter requires a timestamp column")

        timestamps = pd.Series(pd.to_datetime(df["timestamp"], utc=True))
        self._validate_base_interval(timestamps)
        keys = self._filter_keys(timestamps)
        grouped = pd.DataFrame({
            "key": keys,
            "timestamp": timestamps,
            "open": df["open"].to_numpy(dtype=float),
            "close": df["close"].to_numpy(dtype=float),
        }).groupby("key", sort=False).agg(
            first_timestamp=("timestamp", "first"),
            open=("open", "first"),
            close=("close", "last"),
        )

        completed_return = np.log(grouped["close"] / grouped["open"])
        if not self._is_expected_group_start(grouped.iloc[0]["first_timestamp"]):
            completed_return.iloc[0] = np.nan
        previous_return = completed_return.shift(1)
        side_by_key = {}
        for key, value in previous_return.items():
            if not np.isfinite(value) or value == 0:
                side_by_key[key] = None
                continue
            continuation_side = "BUY" if value > 0 else "SELL"
            side_by_key[key] = (
                continuation_side
                if self.return_filter_mode == "continuation"
                else ("SELL" if continuation_side == "BUY" else "BUY")
            )
        self.return_filter_side = np.array(
            [side_by_key.get(key) for key in keys], dtype=object,
        )

    def on_bar(self, i, df=None):
        if i < max(self.lookback, self.atr_period):
            return None
        atr = self.atr[i - 1]
        if not np.isfinite(atr) or atr <= 0:
            return None

        start = i - self.lookback
        upper = float(np.max(self.high[start:i]))
        lower = float(np.min(self.low[start:i]))
        if upper <= lower:
            return None

        buy_breakout = self.high[i] >= upper
        sell_breakout = self.low[i] <= lower
        # OHLC cannot reveal which boundary broke first inside a two-sided bar.
        if buy_breakout and sell_breakout:
            return None
        allowed_side = self._allowed_filter_side(i)
        risk = float(atr * self.sl_atr)

        if buy_breakout and self.direction != "short":
            if allowed_side not in (None, "BUY"):
                return None
            return {
                "side": "BUY",
                "entry": upper,
                "entry_trigger": "price_at_or_above",
                "sl": upper - risk,
                "tp": upper + risk * self.rr,
            }

        if sell_breakout and self.direction != "long":
            if allowed_side not in (None, "SELL"):
                return None
            return {
                "side": "SELL",
                "entry": lower,
                "entry_trigger": "price_at_or_below",
                "sl": lower + risk,
                "tp": lower - risk * self.rr,
            }
        return None

    def _allowed_filter_side(self, index):
        if not self.use_return_filter:
            return None
        if self.return_filter_side is None:
            raise RuntimeError("Return filter data is not bound")
        # A missing completed higher-timeframe signal blocks both sides.
        return self.return_filter_side[index] or "BLOCK"

    def _filter_keys(self, timestamps):
        local_wall = (
            timestamps.dt.tz_convert(self.FILTER_TIMEZONE).dt.tz_localize(None)
        )
        shifted = local_wall - pd.Timedelta(hours=self.FILTER_ROLLOVER_HOUR)
        if self.return_filter_timeframe == "D1":
            return shifted.dt.floor("D")
        return shifted.dt.to_period("W-SAT").dt.start_time

    def _is_expected_group_start(self, timestamp):
        local = pd.Timestamp(timestamp).tz_convert(self.FILTER_TIMEZONE)
        if local.hour != self.FILTER_ROLLOVER_HOUR or local.minute != 0:
            return False
        return self.return_filter_timeframe == "D1" or local.dayofweek == 6

    def _validate_base_interval(self, timestamps):
        positive = timestamps.diff().dropna()
        positive = positive[positive > pd.Timedelta(0)]
        if positive.empty:
            raise RuntimeError("Return filter requires at least two timestamped bars")
        base_interval = positive.min()
        filter_interval = (
            pd.Timedelta(days=1)
            if self.return_filter_timeframe == "D1"
            else pd.Timedelta(weeks=1)
        )
        if base_interval >= filter_interval:
            raise RuntimeError(
                "return_filter_timeframe must be higher than the strategy data timeframe"
            )
