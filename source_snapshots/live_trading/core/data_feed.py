# core/data_feed.py

import MetaTrader5 as mt5
import pandas as pd

from core.broker_clock import BrokerClock


TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M2": mt5.TIMEFRAME_M2,
    "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,
    "M10": mt5.TIMEFRAME_M10,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class TickWrapper:
    def __init__(
        self, mt5_tick, clock: BrokerClock, *, previous_raw_time=None,
        bar_timestamp=None, bar_open=None,
    ):
        self.bid = mt5_tick.bid
        self.ask = mt5_tick.ask
        self.raw_time = float(mt5_tick.time)
        self.utc_timestamp = clock.normalize_live_tick(
            self.raw_time,
            previous_raw_epoch=previous_raw_time,
        )
        self.timestamp = self.utc_timestamp.replace(tzinfo=None)
        self.time = self.utc_timestamp.timestamp()
        self.bar_timestamp = bar_timestamp
        self.bar_open = bar_open


class DataFeed:
    def __init__(self, symbol, timeframe, *, clock=None):
        self.symbol = symbol
        self.clock = clock or BrokerClock()

        if isinstance(timeframe, str):
            if timeframe not in TIMEFRAME_MAP:
                raise RuntimeError(f"Unknown timeframe: {timeframe}")
            self.timeframe = TIMEFRAME_MAP[timeframe]
        else:
            self.timeframe = timeframe

        self.last_seen_bar = None
        self.last_seen_bar_open = None
        self.last_tick_raw_time = None

    # =====================================
    # TICK
    # =====================================
    def get_tick(self):
        tick = mt5.symbol_info_tick(self.symbol)

        if tick is None:
            return None

        wrapped = TickWrapper(
            tick,
            self.clock,
            previous_raw_time=self.last_tick_raw_time,
            bar_timestamp=self.last_seen_bar,
            bar_open=self.last_seen_bar_open,
        )
        self.last_tick_raw_time = wrapped.raw_time
        return wrapped

    # =====================================
    # CLOSED BARS ONLY (NO FUTURE LEAK)
    # =====================================
    def get_bars(self, bars=500):
        rates = mt5.copy_rates_from_pos(
            self.symbol,
            self.timeframe,
            1,  # <- start from last CLOSED candle
            bars
        )

        if rates is None:
            return None

        df = pd.DataFrame(rates)

        if df.empty:
            return None

        df["timestamp"] = [self.clock.normalize_epoch(value) for value in df["time"]]

        if "tick_volume" in df.columns:
            df = df.rename(columns={"tick_volume": "volume"})
        else:
            df["volume"] = 0

        df = df[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        ]

        return df

    # =====================================
    # LIVE BAR FOR NEW BAR DETECTION
    # =====================================
    def _get_live_bars(self):
        rates = mt5.copy_rates_from_pos(
            self.symbol,
            self.timeframe,
            0,
            2
        )

        if rates is None:
            return None

        df = pd.DataFrame(rates)

        if df.empty:
            return None

        df["timestamp"] = [self.clock.normalize_epoch(value) for value in df["time"]]
        return df

    # =====================================
    # NEW BAR DETECTION
    # =====================================
    def is_new_bar(self):
        bars = self._get_live_bars()

        if bars is None:
            return False

        latest_bar_time = bars.iloc[-1]["timestamp"]
        self.last_seen_bar_open = float(bars.iloc[-1]["open"])

        if self.last_seen_bar is None:
            self.last_seen_bar = latest_bar_time
            return False

        if latest_bar_time > self.last_seen_bar:
            self.last_seen_bar = latest_bar_time
            return True

        return False

    # =====================================
    # STARTUP SYNC
    # =====================================
    def sync_last_bar(self):
        bars = self._get_live_bars()

        if bars is None:
            return

        self.last_seen_bar = bars.iloc[-1]["timestamp"]
        self.last_seen_bar_open = float(bars.iloc[-1]["open"])

    # =====================================
    # SNAPSHOT
    # =====================================
    def status_snapshot(self):
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "last_seen_bar": self.last_seen_bar,
            "clock": self.clock.status_snapshot(),
        }
