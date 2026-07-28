"""Write portfolio profile reports and plots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


ARTIFACT_DESCRIPTIONS = {
    "summary.md": "Primary human-readable portfolio report.",
    "equity_curve.png": "Portfolio realized balance and equity envelope.",
    "monthly_returns.png": "Calendar-month portfolio returns.",
    "daily_returns_histogram.png": "Histogram of daily realized balance changes.",
    "mc_drawdown_histogram.png": "Trade-shuffle Monte Carlo max-drawdown histogram.",
    "data/summary.json": "Complete machine-readable portfolio metrics and assumptions.",
    "data/component_trades.csv": "Risk-weighted component trade contributions.",
    "data/equity_curve.csv": "Portfolio close-event equity curve.",
    "data/monthly_returns.csv": "Calendar-month portfolio returns.",
    "data/yearly_stability.csv": "Calendar-year portfolio performance stability metrics.",
    "data/intraday_equity.csv": "Combined intraday mark-to-market equity and margin.",
    "data/daily_equity.csv": "Daily equity diagnostics for configured reset timezones.",
    "data/rolling_challenge.csv": "Per-start evaluation-rule simulation outcomes.",
    "data/monte_carlo_runs.csv": "Trade-shuffle Monte Carlo final return and max drawdown.",
    "data/manifest.json": "Artifact inventory and schema version.",
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


def render_summary(summary: Mapping[str, Any]) -> str:
    portfolio = summary["portfolio"]
    dataset = summary["dataset"]
    performance = summary["performance"]
    events = performance["events"]
    prop_rules = summary["prop_rules"]
    lines = [
        f"# Portfolio Profile: {portfolio['name']}",
        "",
        "## Identity and Dataset",
        "",
        f"- Source: `reports/strategy_profile/*` machine artifacts",
        f"- Period: `{dataset['start']}` - `{dataset['end']}`",
        f"- Bars: `{dataset['bars']}`",
        f"- Timeframe model: {dataset['timeframe_model']}",
        "",
        "## Components",
        "",
        "| Label | Profile | Symbol | Risk per trade | Source Net R | Source Max DD |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for component in portfolio["components"]:
        lines.append(
            f"| `{component['label']}` | `{component['profile']}` | `{component['symbol']}` | "
            f"{_number(component['risk_pct'])}% | {_number(component['source_net_r'])}R | "
            f"{_number(component['source_max_dd_r'])}R |"
        )
    lines.extend([
        "",
        "## Portfolio Performance",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Component trade events | {events['total_trades']} |",
        f"| Wins / losses / BE | {events['wins']} / {events['losses']} / {events['be_trades']} |",
        f"| Win rate | {_number(events['winrate'])}% |",
        f"| Final balance | {_number(performance['final_balance_pct'])}% |",
        f"| Final equity | {_number(performance['final_equity_pct'])}% |",
        f"| Closed-event max DD | {_number(events['max_drawdown'])}% |",
        f"| Realized balance max DD | {_number(performance['balance_drawdown_pct'])}% |",
        f"| Intraday MTM max DD | {_number(performance['intraday_mtm_drawdown_pct'])}% |",
        f"| Profit factor | {_number(events['profit_factor'], 3)} |",
        f"| Expectancy per event | {_number(events['expectancy'], 3)}% |",
        f"| Avg win / loss | {_number(events['avg_win'])}% / {_number(events['avg_loss'])}% |",
        f"| Best / worst event | {_number(events['best_trade'])}% / {_number(events['worst_trade'])}% |",
        f"| Max consecutive event wins / losses | {performance['max_consecutive_event_wins']} / {performance['max_consecutive_event_losses']} |",
        "",
        "## Year-by-year stability",
        "",
        "Trades are component trade events closed during the calendar year. Net, Max DD, and Expectancy are account percent at configured portfolio risk weights.",
        "",
        "| Year | Trades | Net | Max DD | PF | Expectancy |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for row in performance["yearly_stability"]:
        lines.append(
            f"| {row['year']} | {row['trades']} | {_number(row['net_pct'])}% | "
            f"{_number(row['max_drawdown_pct'])}% | {_number(row['profit_factor'], 3)} | "
            f"{_number(row['expectancy_pct'], 3)}% |"
        )
    lines.extend([
        "",
        "## Rolling 365-Day Return",
        "",
    ])
    rolling = performance["rolling_365d"]
    episode = performance["drawdown_episode"]
    lines.extend([
        f"Minimum `{_number(rolling['min_pct'])}%`, median `{_number(rolling['median_pct'])}%`, maximum `{_number(rolling['max_pct'])}%`.",
        "",
        f"Longest max-drawdown episode: `{episode.get('events_peak_to_trough', 'N/A')}` events to trough and "
        f"`{episode.get('events_peak_to_recovery', 'N/A')}` events to recovery.",
        "",
        "## Exposure",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Calendar time in market | {_number(summary['exposure']['calendar_time_in_market_pct'])}% |",
        f"| Bars with position | {_number(summary['exposure']['bars_with_position_pct'])}% |",
        f"| Max simultaneous positions | {summary['exposure']['max_simultaneous_positions']} |",
    ])
    lines.extend([
        "",
        "Exposure overlap is percent of bars where both components are active among bars where either component is active.",
        "",
        "| Pair | Overlap |",
        "|---|---:|",
    ])
    for pair, value in summary["exposure"]["overlap_pct_when_either_active"].items():
        lines.append(f"| `{pair}` | {_number(value)}% |")
    lines.extend([
        "",
        "## Margin Utility",
        "",
        "| Scenario | Mean | Median | P95 | P99 | Maximum | Bars >100% |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for name, values in summary["margin"]["scenarios"].items():
        lines.append(
            f"| `{name}` | {_number(values['mean'])}% | {_number(values['median'])}% | "
            f"{_number(values['p95'])}% | {_number(values['p99'])}% | {_number(values['max'])}% | "
            f"{values['bars_above_100pct']} |"
        )
    lines.extend([
        "",
        "## Daily Equity",
        "",
        "| Reset timezone | Day-start loss P99 / max | Peak-to-trough P99 / max | Equity range P99 / max |",
        "|---|---:|---:|---:|",
    ])
    for timezone_name, values in summary["daily_equity"]["reset_timezones"].items():
        loss = values["loss_from_day_start_baseline_pct"]
        peak = values["pessimistic_peak_to_trough_pct"]
        equity_range = values["daily_equity_range_pct"]
        lines.append(
            f"| {timezone_name} | {_number(loss['p99'])}% / {_number(loss['max'])}% | "
            f"{_number(peak['p99'])}% / {_number(peak['max'])}% | "
            f"{_number(equity_range['p99'])}% / {_number(equity_range['max'])}% |"
        )
    lines.extend([
        "",
        "## Evaluation Rule Simulation",
        "",
        f"Rule profile: `{prop_rules['name']}`; reset timezone `{prop_rules['reset_timezone']}`; "
        f"phase targets `{prop_rules['phase_targets_pct']}`; daily loss `{prop_rules['daily_loss_limit_pct']}%`; "
        f"max loss `{prop_rules['max_loss_limit_pct']}%`.",
        "",
        "| Horizon | Starts | Pass | Fail | Unresolved | Median pass days | Fail causes |",
        "|---:|---:|---:|---:|---:|---:|---|",
    ])
    for horizon, values in summary["challenge"].items():
        causes = ", ".join(
            f"{name}: {_number(value, 1)}%" for name, value in values["fail_causes_pct"].items()
        ) or "None"
        lines.append(
            f"| {horizon}d | {values['starts']} | {_number(values['pass_pct'], 1)}% | "
            f"{_number(values['fail_pct'], 1)}% | {_number(values['unresolved_pct'], 1)}% | "
            f"{_number(values['median_days_to_pass'], 1)} | {causes} |"
        )
    mc = summary["monte_carlo"]
    lines.extend([
        "",
        "## Monte Carlo",
        "",
        f"Mode: `{mc['mode']}`, simulations: `{mc['simulations']}`.",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Mean / median max DD | {_number(mc['mean_dd'])}% / {_number(mc['median_dd'])}% |",
        f"| Best / worst max DD | {_number(mc['best_dd'])}% / {_number(mc['worst_dd'])}% |",
        f"| 95% worst max DD | {_number(mc['p95_dd'])}% |",
        f"| Probability DD > 10% | {_number(mc['dd_gt_10'], 1)}% |",
        f"| Probability DD > 15% | {_number(mc['dd_gt_15'], 1)}% |",
        f"| Probability DD > 20% | {_number(mc['dd_gt_20'], 1)}% |",
        f"| Probability DD > 30% | {_number(mc['dd_gt_30'], 1)}% |",
        "",
        "## Daily Closed-Return Correlation",
        "",
    ])
    corr = summary["correlation"]["daily_closed_return_pct"]
    labels = list(corr)
    lines.append("| Component | " + " | ".join(f"`{label}`" for label in labels) + " |")
    lines.append("|---|" + "|".join("---:" for _ in labels) + "|")
    for row_label in labels:
        row = [f"`{row_label}`"]
        for column_label in labels:
            row.append(_number(corr[row_label][column_label], 3))
        lines.append("| " + " | ".join(row) + " |")
    lines.extend([
        "",
        "## Limitations",
        "",
        *[f"- {item}" for item in summary["limitations"]],
        "",
        "## Artifact Index",
        "",
        *[f"- `{name}` - {description}" for name, description in ARTIFACT_DESCRIPTIONS.items()],
        "",
    ])
    return "\n".join(lines)


def _pyplot(output_dir: Path):
    matplotlib_cache = output_dir.parents[1] / ".matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _plot_equity(profile: Mapping[str, Any], output_dir: Path) -> None:
    intraday = profile["intraday_equity"]
    timestamps = pd.to_datetime(intraday["timestamp"], utc=True)
    if len(intraday) > 25_000:
        step = int(np.ceil(len(intraday) / 25_000))
        plot_frame = intraday.iloc[::step].copy()
        if plot_frame.index[-1] != intraday.index[-1]:
            plot_frame = pd.concat([plot_frame, intraday.tail(1)])
        intraday = plot_frame
        timestamps = pd.to_datetime(intraday["timestamp"], utc=True)
    plt = _pyplot(output_dir)
    plt.style.use("dark_background")
    plt.figure(figsize=(14, 6))
    plt.plot(timestamps, intraday["realized_balance_pct"], label="Realized balance", linewidth=1.2)
    plt.plot(timestamps, intraday["equity_close_pct"], label="Equity close", linewidth=0.8, alpha=0.8)
    plt.fill_between(
        timestamps,
        intraday["equity_low_pct"].astype(float),
        intraday["equity_high_pct"].astype(float),
        alpha=0.15,
        label="Intraday equity envelope",
    )
    plt.title("Portfolio Equity")
    plt.xlabel("Date")
    plt.ylabel("Account %")
    plt.legend()
    plt.grid(True, axis="x", alpha=0.25)
    plt.tight_layout()
    plt.savefig(output_dir / "equity_curve.png", dpi=160)
    plt.close()


def _plot_monthly(profile: Mapping[str, Any], output_dir: Path) -> None:
    monthly = profile["monthly_returns"].copy()
    monthly["close_time"] = pd.to_datetime(monthly["close_time"], utc=True)
    plt = _pyplot(output_dir)
    plt.style.use("dark_background")
    plt.figure(figsize=(16, 5))
    plt.bar(monthly["close_time"].dt.strftime("%Y-%m"), monthly["net_pct"])
    plt.xticks(rotation=90)
    plt.title("Portfolio Monthly Returns")
    plt.ylabel("Account %")
    plt.grid(True, axis="y", alpha=0.25)
    plt.tight_layout()
    plt.savefig(output_dir / "monthly_returns.png", dpi=160)
    plt.close()


def _plot_daily_histogram(profile: Mapping[str, Any], output_dir: Path) -> None:
    daily = profile["daily_equity"]
    utc = daily[daily["reset_timezone"] == daily["reset_timezone"].iloc[0]].copy()
    values = utc["end_balance_pct"] - utc["opening_balance_pct"]
    plt = _pyplot(output_dir)
    plt.style.use("dark_background")
    plt.figure(figsize=(10, 5))
    plt.hist(values, bins=60)
    plt.axvline(0, linestyle="--")
    plt.title("Daily Realized Balance Change Distribution")
    plt.xlabel("Account %")
    plt.ylabel("Days")
    plt.tight_layout()
    plt.savefig(output_dir / "daily_returns_histogram.png", dpi=160)
    plt.close()


def _plot_mc_drawdown(profile: Mapping[str, Any], output_dir: Path) -> None:
    runs = profile["monte_carlo_runs"]
    plt = _pyplot(output_dir)
    plt.style.use("dark_background")
    plt.figure(figsize=(10, 5))
    plt.hist(runs["max_drawdown_pct"], bins=50)
    plt.axvline(runs["max_drawdown_pct"].median(), linestyle="--")
    plt.axvline(runs["max_drawdown_pct"].quantile(0.95), linestyle=":")
    plt.title("Monte Carlo Max Drawdown Distribution")
    plt.xlabel("Max drawdown (%)")
    plt.ylabel("Runs")
    plt.tight_layout()
    plt.savefig(output_dir / "mc_drawdown_histogram.png", dpi=160)
    plt.close()


def write_portfolio_profile(profile: Mapping[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    summary = profile["summary"]
    (output_dir / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    (data_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    profile["component_trades"].to_csv(data_dir / "component_trades.csv", index=False)
    profile["equity_curve"].to_csv(data_dir / "equity_curve.csv", index=False)
    profile["monthly_returns"].to_csv(data_dir / "monthly_returns.csv", index=False)
    profile["yearly_stability"].to_csv(data_dir / "yearly_stability.csv", index=False)
    profile["intraday_equity"].to_csv(data_dir / "intraday_equity.csv", index=False)
    profile["daily_equity"].to_csv(data_dir / "daily_equity.csv", index=False)
    profile["rolling_challenge"].to_csv(data_dir / "rolling_challenge.csv", index=False)
    profile["monte_carlo_runs"].to_csv(data_dir / "monte_carlo_runs.csv", index=False)
    manifest = {
        "schema_version": summary["schema_version"],
        "portfolio_name": summary["portfolio"]["name"],
        "artifacts": ARTIFACT_DESCRIPTIONS,
    }
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    plot_warning = None
    try:
        _plot_equity(profile, output_dir)
        _plot_monthly(profile, output_dir)
        _plot_daily_histogram(profile, output_dir)
        _plot_mc_drawdown(profile, output_dir)
    except ModuleNotFoundError as exc:
        plot_warning = f"Plot generation skipped: missing optional dependency {exc.name!r}."
        (data_dir / "plot_warnings.txt").write_text(
            f"{plot_warning}\n",
            encoding="utf-8",
        )
    if plot_warning:
        with (output_dir / "summary.md").open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Rendering Warnings\n\n- {plot_warning}\n")
    return output_dir / "summary.md"
