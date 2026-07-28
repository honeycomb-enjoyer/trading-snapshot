import math
from types import SimpleNamespace

import pandas as pd
import pytest

from strategies.eurgbp_h4_reversion_return_filter import config
from strategies.eurgbp_h4_reversion_return_filter.strategy import (
    EURGBPH4ReversionReturnFilterStrategy,
)


def _tick(when, *, bid, ask):
    utc = pd.Timestamp(when).to_pydatetime()
    return SimpleNamespace(timestamp=utc.replace(tzinfo=None), utc_timestamp=utc, bid=bid, ask=ask)


def _ready_strategy(previous_return):
    strategy = EURGBPH4ReversionReturnFilterStrategy()
    strategy.range_high = 1.20
    strategy.range_low = 1.10
    strategy.mean_price = 1.15
    strategy.atr = 0.01
    strategy.last_h4_bar = "2026-01-09T18:00:00+00:00"
    previous_week = strategy._filter_key(pd.Timestamp("2026-01-05T00:00:00Z"))
    strategy.completed_week_returns = {previous_week: previous_return}
    return strategy


def test_positive_previous_week_allows_only_reversion_sell():
    strategy = _ready_strategy(0.02)
    now = "2026-01-12T08:00:00Z"
    sell = strategy.check_entry_signal(_tick(now, bid=1.20, ask=1.2001))
    assert sell == {
        "side": "SELL",
        "expected_entry": 1.20,
        "stop_distance": pytest.approx(0.02),
        "tp_distance": pytest.approx(0.0125),
    }

    strategy = _ready_strategy(0.02)
    assert strategy.check_entry_signal(_tick(now, bid=1.0999, ask=1.10)) is None


def test_negative_previous_week_allows_only_reversion_buy():
    strategy = _ready_strategy(-0.02)
    now = "2026-01-12T08:00:00Z"
    buy = strategy.check_entry_signal(_tick(now, bid=1.0999, ask=1.10))
    assert buy["side"] == "BUY"
    assert buy["stop_distance"] == pytest.approx(0.02)
    assert buy["tp_distance"] == pytest.approx(0.0125)
    assert strategy.check_entry_signal(_tick(now, bid=1.20, ask=1.2001)) is None


def test_current_partial_week_return_does_not_replace_previous_week_filter():
    strategy = _ready_strategy(0.02)
    current_week = strategy._filter_key(pd.Timestamp("2026-01-12T08:00:00Z"))
    strategy.completed_week_returns[current_week] = -0.50
    signal = strategy.check_entry_signal(
        _tick("2026-01-12T08:00:00Z", bid=1.20, ask=1.2001)
    )
    assert signal["side"] == "SELL"


def test_partial_first_week_is_never_a_later_filter():
    timestamps = pd.date_range("2026-01-07T00:00:00Z", periods=30, freq="4h")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [1.10] * len(timestamps),
        "high": [1.11] * len(timestamps),
        "low": [1.09] * len(timestamps),
        "close": [1.12] * len(timestamps),
    })
    strategy = EURGBPH4ReversionReturnFilterStrategy()
    returns = strategy._build_completed_returns(frame)
    assert pd.isna(next(iter(returns.values())))


def test_completed_fx_weeks_use_sunday_new_york_rollover_and_previous_return():
    first_week = pd.date_range("2026-01-04T22:00:00Z", periods=30, freq="4h")
    second_week = pd.date_range("2026-01-11T22:00:00Z", periods=30, freq="4h")
    timestamps = first_week.append(second_week)
    closes = [1.02] * len(first_week) + [1.08] * len(second_week)
    opens = [1.00] * len(first_week) + [1.10] * len(second_week)
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": [1.12] * len(timestamps),
        "low": [0.98] * len(timestamps),
        "close": closes,
    })
    strategy = EURGBPH4ReversionReturnFilterStrategy()
    strategy.completed_week_returns = strategy._build_completed_returns(frame)

    values = list(strategy.completed_week_returns.values())
    assert values[0] == pytest.approx(math.log(1.02 / 1.00))
    assert values[1] == pytest.approx(math.log(1.08 / 1.10))
    assert strategy._allowed_side(pd.Timestamp("2026-01-19T08:00:00Z")) == "BUY"


def test_config_matches_backtest_profile_and_live_identity():
    assert config.SYMBOL == "EURGBP"
    assert config.MAGIC == 52001
    assert config.ACCOUNT == "hub_demo"
    assert config.SIGNAL_TIMEFRAME == "H4"
    assert config.RANGE_LOOKBACK == 12
    assert config.ATR_PERIOD == 20
    assert config.DIRECTION == "both"
    assert config.STOP_LOSS == 2.0
    assert config.TAKE_PROFIT == 0.25
    assert config.USE_RETURN_FILTER is True
    assert config.RETURN_FILTER_TIMEFRAME == "W1"
    assert config.RETURN_FILTER_MODE == "reversion"
    assert config.USE_BREAK_EVEN is False
    assert config.DAILY_SL_LIMIT_USD is None
    assert config.WEEKLY_SL_LIMIT_USD == 4 * config.RISK_PER_TRADE_USD
