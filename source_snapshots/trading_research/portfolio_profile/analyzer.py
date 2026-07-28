"""Assemble portfolio diagnostics from strategy_profile machine artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from engine.backtester import compute_metrics
from overfit_tests.monte_carlo_test.metrics import analyze_mc_results, compute_max_drawdown


MARGIN_PREFIX = "margin_pct_equity_per_1pct_risk_leverage_"


@dataclass(frozen=True)
class ComponentProfile:
    label: str
    profile_name: str
    risk_pct: float
    summary: Mapping[str, Any]
    trades: pd.DataFrame
    intraday: pd.DataFrame


def _distribution(values: Sequence[float], *, absolute: bool = False) -> dict[str, float]:
    series = pd.Series(values, dtype=float).dropna()
    if absolute:
        series = series.abs()
    if series.empty:
        return {key: 0.0 for key in ("mean", "median", "p90", "p95", "p99", "max")}
    return {
        "mean": float(series.mean()),
        "median": float(series.median()),
        "p90": float(series.quantile(0.90)),
        "p95": float(series.quantile(0.95)),
        "p99": float(series.quantile(0.99)),
        "max": float(series.max()),
    }


def _max_streak(values: Sequence[float], predicate) -> int:
    best = current = 0
    for value in values:
        current = current + 1 if predicate(value) else 0
        best = max(best, current)
    return best


def _metrics_for_returns(values: Sequence[float]) -> dict[str, float | int]:
    trades = [{"R": float(value)} for value in values]
    equity = [0.0, *pd.Series(values, dtype=float).cumsum().tolist()]
    return compute_metrics(trades, equity)


def _drawdown_episode(events: pd.DataFrame) -> dict[str, Any]:
    if events.empty:
        return {}
    cumulative = np.r_[0.0, events["portfolio_return_pct"].cumsum().to_numpy(dtype=float)]
    peaks = np.maximum.accumulate(cumulative)
    drawdowns = peaks - cumulative
    trough_index = int(np.argmax(drawdowns))
    peak_index = int(np.argmax(cumulative[: trough_index + 1]))
    recovery_candidates = np.flatnonzero(cumulative[trough_index + 1 :] >= cumulative[peak_index])
    recovery_index = None if not len(recovery_candidates) else trough_index + 1 + int(recovery_candidates[0])

    def event_time(index: int | None):
        if index is None or index == 0:
            return None
        return pd.Timestamp(events.iloc[index - 1]["close_time"])

    return {
        "max_drawdown_pct": float(drawdowns[trough_index]),
        "peak_event_index": peak_index,
        "trough_event_index": trough_index,
        "recovery_event_index": recovery_index,
        "events_peak_to_trough": trough_index - peak_index,
        "events_peak_to_recovery": None if recovery_index is None else recovery_index - peak_index,
        "peak_time": event_time(peak_index),
        "trough_time": event_time(trough_index),
        "recovery_time": event_time(recovery_index),
    }


def _rolling_365d(events: pd.DataFrame) -> dict[str, float]:
    if events.empty:
        return {"min_pct": 0.0, "median_pct": 0.0, "max_pct": 0.0}
    indexed = events.set_index("close_time")["portfolio_return_pct"].sort_index()
    daily = indexed.resample("1D").sum()
    rolling = daily.rolling("365D").sum()
    return {
        "min_pct": float(rolling.min()),
        "median_pct": float(rolling.median()),
        "max_pct": float(rolling.max()),
    }


def _yearly_stability(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if events.empty:
        return pd.DataFrame(columns=[
            "year",
            "trades",
            "net_pct",
            "max_drawdown_pct",
            "profit_factor",
            "expectancy_pct",
        ])
    work = events.sort_values("close_time").copy()
    work["year"] = work["close_time"].dt.year
    for year, year_events in work.groupby("year", sort=True):
        values = year_events["portfolio_return_pct"].to_numpy(dtype=float)
        metrics = _metrics_for_returns(values)
        rows.append({
            "year": int(year),
            "trades": int(metrics["total_trades"]),
            "net_pct": float(metrics["net_r"]),
            "max_drawdown_pct": float(metrics["max_drawdown"]),
            "profit_factor": float(metrics["profit_factor"]),
            "expectancy_pct": float(metrics["expectancy"]),
        })
    return pd.DataFrame(rows)


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    return safe.strip("._-") or "portfolio"


def load_component(profile_root: Path, spec: Mapping[str, Any]) -> ComponentProfile:
    profile_name = str(spec["profile"])
    label = str(spec.get("label") or profile_name)
    risk_pct = float(spec["risk_pct"])
    if risk_pct <= 0:
        raise ValueError(f"Portfolio risk must be positive for {profile_name}")

    data_dir = profile_root / profile_name / "data"
    summary_path = data_dir / "summary.json"
    trades_path = data_dir / "trades.csv"
    intraday_path = data_dir / "intraday_equity.csv"
    missing = [path.name for path in (summary_path, trades_path, intraday_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"{profile_name} lacks strategy profile artifacts: {missing}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trades = pd.read_csv(trades_path, parse_dates=["open_time", "close_time"])
    if "signal_time" in trades.columns:
        trades["signal_time"] = pd.to_datetime(trades["signal_time"], utc=True)
    trades["open_time"] = pd.to_datetime(trades["open_time"], utc=True)
    trades["close_time"] = pd.to_datetime(trades["close_time"], utc=True)

    intraday = pd.read_csv(intraday_path, parse_dates=["timestamp"])
    intraday["timestamp"] = pd.to_datetime(intraday["timestamp"], utc=True)
    return ComponentProfile(
        label=label,
        profile_name=profile_name,
        risk_pct=risk_pct,
        summary=summary,
        trades=trades,
        intraday=intraday,
    )


def _common_index(components: Sequence[ComponentProfile]) -> pd.DatetimeIndex:
    common_start = max(component.intraday["timestamp"].min() for component in components)
    common_end = min(component.intraday["timestamp"].max() for component in components)
    if common_end <= common_start:
        raise ValueError("Portfolio components do not have an overlapping intraday period")
    index = None
    for component in components:
        timestamps = pd.DatetimeIndex(component.intraday["timestamp"])
        timestamps = timestamps[(timestamps >= common_start) & (timestamps <= common_end)]
        index = timestamps if index is None else index.union(timestamps)
    if index is None or index.empty:
        raise ValueError("No overlapping timestamps found for portfolio components")
    return index.sort_values()


def _margin_column(frame: pd.DataFrame, leverage: float) -> str:
    direct = f"{MARGIN_PREFIX}{int(leverage) if float(leverage).is_integer() else leverage}"
    if direct in frame.columns:
        return direct
    target = str(int(leverage) if float(leverage).is_integer() else leverage)
    for column in frame.columns:
        if column.startswith(MARGIN_PREFIX) and column.removeprefix(MARGIN_PREFIX) == target:
            return column
    raise ValueError(f"Missing margin column for effective leverage 1:{leverage}")


def _component_balance(component: ComponentProfile, index: pd.DatetimeIndex) -> np.ndarray:
    trades = component.trades.sort_values("close_time")
    if trades.empty:
        return np.zeros(len(index), dtype=float)
    close_times = pd.DatetimeIndex(trades["close_time"])
    cumulative = trades["R"].cumsum().to_numpy(dtype=float)
    base_pos = close_times.searchsorted(index[0], side="right") - 1
    base = cumulative[base_pos] if base_pos >= 0 else 0.0
    positions = close_times.searchsorted(index, side="right") - 1
    balance = np.zeros(len(index), dtype=float)
    mask = positions >= 0
    balance[mask] = cumulative[positions[mask]]
    return (balance - base) * component.risk_pct


def _align_component_intraday(
    component: ComponentProfile,
    index: pd.DatetimeIndex,
    margin_scenarios: Mapping[str, Mapping[str, Any]],
) -> pd.DataFrame:
    frame = component.intraday.sort_values("timestamp").set_index("timestamp")
    aligned = frame.reindex(index, method="ffill").fillna(0.0)
    output = pd.DataFrame(index=index)
    output[f"{component.label}_equity_close_pct"] = aligned["equity_close_r"].to_numpy(dtype=float) * component.risk_pct
    output[f"{component.label}_equity_low_pct"] = aligned["equity_low_r"].to_numpy(dtype=float) * component.risk_pct
    output[f"{component.label}_equity_high_pct"] = aligned["equity_high_r"].to_numpy(dtype=float) * component.risk_pct
    output[f"{component.label}_active_positions"] = aligned["active_positions"].to_numpy(dtype=float)
    output[f"{component.label}_realized_balance_pct"] = _component_balance(component, index)
    for name, scenario in margin_scenarios.items():
        overrides = scenario.get("overrides", {})
        leverage = overrides.get(component.label, overrides.get(component.profile_name, scenario["default_leverage"]))
        column = _margin_column(aligned, float(leverage))
        output[f"{component.label}_margin_{name}_pct"] = aligned[column].to_numpy(dtype=float) * component.risk_pct
    return output.reset_index(names="timestamp")


def _build_component_trades(components: Sequence[ComponentProfile], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for component in components:
        trades = component.trades[
            (component.trades["close_time"] >= start)
            & (component.trades["close_time"] <= end)
        ].copy()
        if trades.empty:
            continue
        trades.insert(0, "component_label", component.label)
        trades.insert(1, "profile_name", component.profile_name)
        trades["risk_pct"] = component.risk_pct
        trades["component_r"] = trades["R"].astype(float)
        trades["portfolio_return_pct"] = trades["component_r"] * component.risk_pct
        rows.append(trades)
    if not rows:
        raise RuntimeError("Portfolio profile requires at least one component trade in the common period")
    output = pd.concat(rows, ignore_index=True).sort_values(["close_time", "component_label"])
    return output.reset_index(drop=True)


def _build_intraday(
    components: Sequence[ComponentProfile],
    margin_scenarios: Mapping[str, Mapping[str, Any]],
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    index = _common_index(components)
    frames = [
        _align_component_intraday(component, index, margin_scenarios).set_index("timestamp")
        for component in components
    ]
    combined = pd.concat(frames, axis=1)
    result = pd.DataFrame({"timestamp": index})
    close_cols = [f"{component.label}_equity_close_pct" for component in components]
    low_cols = [f"{component.label}_equity_low_pct" for component in components]
    high_cols = [f"{component.label}_equity_high_pct" for component in components]
    active_cols = [f"{component.label}_active_positions" for component in components]
    balance_cols = [f"{component.label}_realized_balance_pct" for component in components]
    result["realized_balance_pct"] = combined[balance_cols].sum(axis=1).to_numpy(dtype=float)
    result["equity_close_pct"] = combined[close_cols].sum(axis=1).to_numpy(dtype=float)
    result["equity_low_pct"] = combined[low_cols].sum(axis=1).to_numpy(dtype=float)
    result["equity_high_pct"] = combined[high_cols].sum(axis=1).to_numpy(dtype=float)
    result["active_positions"] = combined[active_cols].sum(axis=1).to_numpy(dtype=int)
    for scenario_name in margin_scenarios:
        cols = [f"{component.label}_margin_{scenario_name}_pct" for component in components]
        result[f"margin_{scenario_name}_pct"] = combined[cols].sum(axis=1).to_numpy(dtype=float)
    for component in components:
        column = f"{component.label}_active_positions"
        result[column] = combined[column].to_numpy(dtype=float)
    return result, index


def _daily_equity(intraday: pd.DataFrame, reset_timezones: Sequence[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    timestamps = pd.Series(pd.to_datetime(intraday["timestamp"], utc=True))
    rows = []
    summary = {}
    for timezone_name in reset_timezones:
        local_dates = np.array(timestamps.dt.tz_convert(timezone_name).dt.date)
        day_starts, day_ends, _, _ = _time_groups(timestamps, timezone_name)
        timezone_rows = []
        for start, end in zip(day_starts, day_ends):
            date = local_dates[int(start)]
            day = intraday.iloc[int(start):int(end) + 1]
            opening_balance = float(day.iloc[0]["realized_balance_pct"])
            opening_equity = float(day.iloc[0]["equity_close_pct"])
            low_equity = float(day["equity_low_pct"].min())
            high_equity = float(day["equity_high_pct"].max())
            peak = max(opening_balance, opening_equity)
            peak_to_trough = 0.0
            for row in day.itertuples(index=False):
                peak = max(peak, float(row.equity_high_pct))
                peak_to_trough = max(peak_to_trough, peak - float(row.equity_low_pct))
            record = {
                "reset_timezone": timezone_name,
                "date": str(date),
                "opening_balance_pct": opening_balance,
                "opening_equity_pct": opening_equity,
                "end_balance_pct": float(day.iloc[-1]["realized_balance_pct"]),
                "end_equity_pct": float(day.iloc[-1]["equity_close_pct"]),
                "loss_from_opening_balance_pct": max(0.0, opening_balance - low_equity),
                "loss_from_opening_equity_pct": max(0.0, opening_equity - low_equity),
                "loss_from_day_start_baseline_pct": max(
                    0.0, max(opening_balance, opening_equity) - low_equity
                ),
                "daily_equity_range_pct": high_equity - low_equity,
                "pessimistic_peak_to_trough_pct": peak_to_trough,
                "bars_with_position": int((day["active_positions"] > 0).sum()),
                "max_simultaneous_positions": int(day["active_positions"].max()),
            }
            rows.append(record)
            timezone_rows.append(record)
        frame = pd.DataFrame(timezone_rows)
        active = frame[frame["bars_with_position"] > 0]
        summary[timezone_name] = {
            "days": len(frame),
            "days_with_position": len(active),
            "loss_from_opening_balance_pct": _distribution(frame["loss_from_opening_balance_pct"]),
            "loss_from_opening_equity_pct": _distribution(frame["loss_from_opening_equity_pct"]),
            "loss_from_day_start_baseline_pct": _distribution(
                frame["loss_from_day_start_baseline_pct"]
            ),
            "daily_equity_range_pct": _distribution(frame["daily_equity_range_pct"]),
            "pessimistic_peak_to_trough_pct": _distribution(frame["pessimistic_peak_to_trough_pct"]),
            "absolute_daily_balance_change_pct": _distribution(
                frame["end_balance_pct"] - frame["opening_balance_pct"], absolute=True
            ),
        }
    return pd.DataFrame(rows), summary


def _time_groups(timestamps: pd.Series, timezone_name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    local_dates = np.array(timestamps.dt.tz_convert(timezone_name).dt.date)
    starts = np.r_[0, np.flatnonzero(local_dates[1:] != local_dates[:-1]) + 1]
    ends = np.r_[starts[1:] - 1, len(timestamps) - 1]
    weekdays = np.array([day.weekday() for day in local_dates[starts]])
    day_of_position = np.empty(len(timestamps), dtype=np.int32)
    for index, (start, end) in enumerate(zip(starts, ends)):
        day_of_position[start:end + 1] = index
    return starts, ends, weekdays, day_of_position


def _challenge_phase(
    *,
    start_index: int,
    end_index: int,
    target_pct: float,
    intraday: pd.DataFrame,
    day_starts: np.ndarray,
    day_ends: np.ndarray,
    day_of_position: np.ndarray,
    daily_loss_limit: float,
    max_loss_limit: float,
    target_requires_flat: bool,
) -> tuple[str, int | None, str | None]:
    balance = intraday["realized_balance_pct"].to_numpy(dtype=float)
    close = intraday["equity_close_pct"].to_numpy(dtype=float)
    low = intraday["equity_low_pct"].to_numpy(dtype=float)
    active = intraday["active_positions"].to_numpy(dtype=int)
    base_balance = float(balance[start_index])
    for day_index in range(int(day_of_position[start_index]), int(day_of_position[end_index]) + 1):
        start = max(int(day_starts[day_index]), start_index)
        end = min(int(day_ends[day_index]), end_index)
        if end < start:
            continue
        relative_low = low[start:end + 1] - base_balance
        relative_balance = balance[start:end + 1] - base_balance
        opening_balance = float(balance[start] - base_balance)
        opening_equity = float(close[start] - base_balance)
        daily_baseline = max(opening_balance, opening_equity)
        max_breach = (
            float(np.min(relative_low)) <= -max_loss_limit
            or float(np.min(relative_balance)) <= -max_loss_limit
        )
        daily_breach = float(np.min(relative_low)) <= daily_baseline - daily_loss_limit
        if max_breach or daily_breach:
            if max_breach and daily_breach:
                return "fail", None, "daily_loss+max_loss"
            return "fail", None, "max_loss" if max_breach else "daily_loss"
        target_hits = relative_balance >= target_pct
        if target_requires_flat:
            target_hits = target_hits & (active[start:end + 1] == 0)
        hits = np.flatnonzero(target_hits)
        if len(hits):
            return "pass", start + int(hits[0]), None
    return "unresolved", None, None


def _challenge_simulation(
    intraday: pd.DataFrame,
    prop_rules: Mapping[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    timestamps = pd.Series(pd.to_datetime(intraday["timestamp"], utc=True))
    reset_timezone = str(prop_rules["reset_timezone"])
    day_starts, day_ends, weekdays, day_of_position = _time_groups(timestamps, reset_timezone)
    start_weekday = int(prop_rules.get("rolling_start_weekday", 0))
    require_flat = bool(prop_rules.get("target_requires_flat", True))
    active = intraday["active_positions"].to_numpy(dtype=int)
    start_candidates = [
        int(position)
        for position, weekday in zip(day_starts, weekdays)
        if weekday == start_weekday and (not require_flat or active[int(position)] == 0)
    ]
    phase_targets = [float(value) for value in prop_rules["phase_targets_pct"]]
    daily_loss_limit = float(prop_rules["daily_loss_limit_pct"])
    max_loss_limit = float(prop_rules["max_loss_limit_pct"])
    rows = []
    summary = {}
    for horizon_days in prop_rules.get("horizons_days", [365]):
        horizon = int(horizon_days)
        horizon_rows = []
        pass_days = []
        fail_causes: dict[str, int] = {}
        for start_index in start_candidates:
            end_time = timestamps.iloc[start_index] + pd.Timedelta(days=horizon)
            end_index = int(timestamps.searchsorted(end_time, side="right") - 1)
            if end_index <= start_index:
                status, phase_reached, finish_index, cause = "unresolved", 0, None, None
            else:
                current_start = start_index
                status, phase_reached, finish_index, cause = "unresolved", 0, None, None
                for phase_number, target in enumerate(phase_targets, start=1):
                    phase_status, phase_finish, phase_cause = _challenge_phase(
                        start_index=current_start,
                        end_index=end_index,
                        target_pct=target,
                        intraday=intraday,
                        day_starts=day_starts,
                        day_ends=day_ends,
                        day_of_position=day_of_position,
                        daily_loss_limit=daily_loss_limit,
                        max_loss_limit=max_loss_limit,
                        target_requires_flat=require_flat,
                    )
                    if phase_status == "fail":
                        status, cause = "fail", phase_cause
                        break
                    if phase_status != "pass" or phase_finish is None:
                        status = "unresolved"
                        break
                    phase_reached = phase_number
                    current_start = phase_finish
                    finish_index = phase_finish
                else:
                    status = "pass"
            days_to_finish = None
            finish_time = None
            if finish_index is not None:
                finish_time = timestamps.iloc[finish_index]
                days_to_finish = (finish_time - timestamps.iloc[start_index]).total_seconds() / 86400.0
            if status == "pass" and days_to_finish is not None:
                pass_days.append(days_to_finish)
            if status == "fail" and cause is not None:
                fail_causes[cause] = fail_causes.get(cause, 0) + 1
            record = {
                "horizon_days": horizon,
                "start_time": timestamps.iloc[start_index],
                "finish_time": finish_time,
                "status": status,
                "phase_reached": phase_reached,
                "fail_cause": cause,
                "days_to_finish": days_to_finish,
            }
            rows.append(record)
            horizon_rows.append(record)
        total = len(horizon_rows)
        pass_count = sum(row["status"] == "pass" for row in horizon_rows)
        fail_count = sum(row["status"] == "fail" for row in horizon_rows)
        unresolved_count = total - pass_count - fail_count
        summary[str(horizon)] = {
            "starts": total,
            "pass_pct": 0.0 if total == 0 else pass_count / total * 100.0,
            "fail_pct": 0.0 if total == 0 else fail_count / total * 100.0,
            "unresolved_pct": 0.0 if total == 0 else unresolved_count / total * 100.0,
            "median_days_to_pass": None if not pass_days else float(np.median(pass_days)),
            "mean_days_to_pass": None if not pass_days else float(np.mean(pass_days)),
            "fail_causes_pct": {
                cause: count / total * 100.0 if total else 0.0
                for cause, count in fail_causes.items()
            },
        }
    return pd.DataFrame(rows), summary


def _monte_carlo(events: pd.DataFrame, config: Mapping[str, Any]) -> tuple[dict[str, Any], pd.DataFrame]:
    returns = events["portfolio_return_pct"].to_numpy(dtype=float)
    simulations = int(config.get("simulations", 1000))
    seed = int(config.get("random_seed", 42))
    rng = np.random.default_rng(seed)
    curves = []
    for _ in range(simulations):
        shuffled = rng.permutation(returns)
        curves.append(np.r_[0.0, np.cumsum(shuffled)])
    report = {"mode": str(config.get("mode", "trade_shuffle")), **analyze_mc_results(curves)}
    runs = pd.DataFrame({
        "simulation": np.arange(1, simulations + 1),
        "final_pct": [float(curve[-1]) for curve in curves],
        "max_drawdown_pct": [float(compute_max_drawdown(curve)) for curve in curves],
    })
    return report, runs


def _daily_component_correlation(events: pd.DataFrame, timezone_name: str) -> dict[str, Any]:
    frame = events.copy()
    frame["date"] = frame["close_time"].dt.tz_convert(timezone_name).dt.date
    pivot = frame.pivot_table(
        index="date",
        columns="component_label",
        values="portfolio_return_pct",
        aggfunc="sum",
        fill_value=0.0,
    )
    return pivot.corr().round(6).to_dict()


def _exposure_overlap(intraday: pd.DataFrame, components: Sequence[ComponentProfile]) -> dict[str, float]:
    result = {}
    for left_index, left in enumerate(components):
        for right in components[left_index + 1:]:
            left_active = intraday[f"{left.label}_active_positions"].to_numpy(dtype=float) > 0
            right_active = intraday[f"{right.label}_active_positions"].to_numpy(dtype=float) > 0
            either = left_active | right_active
            result[f"{left.label}{right.label}"] = (
                0.0 if not np.any(either) else float(np.mean((left_active & right_active)[either]) * 100.0)
            )
    return result


def build_portfolio_profile(
    *,
    profile_root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    components = [load_component(profile_root, spec) for spec in config["components"]]
    if len({component.label for component in components}) != len(components):
        raise ValueError("Portfolio component labels must be unique")
    margin_scenarios = config.get("margin_scenarios", {})
    intraday, index = _build_intraday(components, margin_scenarios)
    events = _build_component_trades(components, index[0], index[-1])
    values = events["portfolio_return_pct"].to_numpy(dtype=float)
    metrics = _metrics_for_returns(values)
    daily, daily_summary = _daily_equity(intraday, config.get("equity_reset_timezones", ["UTC"]))
    challenge, challenge_summary = _challenge_simulation(intraday, config["prop_rules"])
    mc_report, mc_runs = _monte_carlo(events, config.get("monte_carlo", {}))
    equity_curve = (
        events.groupby("close_time", as_index=False)["portfolio_return_pct"]
        .sum()
        .sort_values("close_time")
    )
    equity_curve["equity_pct"] = equity_curve["portfolio_return_pct"].cumsum()
    monthly = (
        events.set_index("close_time")["portfolio_return_pct"]
        .resample("ME")
        .sum()
        .rename("net_pct")
        .reset_index()
    )
    yearly_stability = _yearly_stability(events)
    margin_summary = {
        name: _distribution(intraday[f"margin_{name}_pct"])
        for name in margin_scenarios
    }
    for name in margin_scenarios:
        margin_summary[name]["active_p95"] = _distribution(
            intraday.loc[intraday["active_positions"] > 0, f"margin_{name}_pct"]
        )["p95"]
        margin_summary[name]["bars_above_100pct"] = int((intraday[f"margin_{name}_pct"] > 100.0).sum())

    timestamps = pd.to_datetime(intraday["timestamp"], utc=True)
    interval = timestamps.diff().dropna()
    interval = interval[interval > pd.Timedelta(0)]
    bar_seconds = interval.min().total_seconds() if not interval.empty else 0.0
    period_seconds = max(1.0, (timestamps.iloc[-1] - timestamps.iloc[0]).total_seconds())
    summary = {
        "schema_version": 1,
        "portfolio": {
            "name": _safe_name(str(config["name"])),
            "components": [
                {
                    "label": component.label,
                    "profile": component.profile_name,
                    "risk_pct": component.risk_pct,
                    "strategy_name": component.summary["strategy"]["strategy_name"],
                    "symbol": component.summary["strategy"]["symbol"],
                    "strategy_class": component.summary["strategy"]["strategy_class"],
                    "source_net_r": component.summary["backtest"]["segments"]["full"]["net_r"],
                    "source_max_dd_r": component.summary["backtest"]["segments"]["full"]["max_drawdown"],
                }
                for component in components
            ],
        },
        "dataset": {
            "mode": "assembled_from_strategy_profiles",
            "bars": len(intraday),
            "start": timestamps.iloc[0],
            "end": timestamps.iloc[-1],
            "timeframe_model": "union of component intraday timestamps; missing component bars are forward-filled",
        },
        "performance": {
            "events": metrics,
            "intraday_mtm_drawdown_pct": float(
                np.max(np.maximum.accumulate(intraday["equity_high_pct"]) - intraday["equity_low_pct"])
            ),
            "balance_drawdown_pct": float(
                np.max(np.maximum.accumulate(intraday["realized_balance_pct"]) - intraday["realized_balance_pct"])
            ),
            "final_balance_pct": float(intraday["realized_balance_pct"].iloc[-1]),
            "final_equity_pct": float(intraday["equity_close_pct"].iloc[-1]),
            "max_consecutive_event_wins": _max_streak(values, lambda value: value > 0),
            "max_consecutive_event_losses": _max_streak(values, lambda value: value < 0),
            "drawdown_episode": _drawdown_episode(events),
            "rolling_365d": _rolling_365d(events),
            "yearly_stability": yearly_stability.to_dict("records"),
        },
        "exposure": {
            "calendar_time_in_market_pct": float((intraday["active_positions"] > 0).sum() * bar_seconds / period_seconds * 100.0),
            "bars_with_position_pct": float((intraday["active_positions"] > 0).mean() * 100.0),
            "max_simultaneous_positions": int(intraday["active_positions"].max()),
            "overlap_pct_when_either_active": _exposure_overlap(intraday, components),
        },
        "daily_equity": {
            "reset_timezones": daily_summary,
        },
        "margin": {
            "unit": "percent of account equity occupied at configured risk weights",
            "scenarios": margin_summary,
            "scenario_config": dict(margin_scenarios),
        },
        "prop_rules": dict(config["prop_rules"]),
        "challenge": challenge_summary,
        "monte_carlo": mc_report,
        "correlation": {
            "daily_closed_return_pct": _daily_component_correlation(
                events, str(config["prop_rules"]["reset_timezone"])
            ),
        },
        "limitations": [
            "Portfolio profile is assembled from completed strategy_profile artifacts; it does not re-run strategy logic.",
            "Component intraday equity is exact only at each component profile timeframe; between missing timestamps it is forward-filled.",
            "The report does not model live portfolio guards that reject new entries, force-close positions, or resize orders after a breach.",
            "Trade-shuffle Monte Carlo breaks cross-strategy timing and regime dependence; use it only as a rough tail diagnostic.",
            "Evaluation rolling starts overlap and should be read as a historical stress map, not independent probabilities.",
        ],
    }
    return {
        "summary": summary,
        "component_trades": events,
        "equity_curve": equity_curve,
        "monthly_returns": monthly,
        "yearly_stability": yearly_stability,
        "intraday_equity": intraday,
        "daily_equity": daily,
        "rolling_challenge": challenge,
        "monte_carlo_runs": mc_runs,
    }
