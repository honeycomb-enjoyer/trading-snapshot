"""Basic mean reversion gated by the previous higher-timeframe return."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.basic_mean_reversion import BasicMeanReversion


class MeanReversionWithReturnFilter(BasicMeanReversion):
    """Preserve the base signal and filter its side with a completed D1/W1 bar."""

    FILTER_TIMEZONE = "America/New_York"
    FILTER_ROLLOVER_HOUR = 17
    VALID_FILTER_TIMEFRAMES = frozenset({"D1", "W1"})
    VALID_FILTER_MODES = frozenset({"continuation", "reversion"})

    def __init__(
        self,
        range_lookback=30,
        atr_period=14,
        atr_multiplier=1.5,
        tp_fraction=1.0,
        use_return_filter=False,
        return_filter_timeframe="W1",
        return_filter_mode="reversion",
    ):
        super().__init__(
            range_lookback=range_lookback,
            atr_period=atr_period,
            atr_multiplier=atr_multiplier,
            tp_fraction=tp_fraction,
        )
        self.use_return_filter = bool(use_return_filter)
        self.return_filter_timeframe = str(return_filter_timeframe).upper()
        self.return_filter_mode = str(return_filter_mode).lower()
        if self.return_filter_timeframe not in self.VALID_FILTER_TIMEFRAMES:
            raise ValueError("return_filter_timeframe must be 'D1' or 'W1'")
        if self.return_filter_mode not in self.VALID_FILTER_MODES:
            raise ValueError(
                "return_filter_mode must be 'continuation' or 'reversion'"
            )

    def bind_data(self, df):
        super().bind_data(df)
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
        # If history begins inside an HTF bar, its partial return must never
        # become the filter for the next period.
        if not self._is_expected_group_start(grouped.iloc[0]["first_timestamp"]):
            completed_return.iloc[0] = np.nan

        previous_return = completed_return.shift(1)
        side_by_key = {}
        for key, value in previous_return.items():
            if not np.isfinite(value) or value == 0:
                side_by_key[key] = None
                continue
            continuation_side = "BUY" if value > 0 else "SELL"
            if self.return_filter_mode == "continuation":
                side_by_key[key] = continuation_side
            else:
                side_by_key[key] = "SELL" if continuation_side == "BUY" else "BUY"

        self.return_filter_side = np.array(
            [side_by_key.get(key) for key in keys], dtype=object
        )

    def on_bar(self, i, df=None):
        signal = super().on_bar(i, df)
        if signal is None or not self.use_return_filter:
            return signal
        if self.return_filter_side is None:
            raise RuntimeError("Return filter data is not bound")
        allowed_side = self.return_filter_side[i]
        if allowed_side is None or signal["side"] != allowed_side:
            return None
        return signal

    def _filter_keys(self, timestamps):
        local_wall = (
            timestamps.dt.tz_convert(self.FILTER_TIMEZONE).dt.tz_localize(None)
        )
        shifted = local_wall - pd.Timedelta(hours=self.FILTER_ROLLOVER_HOUR)
        if self.return_filter_timeframe == "D1":
            return shifted.dt.floor("D")
        # W-SAT starts on Sunday, matching the FX week that opens at the
        # configured New York rollover and closes on Friday.
        return shifted.dt.to_period("W-SAT").dt.start_time

    def _is_expected_group_start(self, timestamp):
        utc_timestamp = pd.Timestamp(timestamp)
        local = utc_timestamp.tz_convert(self.FILTER_TIMEZONE)
        if local.hour != self.FILTER_ROLLOVER_HOUR or local.minute != 0:
            return False
        return (
            self.return_filter_timeframe == "D1"
            or local.dayofweek == 6
        )

    def _validate_base_interval(self, timestamps):
        differences = timestamps.diff().dropna()
        positive = differences[differences > pd.Timedelta(0)]
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
