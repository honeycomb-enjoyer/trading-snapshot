from runners.backtest import render_backtest_summary, write_backtest_summary
from runners.monte_carlo import render_monte_carlo_summary, write_monte_carlo_summary


def test_backtest_summary_is_markdown_and_hides_raw_cost_model(tmp_path):
    metrics = {
        "total_trades": 3,
        "wins": 1,
        "losses": 1,
        "be_trades": 1,
        "winrate": 33.333,
        "winrate_no_be": 50.0,
        "net_r": 1.0,
        "max_drawdown": 1.0,
        "profit_factor": 2.0,
        "expectancy": 0.333,
        "avg_win": 2.0,
        "avg_loss": -1.0,
        "best_trade": 2.0,
        "worst_trade": -1.0,
        "execution": {"mode": "open_bar", "fill_timing": "same_bar_trigger"},
        "execution_cost_profile": {
            "symbol": "XAUUSD",
            "profile": "retail_gold_cfd_baseline",
        },
        "execution_costs": {
            "average_r": 0.10,
            "median_r": 0.08,
            "p90_r": 0.20,
        },
    }

    path = write_backtest_summary(
        tmp_path,
        strategy_name="ContinuationBreakout",
        dataset_mode="train",
        bars=100,
        start="2026-01-01 00:00:00+00:00",
        end="2026-01-02 00:00:00+00:00",
        replay_bars=1200,
        metrics=metrics,
    )

    text = path.read_text(encoding="utf-8")
    assert "# Backtest Summary" in text
    assert "| Net R | 1.00R |" in text
    assert "XAUUSD / retail_gold_cfd_baseline" in text
    assert "execution_cost_model" not in text


def test_monte_carlo_summary_is_markdown_and_written(tmp_path):
    config = {"mode": "shuffle", "dataset": "full", "simulations": 100, "random_seed": 7}
    baseline = {"total_trades": 40, "net_r": 12.5}
    report = {
        "simulations": 100,
        "mean_final_r": 12.5,
        "median_final_r": 12.5,
        "best_final_r": 12.5,
        "worst_final_r": 12.5,
        "p5_final_r": 12.5,
        "p95_final_r": 12.5,
        "mean_dd": 4.0,
        "median_dd": 3.8,
        "best_dd": 2.0,
        "worst_dd": 8.0,
        "p95_dd": 6.5,
        "dd_gt_10": 0.0,
        "dd_gt_15": 0.0,
        "dd_gt_20": 0.0,
        "dd_gt_30": 0.0,
        "prob_negative": 0.0,
        "prob_ruin": 0.0,
        "profitable_runs_pct": 100.0,
    }

    path = write_monte_carlo_summary(
        tmp_path,
        config=config,
        baseline_metrics=baseline,
        report=report,
    )

    text = path.read_text(encoding="utf-8")
    assert text == render_monte_carlo_summary(
        config=config,
        baseline_metrics=baseline,
        report=report,
    )
    assert "# Monte Carlo Summary" in text
    assert "| Mean max DD | 4.00R |" in text
    assert "`mc_drawdown_histogram.png`" in text
