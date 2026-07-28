from data.data_split_config import WALKFORWARD_CONFIG


def generate_walkforward_windows(
    df,
    mode,
    train_window,
    test_window,
    step_window
):
    """
    Generates walk-forward windows in BAR SPACE.

    Returns:
        [
            {
                train_start_idx
                train_end_idx
                test_start_idx
                test_end_idx
            }
        ]
    """

    total_bars = len(df)
    windows = []

    if mode not in ("rolling", "anchored"):
        raise ValueError("mode must be rolling or anchored")

    train_start_idx = 0

    while True:

        if mode == "rolling":
            current_train_start = train_start_idx
        else:
            current_train_start = 0

        current_train_end = current_train_start + train_window
        current_test_start = current_train_end
        current_test_end = current_test_start + test_window

        if current_test_end > total_bars:
            break

        windows.append({
            "train_start_idx": current_train_start,
            "train_end_idx": current_train_end,
            "test_start_idx": current_test_start,
            "test_end_idx": current_test_end
        })

        train_start_idx += step_window

    return windows


def print_windows_summary(df, windows):
    print()
    print("========== WALKFORWARD WINDOWS ==========")
    print(f"Total windows: {len(windows)}")
    print()

    for i, w in enumerate(windows, 1):

        train_start_ts = df.iloc[w["train_start_idx"]]["timestamp"]
        train_end_ts = df.iloc[w["train_end_idx"] - 1]["timestamp"]

        test_start_ts = df.iloc[w["test_start_idx"]]["timestamp"]
        test_end_ts = df.iloc[w["test_end_idx"] - 1]["timestamp"]

        train_bars = w["train_end_idx"] - w["train_start_idx"]
        test_bars = w["test_end_idx"] - w["test_start_idx"]

        train_days = round(train_bars / 24, 1)
        test_days = round(test_bars / 24, 1)

        print(f"Window {i}")
        print(
            f" Train: {train_start_ts} -> {train_end_ts} "
            f"({train_days} days)"
        )
        print(
            f" Test : {test_start_ts} -> {test_end_ts} "
            f"({test_days} days)"
        )
        print()

    print("=========================================")