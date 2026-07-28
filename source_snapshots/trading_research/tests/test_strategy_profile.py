from pathlib import Path

import pandas as pd
import pytest

from strategy_profile.analyzer import build_intraday_equity, build_strategy_profile
from strategy_profile.reporting import write_strategy_profile
from runners.strategy_profile import _dataset_symbol, _infer_asset_class


def _frame():
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-02T00:00:00Z", "2024-01-02T00:30:00Z"]),
        "open": [10.0, 10.4],
        "high": [10.8, 10.7],
        "low": [9.5, 10.2],
        "close": [10.4, 10.5],
    })


def _trade():
    return {
        "side": "BUY",
        "open_time": pd.Timestamp("2024-01-02T00:00:00Z"),
        "signal_time": pd.Timestamp("2024-01-02T00:00:00Z"),
        "close_time": pd.Timestamp("2024-01-02T00:30:00Z"),
        "entry": 10.0,
        "exit": 10.5,
        "R": 0.45,
        "close_reason": "strategy_exit",
        "costs_r": 0.05,
        "signal_entry": 10.0,
        "initial_sl": 9.0,
        "initial_tp": None,
        "initial_risk_price": 1.0,
        "open_bar": 0,
        "close_bar": 1,
        "duration_bars": 2,
        "fill_timing": "same_bar_trigger",
    }


def test_intraday_profile_recognizes_cost_at_entry_and_normalizes_margin():
    intraday, trades = build_intraday_equity(_frame(), [_trade()], margin_leverages=[100])

    assert intraday.loc[0, "equity_close_r"] == pytest.approx(0.35)
    assert intraday.loc[0, "equity_low_r"] == pytest.approx(-0.55)
    assert intraday.loc[1, "equity_close_r"] == pytest.approx(0.45)
    assert intraday.loc[0, "margin_pct_equity_per_1pct_risk_leverage_100"] == pytest.approx(0.1)
    assert trades.loc[0, "mae_r"] == pytest.approx(0.55)
    assert trades.loc[0, "mfe_r"] == pytest.approx(0.75)
    assert trades.loc[0, "bar_coverage_hours"] == pytest.approx(1.0)


def test_profile_writer_emits_human_and_machine_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("strategy_profile.reporting.plot_equity", lambda *args, **kwargs: None)
    monkeypatch.setattr("strategy_profile.reporting.plot_monthly_returns", lambda *args, **kwargs: None)
    metrics = {
        "total_trades": 1,
        "wins": 1,
        "losses": 0,
        "be_trades": 0,
        "winrate": 100.0,
        "winrate_no_be": 100.0,
        "net_r": 0.45,
        "max_drawdown": 0.0,
        "profit_factor": 0.45,
        "expectancy": 0.45,
        "avg_win": 0.45,
        "avg_loss": 0.0,
        "best_trade": 0.45,
        "worst_trade": 0.45,
        "dataset_reference": {"manifest": {"data_kind": "in_memory", "sha256": "abc"}},
        "execution_costs": {
            "average_r": 0.05,
            "median_r": 0.05,
            "p90_r": 0.05,
        },
        "execution_cost_profile": {
            "symbol": "TEST",
            "profile": "test_cost_profile",
        },
    }
    profile = build_strategy_profile(
        df=_frame(),
        trades=[_trade()],
        equity=[0.0, 0.45],
        metrics=metrics,
        strategy_params={"test": 1},
        backtest_config={},
        split_config={
            "mode": "manual",
            "dates": {
                "train_start": "2024-01-01T00:00:00Z",
                "train_end": "2024-02-01T00:00:00Z",
                "holdout_start": "2024-02-01T00:00:00Z",
                "holdout_end": "2024-03-01T00:00:00Z",
            },
        },
        monte_carlo_config={"mode": "shuffle", "simulations": 3, "random_seed": 7},
        profile_config={
            "strategy_name": "test_strategy",
            "symbol": "TEST",
            "asset_class": "fx",
            "strategy_class": "TestStrategy",
            "dataset": "full",
            "margin_model": "linear_price_risk",
            "margin_leverages": [100],
            "equity_reset_timezones": ["UTC"],
            "risk_scenarios_pct": [0.5],
        },
    )

    (tmp_path / "summary.txt").write_text("legacy", encoding="utf-8")
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    summary_path = write_strategy_profile(profile, Path(tmp_path))

    assert summary_path.exists()
    rendered = summary_path.read_text(encoding="utf-8")
    assert "Strategy Profile: test_strategy" in rendered
    assert "Average execution cost" in rendered
    assert "test_cost_profile" in rendered
    assert not (tmp_path / "summary.txt").exists()
    for name in (
        "summary.json", "trades.csv", "equity_curve.csv",
        "monthly_returns.csv", "intraday_equity.csv", "daily_equity.csv",
        "monte_carlo_runs.csv", "manifest.json",
    ):
        assert (tmp_path / "data" / name).exists(), name


def test_profile_identity_is_derived_from_strategy_dataset(monkeypatch):
    monkeypatch.setattr(
        "runners.strategy_profile.DATA_CONFIG",
        {"path": "data/raw/mt5/XAUUSD_M30_20210101_20260715_UTC.csv"},
    )
    metrics = {"dataset_reference": {"manifest": {"symbol": "XAUUSD"}}}

    assert _dataset_symbol(metrics) == "XAUUSD"
    assert _infer_asset_class("XAUUSD") == "metal"
    assert _infer_asset_class("AUDCAD") == "fx"
    assert _infer_asset_class("DE40") == "index"
