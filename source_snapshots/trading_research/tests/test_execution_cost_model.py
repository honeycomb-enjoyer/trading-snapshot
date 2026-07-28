import pandas as pd
import pytest

from engine.backtester import run_backtest


class OneTradeStrategy:
    def bind_data(self, _df):
        pass

    def on_bar(self, index, _df=None):
        if index == 0:
            return {"side": "BUY", "entry": 10.0, "sl": 8.0, "tp": 12.0}
        return None


def cost_config(symbol="TEST"):
    return {
        "enabled": True,
        "rollover_timezone": "America/New_York",
        "rollover_time": "17:00",
        "triple_swap_weekday": 2,
        "profiles": {
            symbol: {
                "name": "test_profile",
                "unit_name": "point",
                "price_unit": 0.1,
                "spread_units": 2.0,
                "slippage_units_per_side": 0.5,
                "commission_units_round_turn": 1.0,
                "swap_long_units_per_roll": 0.5,
                "swap_short_units_per_roll": 0.25,
            },
        },
    }


def frame():
    result = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2024-01-08T16:00:00Z",
            "2024-01-08T18:00:00Z",
            "2024-01-08T22:00:00Z",
            "2024-01-09T22:00:00Z",
            "2024-01-10T22:00:00Z",
            "2024-01-11T18:00:00Z",
        ]),
        "open": [10.0] * 6,
        "high": [10.0, 10.5, 10.5, 10.5, 10.5, 12.0],
        "low": [10.0, 9.5, 9.5, 9.5, 9.5, 9.5],
        "close": [10.0, 10.0, 10.0, 10.0, 10.0, 12.0],
    })
    result.attrs["dataset_reference"] = {
        "manifest": {"symbol": "TEST", "timeframe": "H1"},
    }
    return result


def test_cost_model_scales_price_costs_by_stop_and_counts_rollover():
    trades, _, metrics = run_backtest(
        frame(), OneTradeStrategy(), collect_equity=True,
        execution_cost_model=cost_config(),
    )

    trade = trades[0]
    assert trade["initial_risk_price"] == pytest.approx(2.15)
    assert trade["swap_rollover_units"] == 5
    assert trade["spread_cost_r"] == pytest.approx(0.2 / 2.15)
    assert trade["slippage_cost_r"] == pytest.approx(0.1 / 2.15)
    assert trade["commission_cost_r"] == pytest.approx(0.1 / 2.15)
    assert trade["swap_cost_r"] == pytest.approx(0.25 / 2.15)
    assert trade["costs_r"] == pytest.approx(0.35 / 2.15)
    assert trade["total_costs_r"] == pytest.approx(0.65 / 2.15)
    assert metrics["execution_costs"]["average_r"] == pytest.approx(0.65 / 2.15)
    assert metrics["execution_cost_profile"]["average_initial_risk_units"] == pytest.approx(21.5)


def test_enabled_cost_model_requires_matching_manifest_symbol():
    with pytest.raises(ValueError, match="No execution cost profile.*TEST"):
        run_backtest(
            frame(), OneTradeStrategy(), stats_only=True,
            execution_cost_model=cost_config("OTHER"),
        )
