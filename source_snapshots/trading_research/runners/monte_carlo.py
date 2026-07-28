from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from engine.backtester import run_backtest
from master_config import BACKTEST_CONFIG, MONTE_CARLO_CONFIG, STRATEGY_CLASS, STRATEGY_PARAMS
from overfit_tests.monte_carlo_test.metrics import (
    analyze_mc_results,
    compute_max_drawdown,
    plot_mc_drawdown_histogram,
    plot_mc_equity_curves,
    plot_mc_histogram,
    print_mc_report,
)
from overfit_tests.monte_carlo_test.simulator import run_shuffle_mc, run_synthetic_mc
from runners.common import precompute, prepare_data, report_dir, select_dataset


def main():
    config = MONTE_CARLO_CONFIG
    df = precompute(select_dataset(prepare_data(), config["dataset"]), STRATEGY_PARAMS)
    trades, _, metrics = run_backtest(
        df=df, strategy=STRATEGY_CLASS(**STRATEGY_PARAMS), collect_equity=True, **BACKTEST_CONFIG
    )
    if not trades:
        raise RuntimeError("No trades produced")

    if config["mode"] == "shuffle":
        curves = run_shuffle_mc(
            trades=trades,
            simulations=config["simulations"],
            seed=config["random_seed"],
        )
    elif config["mode"] == "synthetic":
        total = metrics["total_trades"]
        curves = run_synthetic_mc(
            winrate=metrics["wins"] / total,
            be_rate=metrics["be_trades"] / total,
            avg_win=metrics["avg_win"],
            avg_loss=metrics["avg_loss"],
            trades_per_run=total,
            simulations=config["simulations"],
            seed=config["random_seed"],
        )
    else:
        raise ValueError(f"Unknown Monte Carlo mode: {config['mode']}")

    report = analyze_mc_results(curves)
    print(f"\nMonte Carlo mode: {config['mode']}")
    print_mc_report(report)
    output = report_dir("monte_carlo")
    plot_mc_equity_curves(curves, report_dir=output)
    plot_mc_drawdown_histogram([compute_max_drawdown(curve) for curve in curves], report_dir=output)
    if config["mode"] == "synthetic":
        plot_mc_histogram(curves, report_dir=output)
    summary_path = write_monte_carlo_summary(
        output,
        config=config,
        baseline_metrics=metrics,
        report=report,
        synthetic_histogram=config["mode"] == "synthetic",
    )
    print(f"Summary: {summary_path}")
    return report


def write_monte_carlo_summary(
    output_dir: Path,
    *,
    config: Mapping[str, Any],
    baseline_metrics: Mapping[str, Any],
    report: Mapping[str, Any],
    synthetic_histogram: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.md"
    summary_path.write_text(
        render_monte_carlo_summary(
            config=config,
            baseline_metrics=baseline_metrics,
            report=report,
            synthetic_histogram=synthetic_histogram,
        ),
        encoding="utf-8",
    )
    return summary_path


def render_monte_carlo_summary(
    *,
    config: Mapping[str, Any],
    baseline_metrics: Mapping[str, Any],
    report: Mapping[str, Any],
    synthetic_histogram: bool = False,
) -> str:
    artifacts = [
        "- `mc_equity_distribution.png` - sampled equity paths and percentile envelope.",
        "- `mc_drawdown_histogram.png` - max-drawdown distribution across runs.",
    ]
    if synthetic_histogram:
        artifacts.append("- `mc_histogram.png` - synthetic final-equity distribution.")

    lines = [
        "# Monte Carlo Summary",
        "",
        "## Run Context",
        "",
        f"- Strategy: `{STRATEGY_CLASS.__name__}`",
        f"- Mode: `{config.get('mode', 'N/A')}`",
        f"- Dataset split: `{config.get('dataset', 'N/A')}`",
        f"- Simulations: `{report.get('simulations', config.get('simulations', 'N/A'))}`",
        f"- Random seed: `{config.get('random_seed', 'N/A')}`",
        f"- Source trades: `{baseline_metrics.get('total_trades', 'N/A')}`",
        f"- Baseline net R: `{_r(baseline_metrics.get('net_r'))}`",
        "",
        "## Final Equity Distribution",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Mean final R | {_r(report.get('mean_final_r'))} |",
        f"| Median final R | {_r(report.get('median_final_r'))} |",
        f"| Best final R | {_r(report.get('best_final_r'))} |",
        f"| Worst final R | {_r(report.get('worst_final_r'))} |",
        f"| 5% worst final | {_r(report.get('p5_final_r'))} |",
        f"| 95% best final | {_r(report.get('p95_final_r'))} |",
        "",
        "## Drawdown Distribution",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Mean max DD | {_r(report.get('mean_dd'))} |",
        f"| Median max DD | {_r(report.get('median_dd'))} |",
        f"| Best max DD | {_r(report.get('best_dd'))} |",
        f"| Worst max DD | {_r(report.get('worst_dd'))} |",
        f"| 95% worst DD | {_r(report.get('p95_dd'))} |",
        f"| Probability DD > 10R | {_percent(report.get('dd_gt_10'))} |",
        f"| Probability DD > 15R | {_percent(report.get('dd_gt_15'))} |",
        f"| Probability DD > 20R | {_percent(report.get('dd_gt_20'))} |",
        f"| Probability DD > 30R | {_percent(report.get('dd_gt_30'))} |",
        "",
        "## Tail Risk",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Probability negative final R | {_percent(report.get('prob_negative'))} |",
        f"| Probability ruin below -50R | {_percent(report.get('prob_ruin'))} |",
        f"| Profitable runs | {_percent(report.get('profitable_runs_pct'))} |",
        "",
        "## Output Artifacts",
        "",
        *artifacts,
        "",
        "Shuffle mode keeps the historical trade distribution fixed and tests path "
        "dependency. Synthetic mode samples from aggregate win/loss statistics, "
        "so it is a rougher stress test.",
    ]
    return "\n".join(lines) + "\n"


def _r(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{decimals}f}R"


def _percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.1f}%"


if __name__ == "__main__":
    main()
