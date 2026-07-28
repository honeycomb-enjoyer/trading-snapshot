import json
from pathlib import Path

import pandas as pd
import pytest

from portfolio_profile.analyzer import build_portfolio_profile
from portfolio_profile.reporting import write_portfolio_profile


def _write_strategy_profile(root: Path, name: str, label_shift: float) -> None:
    data = root / name / "data"
    data.mkdir(parents=True)
    summary = {
        "strategy": {
            "strategy_name": name,
            "symbol": name[-3:],
            "strategy_class": "TestStrategy",
        },
        "backtest": {
            "segments": {
                "full": {
                    "net_r": 1.0,
                    "max_drawdown": 0.5,
                },
            },
        },
    }
    (data / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    timestamps = pd.to_datetime([
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:01:00Z",
        "2024-01-01T00:02:00Z",
    ])
    pd.DataFrame({
        "timestamp": timestamps,
        "realized_balance_pct": [0.0, 0.0, 0.0],
        "equity_close_r": [0.0, label_shift, label_shift + 1.0],
        "equity_low_r": [0.0, label_shift - 0.2, label_shift + 0.8],
        "equity_high_r": [0.0, label_shift + 0.2, label_shift + 1.2],
        "active_positions": [0, 1, 0],
        "margin_pct_equity_per_1pct_risk_leverage_20": [0.0, 10.0, 0.0],
        "margin_pct_equity_per_1pct_risk_leverage_100": [0.0, 2.0, 0.0],
    }).to_csv(data / "intraday_equity.csv", index=False)
    pd.DataFrame({
        "open_time": [timestamps[1]],
        "close_time": [timestamps[2]],
        "side": ["BUY"],
        "R": [1.0],
        "close_reason": ["tp"],
    }).to_csv(data / "trades.csv", index=False)


def _config():
    return {
        "name": "test_portfolio",
        "components": [
            {"label": "A", "profile": "Strategy_AAA", "risk_pct": 0.5},
            {"label": "B", "profile": "Strategy_BBB", "risk_pct": 0.25},
        ],
        "equity_reset_timezones": ["UTC"],
        "margin_scenarios": {
            "eval": {"default_leverage": 100, "overrides": {}},
        },
        "prop_rules": {
            "name": "Test Rules",
            "reset_timezone": "UTC",
            "phase_targets_pct": [0.5],
            "daily_loss_limit_pct": 4.0,
            "max_loss_limit_pct": 12.0,
            "target_requires_flat": True,
            "rolling_start_weekday": 0,
            "horizons_days": [30],
        },
        "monte_carlo": {"mode": "trade_shuffle", "simulations": 3, "random_seed": 1},
    }


def test_portfolio_profile_combines_weighted_intraday_and_trades(tmp_path):
    _write_strategy_profile(tmp_path, "Strategy_AAA", 0.0)
    _write_strategy_profile(tmp_path, "Strategy_BBB", 1.0)

    profile = build_portfolio_profile(profile_root=tmp_path, config=_config())

    summary = profile["summary"]
    assert summary["performance"]["final_balance_pct"] == pytest.approx(0.75)
    assert profile["intraday_equity"].loc[1, "equity_close_pct"] == pytest.approx(0.25)
    assert profile["intraday_equity"].loc[1, "margin_eval_pct"] == pytest.approx(1.5)
    assert summary["performance"]["events"]["total_trades"] == 2
    yearly = summary["performance"]["yearly_stability"]
    assert yearly == [{
        "year": 2024,
        "trades": 2,
        "net_pct": pytest.approx(0.75),
        "max_drawdown_pct": pytest.approx(0.0),
        "profit_factor": pytest.approx(0.75),
        "expectancy_pct": pytest.approx(0.375),
    }]
    assert summary["challenge"]["30"]["pass_pct"] == pytest.approx(100.0)


def test_portfolio_writer_emits_artifacts(tmp_path, monkeypatch):
    _write_strategy_profile(tmp_path, "Strategy_AAA", 0.0)
    _write_strategy_profile(tmp_path, "Strategy_BBB", 1.0)
    profile = build_portfolio_profile(profile_root=tmp_path, config=_config())
    monkeypatch.setattr("portfolio_profile.reporting._plot_equity", lambda *args, **kwargs: None)
    monkeypatch.setattr("portfolio_profile.reporting._plot_monthly", lambda *args, **kwargs: None)
    monkeypatch.setattr("portfolio_profile.reporting._plot_daily_histogram", lambda *args, **kwargs: None)
    monkeypatch.setattr("portfolio_profile.reporting._plot_mc_drawdown", lambda *args, **kwargs: None)

    summary_path = write_portfolio_profile(profile, tmp_path / "out")

    assert summary_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "Portfolio Profile: test_portfolio" in summary_text
    assert "## Year-by-year stability" in summary_text
    for name in (
        "summary.json", "component_trades.csv", "equity_curve.csv",
        "monthly_returns.csv", "yearly_stability.csv",
        "intraday_equity.csv", "daily_equity.csv",
        "rolling_challenge.csv", "monte_carlo_runs.csv", "manifest.json",
    ):
        assert (tmp_path / "out" / "data" / name).exists(), name
