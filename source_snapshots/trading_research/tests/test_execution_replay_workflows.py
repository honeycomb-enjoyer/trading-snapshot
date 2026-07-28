import pandas as pd
from unittest.mock import patch

from optimizer.worker import evaluate_params, init_worker
from overfit_tests.walkforward_test import window_runner


class ReplayStrategy:
    def __init__(self, **_params):
        pass

    def bind_data(self, df):
        self.df = df

    def on_bar(self, i, df):
        if i == 0:
            return {"side": "BUY", "entry": 10.0, "sl": 9.0, "tp": None}
        return None

    def should_exit(self, i, position, df):
        return i == position["open_bar"]


def _signal_frame(periods=4):
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=periods, freq="h")
    result = pd.DataFrame({
        "timestamp": timestamps,
        "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0,
    })
    result.attrs["dataset_reference"] = {
        "manifest": {"symbol": "TEST", "timeframe": "H1"},
    }
    return result


def _replay_frame(periods=48):
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=periods, freq="5min")
    result = pd.DataFrame({
        "timestamp": timestamps,
        "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0,
    })
    result.attrs["dataset_reference"] = {
        "manifest": {"symbol": "TEST", "timeframe": "M5"},
    }
    return result


def _cost_model():
    return {
        "enabled": True,
        "profiles": {
            "TEST": {
                "price_unit": 0.01,
                "spread_units": 1.0,
                "slippage_units_per_side": 0.0,
                "commission_units_round_turn": 1.0,
                "swap_long_units_per_roll": 0.0,
                "swap_short_units_per_roll": 0.0,
            },
        },
    }


def _execution_params():
    return {
        "use_break_even": False,
        "break_even_trigger": 1.0,
        "break_even_offset": 0.0,
        "daily_sl_limit": None,
        "weekly_sl_limit": None,
        "execution_cost_model": _cost_model(),
        "max_simultaneous_positions": 1,
        "execution_mode": "next_bar",
    }


def test_optimizer_worker_uses_configured_execution_replay():
    init_worker(
        _signal_frame(), ReplayStrategy,
        {"min_trades": None, "min_profit_factor": None, "min_net_r": None, "max_drawdown": None},
        _replay_frame(), {"replay_entry_bar_offset": 0, "replay_exit_bar_offset": 0},
    )
    result = evaluate_params({"strategy_params": {}, "execution_params": _execution_params()})

    assert result["execution"]["replay_enabled"] is True
    assert result["total_trades"] == 1
    assert result["execution_costs"]["average_r"] > 0
    assert result["execution_cost_profile"]["symbol"] == "TEST"


@patch("overfit_tests.walkforward_test.window_runner.run_backtest")
@patch("overfit_tests.walkforward_test.window_runner.run_optimizer")
def test_walkforward_passes_window_scoped_replay_to_train_and_oos(run_optimizer, run_backtest):
    execution_params = _execution_params()
    run_optimizer.return_value = [{
        "strategy_params": {}, "execution_params": execution_params,
        "profit_factor": 1.1,
    }]
    trades = [
        {"close_time": pd.Timestamp("2024-01-01T02:55:00Z")},
        {"close_time": pd.Timestamp("2024-01-01T03:55:00Z")},
    ]
    run_backtest.return_value = (trades, [0.0, 0.1, 0.2], {
        "profit_factor": 1.2, "net_r": 0.2, "max_drawdown": 0.1,
    })
    window = {
        "train_start_idx": 0, "train_end_idx": 2,
        "test_start_idx": 2, "test_end_idx": 4,
    }

    result = window_runner.run_single_window(
        df=_signal_frame(),
        window=window,
        strategy_class=ReplayStrategy,
        param_grid={},
        execution_grid={},
        precompute_fn=lambda frame, _grid: frame,
        execution_replay_df=_replay_frame(),
        replay_kwargs={"replay_entry_bar_offset": 0, "replay_exit_bar_offset": 0},
    )

    assert result is not None
    train_replay = run_optimizer.call_args.kwargs["execution_replay_df"]
    oos_replay = run_backtest.call_args.kwargs["execution_replay_df"]
    assert run_backtest.call_args.kwargs["execution_cost_model"] == _cost_model()
    assert train_replay["timestamp"].min() == pd.Timestamp("2024-01-01T00:00:00Z")
    assert train_replay["timestamp"].max() < pd.Timestamp("2024-01-01T02:00:00Z")
    assert oos_replay["timestamp"].min() == pd.Timestamp("2024-01-01T02:00:00Z")
    assert oos_replay["timestamp"].max() < pd.Timestamp("2024-01-01T04:00:00Z")
