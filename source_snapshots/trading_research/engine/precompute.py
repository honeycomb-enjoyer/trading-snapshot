# engine/precompute.py

import numpy as np
import pandas as pd


# ====================================
# SWINGS
# ====================================
def compute_swings(df, swing_window=2):
    """
    Detect confirmed swing points.

    IMPORTANT

    Swing at bar i becomes known only after
    swing_window future candles exist.

    Therefore labels are intentionally shifted
    forward by swing_window bars to eliminate
    lookahead bias.

    Example (window=2):

        bar 10 = swing high

    It becomes available only on bar 12.

    swing_high[12] = 1

    NOT swing_high[10].
    """

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    n = len(df)
    w = swing_window

    swing_high = np.zeros(n, dtype=np.int8)
    swing_low = np.zeros(n, dtype=np.int8)

    for i in range(w, n - w):

        if (
            highs[i] >= highs[i - w:i].max()
            and highs[i] >= highs[i + 1:i + w + 1].max()
        ):
            confirm = i + w
            if confirm < n:
                swing_high[confirm] = 1

        if (
            lows[i] <= lows[i - w:i].min()
            and lows[i] <= lows[i + 1:i + w + 1].min()
        ):
            confirm = i + w
            if confirm < n:
                swing_low[confirm] = 1

    return swing_high, swing_low


# ====================================
# TREND LABELS
# ====================================
def compute_trend_labels(
    df,
    swing_high,
    swing_low,
    trend_lookback=300
):
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    n = len(df)

    trend = np.full(n, "none", dtype=object)

    for i in range(max(200, trend_lookback), n):

        start = max(0, i - trend_lookback)

        swing_high_values = highs[start:i][
            swing_high[start:i] == 1
        ]

        swing_low_values = lows[start:i][
            swing_low[start:i] == 1
        ]

        if len(swing_high_values) < 2:
            continue

        if len(swing_low_values) < 2:
            continue

        sh1, sh2 = swing_high_values[-2:]
        sl1, sl2 = swing_low_values[-2:]

        if sh2 >= sh1 and sl2 >= sl1:
            trend[i] = "bull"

        elif sh2 <= sh1 and sl2 <= sl1:
            trend[i] = "bear"

    return trend


# ====================================
# ATR
# ====================================
def compute_atr(df, atr_period):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(atr_period).mean()

    return atr.to_numpy()


# ====================================
# ENRICH DF
# ====================================
def enrich_dataframe(
    df,
    swing_window=2,
    atr_periods=(20, 40, 60),
    silent=True,
    include_swings=True,
    include_trend=True,
):
    df = df.copy()

    if include_swings or include_trend:
        if not silent:
            print("Precomputing swings...")
        swing_high, swing_low = compute_swings(df, swing_window)
        df["swing_high"] = swing_high
        df["swing_low"] = swing_low

        if include_trend:
            if not silent:
                print("Precomputing trend...")
            df["trend"] = compute_trend_labels(df, swing_high, swing_low)

    if not silent:
        print("Precomputing ATR...")

    for period in atr_periods:
        df[f"atr_{period}"] = compute_atr(df, period)

    if not silent:
        print("Precompute complete.")

    return df


def _configured_values(config, key):
    if key not in config:
        return ()
    value = config[key]
    if isinstance(value, (list, tuple, set, range)):
        return tuple(value)
    return (value,)


def precompute_for_params(df, params_or_grid, silent=True):
    """Compute only features requested by strategy params or an optimizer grid."""
    atr_periods = tuple(dict.fromkeys(
        int(value) for value in _configured_values(params_or_grid, "atr_period")
    ))
    trend_values = _configured_values(params_or_grid, "use_trend_filter")
    include_trend = any(bool(value) for value in trend_values)
    swing_values = _configured_values(params_or_grid, "swing_window")
    include_swings = bool(swing_values) or include_trend

    if len(set(swing_values)) > 1:
        raise ValueError("A param_grid may contain only one swing_window because the trend column is shared")
    swing_window = int(swing_values[0]) if swing_values else 2

    if not atr_periods and not include_swings:
        return df.copy()
    return enrich_dataframe(
        df,
        swing_window=swing_window,
        atr_periods=atr_periods,
        silent=silent,
        include_swings=include_swings,
        include_trend=include_trend,
    )


# ====================================
# WRAPPER
# ====================================
def precompute_trend(
    df,
    swing_window=2,
    atr_periods=(20, 40, 60),
    silent=True
):
    return enrich_dataframe(
        df,
        swing_window=swing_window,
        atr_periods=atr_periods,
        silent=silent
    )
