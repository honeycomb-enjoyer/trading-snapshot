import pandas as pd
import pytest

from engine.precompute import precompute_for_params
from strategy.basic_mean_reversion import BasicMeanReversion
from strategy.mean_reversion_with_return_filter import (
    MeanReversionWithReturnFilter,
)


def weekly_frame(current_high=10.8, current_low=10.2):
    timestamps = pd.to_datetime([
        "2024-01-07T22:00:00Z", "2024-01-08T02:00:00Z",
        "2024-01-08T06:00:00Z", "2024-01-08T10:00:00Z",
        "2024-01-14T22:00:00Z", "2024-01-15T02:00:00Z",
        "2024-01-15T06:00:00Z", "2024-01-15T10:00:00Z",
        "2024-01-15T14:00:00Z",
    ])
    return pd.DataFrame({
        "timestamp": timestamps,
        # The completed first week is positive: open 10.0 -> close 11.0.
        "open":  [10.0, 10.2, 10.5, 10.8, 10.6, 10.5, 10.4, 10.3, 10.4],
        "high":  [10.3, 10.6, 10.9, 11.2, 10.8, 10.7, 10.6, 10.5, current_high],
        "low":   [9.8, 10.0, 10.3, 10.6, 10.4, 10.3, 10.2, 10.1, current_low],
        "close": [10.2, 10.5, 10.8, 11.0, 10.5, 10.4, 10.3, 10.4, 10.5],
    })


def strategy(mode="reversion", timeframe="W1"):
    return MeanReversionWithReturnFilter(
        range_lookback=2,
        atr_period=2,
        atr_multiplier=1.0,
        tp_fraction=1.0,
        use_return_filter=True,
        return_filter_timeframe=timeframe,
        return_filter_mode=mode,
    )


def prepared(frame):
    return precompute_for_params(frame, {"atr_period": 2})


def test_weekly_reversion_filter_allows_sell_after_positive_week():
    frame = prepared(weekly_frame(current_high=10.8, current_low=10.2))
    candidate = strategy(mode="reversion")
    candidate.bind_data(frame)

    signal = candidate.on_bar(8)

    assert signal is not None
    assert signal["side"] == "SELL"


def test_weekly_reversion_filter_blocks_buy_after_positive_week():
    # Avoid the SELL trigger while touching the preceding two-bar low.
    frame = prepared(weekly_frame(current_high=10.45, current_low=10.1))
    candidate = strategy(mode="reversion")
    candidate.bind_data(frame)

    assert candidate.on_bar(8) is None


def test_continuation_mode_inverts_the_allowed_side():
    frame = prepared(weekly_frame(current_high=10.45, current_low=10.1))
    candidate = strategy(mode="continuation")
    candidate.bind_data(frame)

    signal = candidate.on_bar(8)

    assert signal is not None
    assert signal["side"] == "BUY"


def test_filter_uses_previous_completed_week_not_current_week_return():
    frame = weekly_frame(current_high=10.8, current_low=10.2)
    frame.loc[4:7, "close"] = [9.5, 9.2, 9.0, 8.8]
    frame = prepared(frame)
    candidate = strategy(mode="reversion")
    candidate.bind_data(frame)

    assert candidate.return_filter_side[4] == "SELL"
    assert candidate.return_filter_side[8] == "SELL"


def test_d1_filter_is_supported_and_case_normalized():
    frame = prepared(weekly_frame())
    candidate = strategy(mode="REVERSION", timeframe="d1")
    candidate.bind_data(frame)

    assert candidate.return_filter_timeframe == "D1"
    assert candidate.return_filter_mode == "reversion"


def test_filter_is_disabled_by_default_and_matches_basic_strategy():
    frame = prepared(weekly_frame(current_high=10.8, current_low=10.2))
    frame_without_timestamp = frame.drop(columns=["timestamp"])
    base = BasicMeanReversion(
        range_lookback=2,
        atr_period=2,
        atr_multiplier=1.0,
        tp_fraction=1.0,
    )
    optional_filter = MeanReversionWithReturnFilter(
        range_lookback=2,
        atr_period=2,
        atr_multiplier=1.0,
        tp_fraction=1.0,
    )
    base.bind_data(frame_without_timestamp)
    optional_filter.bind_data(frame_without_timestamp)

    assert optional_filter.use_return_filter is False
    assert [optional_filter.on_bar(i) for i in range(len(frame))] == [
        base.on_bar(i) for i in range(len(frame))
    ]


@pytest.mark.parametrize("timeframe", ["H4", "M30", "MONTH"])
def test_invalid_filter_timeframe_is_rejected(timeframe):
    with pytest.raises(ValueError, match="D1.*W1"):
        strategy(timeframe=timeframe)


def test_invalid_filter_mode_is_rejected():
    with pytest.raises(ValueError, match="continuation.*reversion"):
        strategy(mode="reverse")
