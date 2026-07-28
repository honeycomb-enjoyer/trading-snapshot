import pandas as pd
import pytest

from strategy.continuation_breakout import ContinuationBreakout


def breakout_frame():
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="h"),
        "open": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
        "high": [10.2, 10.4, 10.5, 10.6, 10.7, 10.9],
        "low": [9.9, 10.0, 10.1, 10.2, 10.3, 10.4],
        "close": [10.1, 10.2, 10.3, 10.4, 10.5, 10.8],
        "atr_2": [0.2, 0.3, 0.4, 0.5, 0.6, 99.0],
    })


def test_buy_uses_range_boundary_and_previous_bar_atr():
    frame = breakout_frame()
    candidate = ContinuationBreakout(
        lookback=2, atr_period=2, sl_atr=2.0, rr=3.0, direction="long"
    )
    candidate.bind_data(frame)

    signal = candidate.on_bar(5)

    assert signal["side"] == "BUY"
    assert signal["entry_trigger"] == "price_at_or_above"
    assert signal["entry"] == pytest.approx(10.7)
    assert signal["sl"] == pytest.approx(9.5)
    assert signal["tp"] == pytest.approx(14.3)


def test_two_sided_breakout_is_skipped_when_intrabar_order_is_unknown():
    frame = breakout_frame()
    frame.loc[5, "low"] = 10.0
    candidate = ContinuationBreakout(lookback=2, atr_period=2)
    candidate.bind_data(frame)

    assert candidate.on_bar(5) is None


def daily_filter_frame(previous_day_close):
    timestamps = pd.to_datetime([
        "2024-01-07T22:00:00Z", "2024-01-08T02:00:00Z",
        "2024-01-08T06:00:00Z", "2024-01-08T10:00:00Z",
        "2024-01-08T14:00:00Z", "2024-01-08T18:00:00Z",
        "2024-01-08T22:00:00Z", "2024-01-09T02:00:00Z",
        "2024-01-09T06:00:00Z",
    ])
    close = [10.1, 10.2, 10.3, 10.4, 10.5, previous_day_close, 10.4, 10.5, 10.8]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.4, 10.4, 10.5],
        "high": [10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.5, 10.6, 10.9],
        "low": [9.9, 10.0, 10.1, 10.2, 10.3, 10.4, 10.3, 10.3, 10.4],
        "close": close,
        "atr_2": [0.2] * 9,
    })


def filtered_strategy():
    return ContinuationBreakout(
        lookback=2,
        atr_period=2,
        direction="long",
        use_return_filter=True,
        return_filter_timeframe="D1",
        return_filter_mode="continuation",
    )


def test_positive_previous_day_allows_long_breakout():
    frame = daily_filter_frame(previous_day_close=10.8)
    candidate = filtered_strategy()
    candidate.bind_data(frame)

    assert candidate.return_filter_side[8] == "BUY"
    assert candidate.on_bar(8)["side"] == "BUY"


def test_negative_previous_day_blocks_long_breakout():
    frame = daily_filter_frame(previous_day_close=9.8)
    candidate = filtered_strategy()
    candidate.bind_data(frame)

    assert candidate.return_filter_side[8] == "SELL"
    assert candidate.on_bar(8) is None


def test_weekly_filter_keeps_previous_completed_week_across_dst_boundary():
    timestamps = pd.to_datetime([
        # New York is UTC-5: the first weekly boundary is Sunday 22:00 UTC.
        "2024-03-03T22:00:00Z", "2024-03-04T02:00:00Z",
        "2024-03-04T06:00:00Z", "2024-03-04T10:00:00Z",
        # New York DST starts on March 10: the next boundary is 21:00 UTC.
        "2024-03-10T21:00:00Z", "2024-03-11T01:00:00Z",
        "2024-03-11T05:00:00Z", "2024-03-11T09:00:00Z",
        "2024-03-11T13:00:00Z",
    ])
    frame = pd.DataFrame({
        "timestamp": timestamps,
        # The completed first week is positive: 10.0 -> 11.0.
        "open": [10.0, 10.2, 10.5, 10.8, 10.6, 10.5, 10.4, 10.3, 10.4],
        "high": [10.3, 10.6, 10.9, 11.2, 10.8, 10.7, 10.6, 10.5, 10.9],
        "low": [9.8, 10.0, 10.3, 10.6, 10.4, 10.3, 10.2, 10.1, 10.4],
        "close": [10.2, 10.5, 10.8, 11.0, 10.5, 10.4, 10.3, 10.4, 10.8],
        "atr_2": [0.2] * 9,
    })
    candidate = ContinuationBreakout(
        lookback=2,
        atr_period=2,
        direction="long",
        use_return_filter=True,
        return_filter_timeframe="W1",
        return_filter_mode="continuation",
    )
    candidate.bind_data(frame)

    assert candidate.return_filter_side[4] == "BUY"
    assert candidate.return_filter_side[8] == "BUY"
    assert candidate.on_bar(8)["side"] == "BUY"
