# strategy/audcad_h4_reversion.py

import numpy as np


class BasicMeanReversion:
    """
    ==========================================================
    TAKE PROFIT CALCULATION

    tp_fraction =
    fraction of distance from ENTRY to range midpoint.

    1.0 -> full move to midpoint
    0.5 -> halfway
    ==========================================================
    """

    def __init__(
        self,
        range_lookback=30,
        atr_period=14,
        atr_multiplier=1.5,
        tp_fraction=1.0,
    ):
        self.range_lookback = range_lookback
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.tp_fraction = tp_fraction

    # ==========================================================
    # DATA
    # ==========================================================

    def bind_data(self, df):

        self.high = df["high"].to_numpy()
        self.low = df["low"].to_numpy()
        self.close = df["close"].to_numpy()

        atr_col = f"atr_{self.atr_period}"

        if atr_col not in df.columns:
            raise RuntimeError(
                f"Missing ATR column: {atr_col}"
            )

        self.atr = df[atr_col].to_numpy()

    # ==========================================================
    # MAIN
    # ==========================================================

    def on_bar(self, i, df=None):

        min_bars = max(
            self.range_lookback,
            self.atr_period,
        )

        if i < min_bars:
            return None

        # The current bar is still forming when its range boundary is touched.
        # Only the ATR of the last fully closed bar is available at entry time.
        atr_value = self.atr[i - 1]

        if np.isnan(atr_value) or atr_value <= 0:
            return None

        start = i - self.range_lookback
        end = i

        highest_high = np.max(self.high[start:end])
        lowest_low = np.min(self.low[start:end])

        range_size = highest_high - lowest_low

        if range_size <= 0:
            return None

        mean_price = lowest_low + range_size * 0.5

        current_high = self.high[i]
        current_low = self.low[i]

        # ======================================================
        # SELL
        # ======================================================

        if current_high >= highest_high:

            entry = highest_high

            risk = atr_value * self.atr_multiplier

            sl = entry + risk

            tp = (
                entry
                - (entry - mean_price)
                * self.tp_fraction
            )

            if tp >= entry:
                return None

            return {
                "side": "SELL",
                "entry": float(entry),
                "entry_trigger": "price_at_or_above",
                "sl": float(sl),
                "tp": float(tp),
            }

        # ======================================================
        # BUY
        # ======================================================

        if current_low <= lowest_low:

            entry = lowest_low

            risk = atr_value * self.atr_multiplier

            sl = entry - risk

            tp = (
                entry
                + (mean_price - entry)
                * self.tp_fraction
            )

            if tp <= entry:
                return None

            return {
                "side": "BUY",
                "entry": float(entry),
                "entry_trigger": "price_at_or_below",
                "sl": float(sl),
                "tp": float(tp),
            }

        return None
