"""Render compact human reports and machine-readable strategy artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from engine.backtester import plot_equity, plot_monthly_returns


ARTIFACT_DESCRIPTIONS = {
    "summary.md": "Primary human-readable strategy report.",
    "equity_curve.png": "Strategy equity curve.",
    "monthly_returns.png": "Calendar-month returns chart.",
    "data/summary.json": "Complete machine-readable metrics and assumptions.",
    "data/trades.csv": "Enriched closed trades with risk, MAE/MFE, timing, and margin scenarios.",
    "data/equity_curve.csv": "Trade-close equity curve in R.",
    "data/monthly_returns.csv": "Calendar-month returns in R.",
    "data/intraday_equity.csv": "Execution-timeframe mark-to-market equity envelope and simultaneous margin scenarios.",
    "data/daily_equity.csv": "Daily equity diagnostics for every configured reset timezone.",
    "data/monte_carlo_runs.csv": "Final R and max drawdown for every Monte Carlo run.",
    "data/manifest.json": "Artifact inventory and schema version.",
}

LEGACY_ROOT_DATA_FILES = {
    "summary.txt", "summary.json", "trades.csv", "equity_curve.csv",
    "monthly_returns.csv", "intraday_equity.csv", "daily_equity.csv",
    "monte_carlo_runs.csv", "manifest.json",
}


def _json_default(value):
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _number(value, digits=2):
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}"


def _mapping_rows(mapping: Mapping[str, Any]) -> list[str]:
    rows = []
    for key, value in mapping.items():
        if key == "execution_cost_model":
            continue
        rows.append(f"| `{key}` | `{value}` |")
    return rows


def _worst_intraday(summary: Mapping[str, Any], metric: str, statistic: str) -> float:
    values = [
        float(timezone[metric][statistic])
        for timezone in summary["intraday_equity"]["reset_timezones"].values()
    ]
    return max(values) if values else 0.0


def render_summary(summary: Mapping[str, Any]) -> str:
    strategy = summary["strategy"]
    dataset = summary["dataset"]
    full = summary["backtest"]["segments"]["full"]
    mc = summary["monte_carlo"]
    diagnostics = summary["trade_diagnostics"]
    margin = summary["margin"]["effective_leverage_scenarios"]
    execution_costs = summary.get("execution", {}).get("costs", {})
    cost_profile = summary.get("execution", {}).get("cost_profile", {})
    cost_rows = []
    if execution_costs:
        cost_rows = [
            f"| Average execution cost | {_number(execution_costs.get('average_r', 0.0), 3)}R |",
            f"| Median execution cost | {_number(execution_costs.get('median_r', 0.0), 3)}R |",
            f"| P90 execution cost | {_number(execution_costs.get('p90_r', 0.0), 3)}R |",
        ]
        if cost_profile:
            cost_rows.append(
                f"| Execution cost profile | {cost_profile.get('symbol', 'N/A')} / "
                f"{cost_profile.get('profile', 'N/A')} |"
            )
    lines = [
        f"# Strategy Profile: {strategy['strategy_name']}",
        "",
        "## Identity and dataset",
        "",
        f"- Symbol: `{strategy['symbol']}`",
        f"- Asset class: `{strategy['asset_class']}`",
        f"- Strategy class: `{strategy['strategy_class']}`",
        f"- Dataset: `{dataset['mode']}`; {dataset['bars']} bars",
        f"- Signal timeframe: `{dataset.get('signal_timeframe') or dataset.get('timeframe') or 'N/A'}`",
        f"- Execution timeframe: `{dataset.get('timeframe') or 'N/A'}`",
        f"- Lower-timeframe replay: `{'enabled' if summary.get('execution', {}).get('replay_enabled') else 'disabled'}`",
        f"- Replay entry / exit bar offsets: `{summary.get('execution', {}).get('entry_bar_offset', 0)}` / `{summary.get('execution', {}).get('exit_bar_offset', 0)}`",
        f"- Replay skips - incomplete closed history / unavailable entry bar: `{summary.get('execution', {}).get('skipped_history_incomplete', 0)}` / `{summary.get('execution', {}).get('skipped_entry_bar_unavailable', 0)}`",
        f"- Period: `{dataset['start']}` - `{dataset['end']}`",
        f"- Venue: `{dataset.get('venue') or 'N/A'}`; timezone: `{dataset.get('timezone') or 'N/A'}`",
        f"- Dataset SHA-256: `{dataset.get('content_sha256') or 'N/A'}`",
        "",
        "### Data quality warnings",
        "",
        *(
            [f"- {warning}" for warning in dataset.get("warnings", [])]
            or ["- None."]
        ),
        "",
        "## Parameters",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        *_mapping_rows(summary["parameters"]),
        "",
        "## Management",
        "",
        "| Setting | Value |",
        "|---|---:|",
        *_mapping_rows(summary["management"]),
        "",
        "## Full backtest",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Trades | {full['total_trades']} |",
        f"| Wins / losses / BE | {full['wins']} / {full['losses']} / {full['be_trades']} |",
        f"| Win rate | {_number(full['winrate'])}% |",
        f"| Net R | {_number(full['net_r'])}R |",
        f"| Max drawdown | {_number(full['max_drawdown'])}R |",
        f"| Profit factor | {_number(full['profit_factor'], 3)} |",
        f"| Expectancy | {_number(full['expectancy'], 3)}R |",
        f"| Average win / loss | {_number(full['avg_win'])}R / {_number(full['avg_loss'])}R |",
        f"| Best / worst trade | {_number(full['best_trade'])}R / {_number(full['worst_trade'])}R |",
        *cost_rows,
        f"| Max consecutive wins / losses | {summary['backtest']['max_consecutive_wins']} / {summary['backtest']['max_consecutive_losses']} |",
        f"| Calendar time in market | {_number(diagnostics['calendar_time_in_market_pct'])}% |",
        f"| Available {dataset.get('timeframe') or 'dataset'} bars with a position | {_number(diagnostics['available_bars_with_position_pct'])}% |",
        f"| Max simultaneous positions | {diagnostics['max_simultaneous_positions']} |",
        f"| Same-bar exits | {diagnostics['same_bar_exit_trades']} ({_number(diagnostics['same_bar_exit_pct'])}%) |",
        f"| Same-bar SL+TP, stop-first | {diagnostics['same_bar_both_sl_tp_trades']} |",
        "",
        "## Train and holdout",
        "",
        "| Segment | Trades | Net R | Max DD | PF | Expectancy | Win rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("train", "holdout"):
        segment = summary["backtest"]["segments"].get(name)
        if segment:
            lines.append(
                f"| {name} | {segment['total_trades']} | {_number(segment['net_r'])}R | "
                f"{_number(segment['max_drawdown'])}R | {_number(segment['profit_factor'], 3)} | "
                f"{_number(segment['expectancy'], 3)}R | {_number(segment['winrate'])}% |"
            )
    lines.extend([
        "",
        "## Year-by-year stability",
        "",
        "| Year | Trades | Net R | Max DD | PF | Expectancy |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for year in summary["backtest"]["yearly"]:
        lines.append(
            f"| {year['year']} | {year['total_trades']} | {_number(year['net_r'])}R | "
            f"{_number(year['max_drawdown'])}R | {_number(year['profit_factor'], 3)} | "
            f"{_number(year['expectancy'], 3)}R |"
        )
    rolling = summary["backtest"]["rolling_365d"]
    episode = summary["backtest"]["drawdown_episode"]
    lines.extend([
        "",
        f"Rolling 365-day Net R: minimum `{_number(rolling['min_r'])}R`, median "
        f"`{_number(rolling['median_r'])}R`, maximum `{_number(rolling['max_r'])}R`.",
        "",
        f"Longest max-drawdown episode: `{episode.get('trades_peak_to_trough', 'N/A')}` trades to trough and "
        f"`{episode.get('trades_peak_to_recovery', 'N/A')}` trades to recovery.",
        "",
        "## Monte Carlo",
        "",
        f"Mode: `{mc['mode']}`, simulations: `{mc['simulations']}`.",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Mean / median max DD | {_number(mc['mean_dd'])}R / {_number(mc['median_dd'])}R |",
        f"| Best / worst max DD | {_number(mc['best_dd'])}R / {_number(mc['worst_dd'])}R |",
        f"| 95% worst max DD | {_number(mc['p95_dd'])}R |",
        f"| Probability DD > 10R | {_number(mc['dd_gt_10'], 1)}% |",
        f"| Probability DD > 15R | {_number(mc['dd_gt_15'], 1)}% |",
        f"| Probability DD > 20R | {_number(mc['dd_gt_20'], 1)}% |",
        f"| Probability DD > 30R | {_number(mc['dd_gt_30'], 1)}% |",
        "",
        "## Trade excursion and exposure",
        "",
        "| Metric | Mean | Median | P95 | P99 | Maximum |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for label, key in (("MAE", "mae_r"), ("MFE", "mfe_r"), ("Bar coverage, hours", "bar_coverage_hours")):
        values = diagnostics[key]
        suffix = "R" if key != "bar_coverage_hours" else ""
        lines.append(
            f"| {label} | {_number(values['mean'])}{suffix} | {_number(values['median'])}{suffix} | "
            f"{_number(values['p95'])}{suffix} | {_number(values['p99'])}{suffix} | {_number(values['max'])}{suffix} |"
        )
    lines.extend([
        "",
        "## Margin utility",
        "",
        "Values are percent of account equity occupied while a position is open, normalized to `1%` risk per trade.",
        "For the linear price-risk model: `margin % = risk % x entry / (effective leverage x stop distance)`.",
        "",
        "| Effective leverage | Mean | Median | P95 | P99 | Maximum | Trades >100% |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for leverage, values in margin.items():
        lines.append(
            f"| 1:{leverage} | {_number(values['mean'])}% | {_number(values['median'])}% | "
            f"{_number(values['p95'])}% | {_number(values['p99'])}% | {_number(values['max'])}% | "
            f"{values['trades_above_100pct_equity']} |"
        )
    highest_margin = max(margin.items(), key=lambda item: item[1]["max"])
    highest_leverage, highest_values = highest_margin
    max_trade = highest_values["max_margin_trade"]
    lines.extend([
        "",
        "Maximum-margin observation across the configured scenarios:",
        "",
        f"- Effective leverage: `1:{highest_leverage}`",
        f"- Open time: `{max_trade['open_time']}`; side: `{max_trade['side']}`",
        f"- Entry / initial SL / price risk: `{_number(max_trade['entry'])}` / "
        f"`{_number(max_trade['initial_sl'])}` / `{_number(max_trade['initial_risk_price'], 4)}`",
        f"- Normalized margin at 1% risk: `{_number(highest_values['max'])}%`",
        f"- Trade result: `{_number(max_trade['result_r'])}R` (`{max_trade['close_reason']}`)",
    ])
    if float(highest_values["max"]) > float(highest_values["p99"]) * 5.0:
        lines.extend([
            "",
            "The maximum is a severe narrow-stop outlier (more than 5x P99). "
            "Use median/P95/P99 for typical portfolio load and keep the maximum as a "
            "separate pre-trade margin-feasibility event.",
        ])
    lines.extend([
        "",
        "## Intraday equity by reset timezone",
        "",
        f"All values are in R. Peak-to-trough is a conservative {dataset.get('timeframe') or 'dataset'} OHLC envelope.",
        "",
        "| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |",
        "|---|---:|---:|---:|",
    ])
    for timezone_name, values in summary["intraday_equity"]["reset_timezones"].items():
        loss = values["loss_from_day_start_r"]
        peak = values["pessimistic_peak_to_trough_r"]
        equity_range = values["daily_equity_range_r"]
        lines.append(
            f"| {timezone_name} | {_number(loss['p99'])}R / {_number(loss['max'])}R | "
            f"{_number(peak['p99'])}R / {_number(peak['max'])}R | "
            f"{_number(equity_range['p99'])}R / {_number(equity_range['max'])}R |"
        )
    lines.extend([
        "",
        "## Risk scaling reference",
        "",
        "This table scales isolated historical and simulated R metrics. It is not a portfolio recommendation.",
        "",
        "| Risk per trade | Historical DD | MC 95% DD | MC worst DD | Day-start loss P99 / max | Intraday peak-to-trough P99 / max |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    day_p99 = _worst_intraday(summary, "loss_from_day_start_r", "p99")
    day_max = _worst_intraday(summary, "loss_from_day_start_r", "max")
    peak_p99 = _worst_intraday(summary, "pessimistic_peak_to_trough_r", "p99")
    peak_max = _worst_intraday(summary, "pessimistic_peak_to_trough_r", "max")
    for risk_pct in summary["risk_scenarios_pct"]:
        factor = float(risk_pct)
        lines.append(
            f"| {factor:.2f}% | {_number(full['max_drawdown'] * factor)}% | "
            f"{_number(mc['p95_dd'] * factor)}% | {_number(mc['worst_dd'] * factor)}% | "
            f"{_number(day_p99 * factor)}% / {_number(day_max * factor)}% | "
            f"{_number(peak_p99 * factor)}% / {_number(peak_max * factor)}% |"
        )
    lines.extend([
        "",
        "## Close reasons",
        "",
        "| Reason | Trades |",
        "|---|---:|",
        *[f"| `{reason}` | {count} |" for reason, count in summary["backtest"]["close_reasons"].items()],
        "",
        "## Limitations",
        "",
        *[f"- {item}" for item in summary["limitations"]],
        "",
        "## Artifact index",
        "",
        *[f"- `{name}` - {description}" for name, description in ARTIFACT_DESCRIPTIONS.items()],
        "",
    ])
    return "\n".join(lines)


def _flatten_trade_margin(trades: pd.DataFrame) -> pd.DataFrame:
    output = trades.copy()
    if "margin_pct_equity_per_1pct_risk" not in output:
        return output
    margin_rows = output["margin_pct_equity_per_1pct_risk"].apply(pd.Series)
    margin_rows.columns = [
        f"margin_pct_equity_per_1pct_risk_leverage_{column}"
        for column in margin_rows.columns
    ]
    return pd.concat(
        [output.drop(columns=["margin_pct_equity_per_1pct_risk"]), margin_rows],
        axis=1,
    )


def write_strategy_profile(profile: Mapping[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for legacy_name in LEGACY_ROOT_DATA_FILES:
        legacy_path = output_dir / legacy_name
        if legacy_path.is_file():
            legacy_path.unlink()
    summary = profile["summary"]
    report = render_summary(summary)
    (output_dir / "summary.md").write_text(report, encoding="utf-8")
    (data_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    _flatten_trade_margin(profile["trades"]).to_csv(data_dir / "trades.csv", index=False)
    profile["equity_curve"].to_csv(data_dir / "equity_curve.csv", index=False)
    profile["monthly_returns"].to_csv(data_dir / "monthly_returns.csv", index=False)
    profile["intraday_equity"].to_csv(data_dir / "intraday_equity.csv", index=False)
    profile["daily_equity"].to_csv(data_dir / "daily_equity.csv", index=False)
    profile["monte_carlo_runs"].to_csv(data_dir / "monte_carlo_runs.csv", index=False)

    matplotlib_cache = output_dir.parents[1] / ".matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    trades = profile["trades"].to_dict("records")
    plot_equity(trades, profile["raw_equity"], report_dir=output_dir)
    plot_monthly_returns(trades, report_dir=output_dir)

    manifest = {
        "schema_version": summary["schema_version"],
        "strategy_name": summary["strategy"]["strategy_name"],
        "artifacts": ARTIFACT_DESCRIPTIONS,
    }
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return output_dir / "summary.md"
