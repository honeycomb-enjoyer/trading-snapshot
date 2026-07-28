import math
from types import SimpleNamespace

import pandas as pd
import pytest

from strategies.xau_h4_continuation_breakout import config
from strategies.xau_h4_continuation_breakout.strategy import (
    XAUH4ContinuationBreakoutStrategy,
)


def _tick(when, *, bid, ask):
    utc = pd.Timestamp(when).to_pydatetime()
    return SimpleNamespace(timestamp=utc.replace(tzinfo=None), utc_timestamp=utc, bid=bid, ask=ask)


def _ready_strategy(previous_return):
    strategy = XAUH4ContinuationBreakoutStrategy()
    strategy.range_high = 2000.0
    strategy.range_low = 1900.0
    strategy.atr = 20.0
    strategy.last_h4_bar = "2026-01-09T20:00:00+00:00"
    previous_week = strategy._filter_key(pd.Timestamp("2026-01-05T00:00:00Z"))
    strategy.completed_week_returns = {previous_week: previous_return}
    return strategy


def test_positive_previous_week_allows_only_continuation_buy():
    strategy = _ready_strategy(0.02)
    now = "2026-01-12T08:00:00Z"
    buy = strategy.check_entry_signal(_tick(now, bid=1999.9, ask=2000.0))
    assert buy == {
        "side": "BUY",
        "expected_entry": 2000.0,
        "stop_distance": pytest.approx(25.0),
        "tp_distance": pytest.approx(37.5),
    }

    strategy = _ready_strategy(0.02)
    assert strategy.check_entry_signal(_tick(now, bid=1900.0, ask=1900.1)) is None


def test_negative_previous_week_allows_only_continuation_sell():
    strategy = _ready_strategy(-0.02)
    now = "2026-01-12T08:00:00Z"
    sell = strategy.check_entry_signal(_tick(now, bid=1900.0, ask=1900.1))
    assert sell["side"] == "SELL"
    assert sell["expected_entry"] == 1900.0
    assert sell["stop_distance"] == pytest.approx(25.0)
    assert sell["tp_distance"] == pytest.approx(37.5)
    assert strategy.check_entry_signal(_tick(now, bid=1999.9, ask=2000.0)) is None


def test_current_partial_week_return_does_not_replace_previous_week_filter():
    strategy = _ready_strategy(0.02)
    current_week = strategy._filter_key(pd.Timestamp("2026-01-12T08:00:00Z"))
    strategy.completed_week_returns[current_week] = -0.50
    signal = strategy.check_entry_signal(
        _tick("2026-01-12T08:00:00Z", bid=1999.9, ask=2000.0)
    )
    assert signal["side"] == "BUY"


def test_two_sided_breakout_is_skipped_like_backtest_ohlc_ambiguity():
    strategy = _ready_strategy(0.02)
    signal = strategy.check_entry_signal(
        _tick("2026-01-12T08:00:00Z", bid=1900.0, ask=2000.0)
    )
    assert signal is None
    assert strategy.last_long_signal_bar == strategy.last_h4_bar
    assert strategy.last_short_signal_bar == strategy.last_h4_bar
    assert strategy.consume_state_dirty()


def test_completed_weeks_use_sunday_new_york_rollover_and_previous_return():
    first_week = pd.date_range("2026-01-04T22:00:00Z", periods=30, freq="4h")
    second_week = pd.date_range("2026-01-11T22:00:00Z", periods=30, freq="4h")
    timestamps = first_week.append(second_week)
    closes = [2010.0] * len(first_week) + [1980.0] * len(second_week)
    opens = [2000.0] * len(first_week) + [2000.0] * len(second_week)
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": [2020.0] * len(timestamps),
        "low": [1970.0] * len(timestamps),
        "close": closes,
    })
    strategy = XAUH4ContinuationBreakoutStrategy()
    strategy.completed_week_returns = strategy._build_completed_returns(frame)

    values = list(strategy.completed_week_returns.values())
    assert values[0] == pytest.approx(math.log(2010.0 / 2000.0))
    assert values[1] == pytest.approx(math.log(1980.0 / 2000.0))
    assert strategy._allowed_side(pd.Timestamp("2026-01-19T08:00:00Z")) == "SELL"


def test_config_matches_backtest_profile_and_live_identity():
    assert config.SYMBOL == "XAUUSD"
    assert config.MAGIC == 53001
    assert config.ACCOUNT == "hub_demo"
    assert config.SIGNAL_TIMEFRAME == "H4"
    assert config.LOOKBACK == 24
    assert config.ATR_PERIOD == 20
    assert config.STOP_LOSS == 1.25
    assert config.TAKE_PROFIT == 1.5
    assert config.DIRECTION == "both"
    assert config.USE_RETURN_FILTER is True
    assert config.RETURN_FILTER_TIMEFRAME == "W1"
    assert config.RETURN_FILTER_MODE == "continuation"
    assert config.USE_BREAK_EVEN is False
    assert config.DAILY_SL_LIMIT_USD is None
    assert config.WEEKLY_SL_LIMIT_USD == 3 * config.RISK_PER_TRADE_USD
