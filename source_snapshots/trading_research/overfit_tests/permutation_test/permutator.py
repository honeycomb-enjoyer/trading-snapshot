import numpy as np
import pandas as pd


def _detect_weekend_gaps(df, expected_hours=1):
    """
    Detect bars after long time gaps (weekend / missing session).
    Works when df.index is DatetimeIndex.
    """
    delta_hours = pd.Series(df.index).diff().dt.total_seconds() / 3600.0
    weekend_mask = delta_hours > expected_hours + 0.5
    weekend_mask.iloc[0] = False
    return weekend_mask.to_numpy()


def get_permutation(df, seed=None):
    """
    Permute OHLC while preserving:
    - first bar exactly
    - approximate overall price path
    - weekend gap positions

    Expected input:
        DataFrame indexed by timestamp
        columns = open, high, low, close

    Returns:
        DataFrame with columns:
        timestamp, open, high, low, close
    """
    rng = np.random.default_rng(seed)

    required = ["open", "high", "low", "close"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column: {col}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Input dataframe must use DatetimeIndex")

    n = len(df)
    if n < 10:
        raise ValueError("Dataset too small for permutation")

    log_df = np.log(df[required].copy())

    # =========================
    # Relative price components
    # =========================
    prev_close = log_df["close"].shift(1)

    relative_open = (log_df["open"] - prev_close).to_numpy()
    relative_high = (log_df["high"] - log_df["open"]).to_numpy()
    relative_low = (log_df["low"] - log_df["open"]).to_numpy()
    relative_close = (log_df["close"] - log_df["open"]).to_numpy()

    weekend_mask = _detect_weekend_gaps(df)

    valid_idx = np.arange(1, n)

    weekend_idx = valid_idx[weekend_mask[1:]]
    normal_idx = valid_idx[~weekend_mask[1:]]

    # =========================
    # Shuffle intrabar structure
    # =========================
    intrabar_perm = rng.permutation(valid_idx)

    shuffled_high = relative_high.copy()
    shuffled_low = relative_low.copy()
    shuffled_close = relative_close.copy()

    shuffled_high[1:] = relative_high[intrabar_perm]
    shuffled_low[1:] = relative_low[intrabar_perm]
    shuffled_close[1:] = relative_close[intrabar_perm]

    # =========================
    # Shuffle open gaps
    # =========================
    shuffled_open = relative_open.copy()

    if len(normal_idx) > 0:
        normal_perm = rng.permutation(normal_idx)
        shuffled_open[normal_idx] = relative_open[normal_perm]

    if len(weekend_idx) > 0:
        weekend_perm = rng.permutation(weekend_idx)
        shuffled_open[weekend_idx] = relative_open[weekend_perm]

    # =========================
    # Rebuild series
    # =========================
    perm = np.zeros((n, 4))

    # First bar unchanged
    perm[0] = log_df.iloc[0].to_numpy()

    for i in range(1, n):
        new_open = perm[i - 1, 3] + shuffled_open[i]
        new_high = new_open + shuffled_high[i]
        new_low = new_open + shuffled_low[i]
        new_close = new_open + shuffled_close[i]

        perm[i, 0] = new_open
        perm[i, 1] = new_high
        perm[i, 2] = new_low
        perm[i, 3] = new_close

    perm = np.exp(perm)

    # IMPORTANT:
    # build dataframe explicitly to avoid duplicate timestamp columns
    perm_df = pd.DataFrame({
        "timestamp": list(df.index),
        "open": perm[:, 0],
        "high": perm[:, 1],
        "low": perm[:, 2],
        "close": perm[:, 3],
    })

    return perm_df


if __name__ == "__main__":
    idx = pd.date_range("2024-01-01", periods=100, freq="H")
    base = np.cumsum(np.random.randn(100) * 0.002) + 10

    df = pd.DataFrame({
        "open": np.exp(base),
        "high": np.exp(base + np.abs(np.random.randn(100) * 0.001)),
        "low": np.exp(base - np.abs(np.random.randn(100) * 0.001)),
        "close": np.exp(base + np.random.randn(100) * 0.0005),
    }, index=idx)

    perm_df = get_permutation(df, seed=42)

    print("Original first open:", df.iloc[0]["open"])
    print("Permuted first open:", perm_df.iloc[0]["open"])
    print("Original last close:", df.iloc[-1]["close"])
    print("Permuted last close:", perm_df.iloc[-1]["close"])
