from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from master_config import (
    BACKTEST_CONFIG,
    BACKTEST_DATASET,
    STRATEGY_CLASS,
    STRATEGY_PARAMS,
)
from engine.backtester import run_backtest
from runners.common import (
    precompute,
    prepare_data,
    prepare_execution_replay_data,
    report_dir,
    select_dataset,
)


def main():
    manager = prepare_data()
    df = precompute(select_dataset(manager, BACKTEST_DATASET), STRATEGY_PARAMS)
    replay_df, replay_kwargs = prepare_execution_replay_data(manager, signal_df=df)
    output = report_dir("backtest")

    print("\n========== BACKTEST DATASET ==========")
    print(f"Mode: {BACKTEST_DATASET}")
    print(f"Bars: {len(df)}")
    print(f"Start: {df['timestamp'].iloc[0]}")
    print(f"End:   {df['timestamp'].iloc[-1]}")
    if replay_df is not None:
        print(f"Execution replay: {len(replay_df)} lower-timeframe bars")
    print("======================================")

    print("\n========== PARAMETERS ==========")
    for key, value in STRATEGY_PARAMS.items():
        print(f"{key}: {value}")
    print("================================")

    print("\n========== MANAGEMENT ==========")
    for key, value in BACKTEST_CONFIG.items():
        if key == "execution_cost_model":
            profiles = ", ".join(value.get("profiles", {}))
            value = f"enabled={value.get('enabled', False)}, profiles=[{profiles}]"
        print(f"{key}: {value}")
    print("================================")

    trades, equity, metrics = run_backtest(
        df=df,
        strategy=STRATEGY_CLASS(**STRATEGY_PARAMS),
        stats_only=False,
        plot=True,
        report_dir=output,
        execution_replay_df=replay_df,
        **replay_kwargs,
        **BACKTEST_CONFIG,
    )
    summary_path = write_backtest_summary(
        output,
        strategy_name=STRATEGY_CLASS.__name__,
        dataset_mode=BACKTEST_DATASET,
        bars=len(df),
        start=df["timestamp"].iloc[0] if len(df) else None,
        end=df["timestamp"].iloc[-1] if len(df) else None,
        replay_bars=len(replay_df) if replay_df is not None else None,
        metrics=metrics,
    )
    print(f"Summary: {summary_path}")
    return trades, equity, metrics


def write_backtest_summary(
    output_dir: Path,
    *,
    strategy_name: str,
    dataset_mode: str,
    bars: int,
    start: Any,
    end: Any,
    replay_bars: int | None,
    metrics: Mapping[str, Any],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.md"
    summary_path.write_text(
        render_backtest_summary(
            strategy_name=strategy_name,
            dataset_mode=dataset_mode,
            bars=bars,
            start=start,
            end=end,
            replay_bars=replay_bars,
            metrics=metrics,
        ),
        encoding="utf-8",
    )
    return summary_path


def render_backtest_summary(
    *,
    strategy_name: str,
    dataset_mode: str,
    bars: int,
    start: Any,
    end: Any,
    replay_bars: int | None,
    metrics: Mapping[str, Any],
) -> str:
    execution = metrics.get("execution", {})
    cost_profile = metrics.get("execution_cost_profile", {})
    cost_label = "disabled"
    if cost_profile:
        cost_label = (
            f"{cost_profile.get('symbol', 'N/A')} / "
            f"{cost_profile.get('profile', 'N/A')}"
        )

    rows = [
        ("Trades", metrics.get("total_trades")),
        ("Wins / losses / BE", f"{metrics.get('wins')} / {metrics.get('losses')} / {metrics.get('be_trades')}"),
        ("Win rate", _percent(metrics.get("winrate"))),
        ("Win rate without BE", _percent(metrics.get("winrate_no_be"))),
        ("Net R", _r(metrics.get("net_r"))),
        ("Max drawdown", _r(metrics.get("max_drawdown"))),
        ("Profit factor", _number(metrics.get("profit_factor"), 3)),
        ("Expectancy", _r(metrics.get("expectancy"), 3)),
        ("Average win / loss", f"{_r(metrics.get('avg_win'))} / {_r(metrics.get('avg_loss'))}"),
        ("Best / worst trade", f"{_r(metrics.get('best_trade'))} / {_r(metrics.get('worst_trade'))}"),
    ]

    costs = metrics.get("execution_costs") or {}
    if costs:
        rows.extend([
            ("Average execution cost", _r(costs.get("average_r"), 3)),
            ("Median execution cost", _r(costs.get("median_r"), 3)),
            ("P90 execution cost", _r(costs.get("p90_r"), 3)),
        ])

    replay_label = (
        f"enabled, {replay_bars:,} lower-timeframe bars"
        if replay_bars is not None
        else "disabled"
    )
    lines = [
        "# Backtest Summary",
        "",
        "## Run Context",
        "",
        f"- Strategy: `{strategy_name}`",
        f"- Dataset split: `{dataset_mode}`",
        f"- Period: `{_text(start)}` - `{_text(end)}`",
        f"- Bars: `{bars:,}`",
        f"- Execution mode: `{execution.get('mode', 'N/A')}`",
        f"- Execution replay: `{replay_label}`",
        f"- Fill timing: `{execution.get('fill_timing', 'N/A')}`",
        f"- Cost profile: `{cost_label}`",
        "",
        "## Results",
        "",
        "| Metric | Result |",
        "|---|---:|",
        *[f"| {name} | {_text(value)} |" for name, value in rows],
        "",
        "## Output Artifacts",
        "",
        "- `equity_curve.png` - cumulative backtest equity in R.",
        "- `monthly_returns.png` - calendar-month return profile.",
        "",
        "This summary mirrors the terminal output and keeps the heavy configuration "
        "objects out of the human-readable report.",
    ]
    return "\n".join(lines) + "\n"


def _number(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{decimals}f}"


def _percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f}%"


def _r(value: Any, decimals: int = 2) -> str:
    return "N/A" if value is None else f"{float(value):.{decimals}f}R"


def _text(value: Any) -> str:
    return "N/A" if value is None else str(value)


if __name__ == "__main__":
    main()
