"""Build normalized trade, margin, and mark-to-market strategy diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from engine.backtester import compute_metrics
from overfit_tests.monte_carlo_test.metrics import analyze_mc_results, compute_max_drawdown
from overfit_tests.monte_carlo_test.simulator import run_shuffle_mc, run_synthetic_mc


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


def _metrics_for_trades(frame: pd.DataFrame) -> dict[str, float | int]:
    if frame.empty:
        return compute_metrics([], [0.0])
    records = frame.to_dict("records")
    equity = [0.0, *frame["R"].cumsum().astype(float).tolist()]
    return compute_metrics(records, equity)


def _drawdown_episode(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {}
    cumulative = np.r_[0.0, trades["R"].cumsum().to_numpy(dtype=float)]
    peaks = np.maximum.accumulate(cumulative)
    drawdowns = peaks - cumulative
    trough_index = int(np.argmax(drawdowns))
    peak_index = int(np.argmax(cumulative[: trough_index + 1]))
    recovery_candidates = np.flatnonzero(cumulative[trough_index + 1 :] >= cumulative[peak_index])
    recovery_index = None
    if len(recovery_candidates):
        recovery_index = trough_index + 1 + int(recovery_candidates[0])

    def trade_time(index: int | None):
        if index is None or index == 0:
            return None
        return pd.Timestamp(trades.iloc[index - 1]["close_time"])

    return {
        "max_drawdown_r": float(drawdowns[trough_index]),
        "peak_trade_index": peak_index,
        "trough_trade_index": trough_index,
        "recovery_trade_index": recovery_index,
        "trades_peak_to_trough": trough_index - peak_index,
        "trades_peak_to_recovery": None if recovery_index is None else recovery_index - peak_index,
        "peak_time": trade_time(peak_index),
        "trough_time": trade_time(trough_index),
        "recovery_time": trade_time(recovery_index),
    }


def _validate_trade_diagnostics(trades: Sequence[Mapping[str, Any]]) -> None:
    required = {
        "initial_sl", "initial_risk_price", "open_bar", "close_bar", "duration_bars"
    }
    for index, trade in enumerate(trades):
        missing = required.difference(trade)
        if missing:
            raise ValueError(
                f"Trade {index} lacks strategy-profile diagnostics: {sorted(missing)}"
            )
        if float(trade["initial_risk_price"]) <= 0:
            raise ValueError(f"Trade {index} has non-positive initial risk")


def build_intraday_equity(
    df: pd.DataFrame,
    trades: Sequence[Mapping[str, Any]],
    *,
    margin_leverages: Sequence[float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return conservative bar equity envelope and enriched trade diagnostics.

    Fixed R costs are recognized from entry for a conservative prop-equity view.
    For overlapping positions, bar lows/highs assume adverse/favorable extrema can
    coincide. M30 OHLC cannot reveal the true intrabar path, so this is explicitly
    an envelope rather than a tick-perfect reconstruction.
    """
    _validate_trade_diagnostics(trades)
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    bar_count = len(df)

    realized_events = np.zeros(bar_count + 1, dtype=float)
    close_marks = np.zeros(bar_count, dtype=float)
    low_marks = np.zeros(bar_count, dtype=float)
    high_marks = np.zeros(bar_count, dtype=float)
    active_positions = np.zeros(bar_count, dtype=int)
    margin = {
        float(leverage): np.zeros(bar_count, dtype=float)
        for leverage in margin_leverages
    }
    enriched = []

    positive_intervals = timestamps.diff().dropna()
    positive_intervals = positive_intervals[positive_intervals > pd.Timedelta(0)]
    bar_interval = positive_intervals.min() if not positive_intervals.empty else pd.Timedelta(0)

    for trade in trades:
        record = dict(trade)
        open_bar = int(record["open_bar"])
        close_bar = int(record["close_bar"])
        if not (0 <= open_bar <= close_bar < bar_count):
            raise ValueError(
                f"Trade bar range [{open_bar}, {close_bar}] is outside dataset"
            )
        entry = float(record["entry"])
        risk = float(record["initial_risk_price"])
        side = str(record["side"]).upper()
        direction = 1.0 if side == "BUY" else -1.0
        costs_r = float(record.get("costs_r", 0.0))
        bars = np.arange(open_bar, close_bar + 1)

        close_r = direction * (closes[bars] - entry) / risk - costs_r
        if side == "BUY":
            low_r = (lows[bars] - entry) / risk - costs_r
            high_r = (highs[bars] - entry) / risk - costs_r
        elif side == "SELL":
            low_r = (entry - highs[bars]) / risk - costs_r
            high_r = (entry - lows[bars]) / risk - costs_r
        else:
            raise ValueError(f"Unsupported trade side: {side!r}")

        # The close-bar equity ends at the actual modeled fill, not at the OHLC close.
        close_r[-1] = float(record["R"])
        close_reason = str(record.get("close_reason", ""))
        if close_reason.startswith("sl") or close_reason == "break_even":
            # Prices beyond the executed protective stop are not an equity loss.
            low_r[-1] = float(record["R"])
        else:
            low_r[-1] = min(float(low_r[-1]), float(record["R"]))
        high_r[-1] = max(float(high_r[-1]), float(record["R"]))

        close_marks[bars] += close_r
        low_marks[bars] += low_r
        high_marks[bars] += high_r
        active_positions[bars] += 1
        if close_bar + 1 < bar_count:
            realized_events[close_bar + 1] += float(record["R"])

        margin_per_1pct = {}
        for leverage, values in margin.items():
            # Linear price-risk instruments: contract size and account-currency
            # conversion cancel in the normalized margin/risk ratio. Leverage is
            # therefore effective symbol leverage, not necessarily account leverage.
            value = entry / (leverage * risk)
            values[bars] += value
            margin_per_1pct[str(int(leverage) if leverage.is_integer() else leverage)] = value

        record["mae_r"] = max(0.0, -float(np.min(low_r)))
        record["mfe_r"] = max(0.0, float(np.max(high_r)))
        record["bar_coverage_hours"] = float(
            (close_bar - open_bar + 1) * bar_interval.total_seconds() / 3600
        )
        record["margin_pct_equity_per_1pct_risk"] = margin_per_1pct
        enriched.append(record)

    realized = np.cumsum(realized_events[:-1])
    close_equity = realized + close_marks
    low_equity = realized + low_marks
    high_equity = realized + high_marks
    inactive = active_positions == 0
    close_equity[inactive] = realized[inactive]
    low_equity[inactive] = realized[inactive]
    high_equity[inactive] = realized[inactive]

    intraday = pd.DataFrame({
        "timestamp": timestamps,
        "equity_close_r": close_equity,
        "equity_low_r": low_equity,
        "equity_high_r": high_equity,
        "active_positions": active_positions,
    })
    for leverage, values in margin.items():
        label = int(leverage) if leverage.is_integer() else leverage
        intraday[f"margin_pct_equity_per_1pct_risk_leverage_{label}"] = values

    return intraday, pd.DataFrame(enriched)


def build_daily_equity(
    intraday: pd.DataFrame,
    reset_timezones: Sequence[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    timestamps = pd.to_datetime(intraday["timestamp"], utc=True)
    rows = []
    summary = {}

    for timezone_name in reset_timezones:
        dates = timestamps.dt.tz_convert(timezone_name).dt.date
        previous_close = 0.0
        timezone_rows = []
        for date in pd.unique(dates):
            indexes = np.flatnonzero(np.asarray(dates == date))
            day = intraday.iloc[indexes]
            start_equity = previous_close
            low_equity = float(day["equity_low_r"].min())
            high_equity = float(day["equity_high_r"].max())
            peak = start_equity
            peak_to_trough = 0.0
            for row in day.itertuples(index=False):
                peak = max(peak, float(row.equity_high_r))
                peak_to_trough = max(peak_to_trough, peak - float(row.equity_low_r))
            end_equity = float(day.iloc[-1]["equity_close_r"])
            row = {
                "reset_timezone": timezone_name,
                "date": str(date),
                "start_equity_r": start_equity,
                "end_equity_r": end_equity,
                "daily_close_change_r": end_equity - start_equity,
                "loss_from_day_start_r": max(0.0, start_equity - low_equity),
                "daily_equity_range_r": high_equity - low_equity,
                "pessimistic_peak_to_trough_r": peak_to_trough,
                "bars_with_position": int((day["active_positions"] > 0).sum()),
                "max_simultaneous_positions": int(day["active_positions"].max()),
            }
            rows.append(row)
            timezone_rows.append(row)
            previous_close = end_equity

        timezone_frame = pd.DataFrame(timezone_rows)
        active_days = timezone_frame[timezone_frame["bars_with_position"] > 0]
        summary[timezone_name] = {
            "days": len(timezone_frame),
            "days_with_position": len(active_days),
            "loss_from_day_start_r": _distribution(timezone_frame["loss_from_day_start_r"]),
            "loss_from_day_start_active_days_r": _distribution(active_days["loss_from_day_start_r"]),
            "daily_equity_range_r": _distribution(timezone_frame["daily_equity_range_r"]),
            "pessimistic_peak_to_trough_r": _distribution(
                timezone_frame["pessimistic_peak_to_trough_r"]
            ),
            "absolute_daily_close_change_r": _distribution(
                timezone_frame["daily_close_change_r"], absolute=True
            ),
        }
    return pd.DataFrame(rows), summary


def _segment_metrics(
    trades: pd.DataFrame,
    split_config: Mapping[str, Any],
) -> dict[str, Any]:
    result = {"full": _metrics_for_trades(trades)}
    if split_config.get("mode") != "manual":
        return result
    dates = split_config.get("dates", {})
    for name in ("train", "holdout"):
        start = pd.Timestamp(dates[f"{name}_start"])
        end = pd.Timestamp(dates[f"{name}_end"])
        subset = trades[
            (trades["open_time"] >= start) & (trades["open_time"] < end)
        ]
        result[name] = _metrics_for_trades(subset)
    return result


def _yearly_metrics(trades: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for year, frame in trades.groupby(trades["close_time"].dt.year):
        rows.append({"year": int(year), **_metrics_for_trades(frame)})
    return rows


def _rolling_365d(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"min_r": 0.0, "median_r": 0.0, "max_r": 0.0}
    indexed = trades.set_index("close_time")["R"].sort_index()
    daily = indexed.resample("1D").sum()
    rolling = daily.rolling("365D").sum()
    return {
        "min_r": float(rolling.min()),
        "median_r": float(rolling.median()),
        "max_r": float(rolling.max()),
    }


def _margin_summary(
    trades: pd.DataFrame,
    margin_leverages: Sequence[float],
) -> dict[str, Any]:
    result = {}
    for leverage in margin_leverages:
        values = trades["entry"] / (float(leverage) * trades["initial_risk_price"])
        distribution = _distribution(values)
        max_index = values.idxmax()
        max_trade = trades.loc[max_index]
        distribution["trades_above_25pct_equity"] = int((values > 25.0).sum())
        distribution["trades_above_50pct_equity"] = int((values > 50.0).sum())
        distribution["trades_above_100pct_equity"] = int((values > 100.0).sum())
        distribution["max_margin_trade"] = {
            "open_time": max_trade["open_time"],
            "side": max_trade["side"],
            "entry": float(max_trade["entry"]),
            "initial_sl": float(max_trade["initial_sl"]),
            "initial_risk_price": float(max_trade["initial_risk_price"]),
            "result_r": float(max_trade["R"]),
            "close_reason": max_trade["close_reason"],
        }
        result[str(int(leverage) if float(leverage).is_integer() else leverage)] = distribution
    return result


def _monte_carlo(
    trades: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    mode = str(config["mode"])
    simulations = int(config["simulations"])
    seed = int(config["random_seed"])
    if mode == "shuffle":
        curves = run_shuffle_mc(trades, simulations=simulations, seed=seed)
    elif mode == "synthetic":
        total = int(metrics["total_trades"])
        curves = run_synthetic_mc(
            winrate=float(metrics["wins"]) / total,
            be_rate=float(metrics["be_trades"]) / total,
            avg_win=float(metrics["avg_win"]),
            avg_loss=float(metrics["avg_loss"]),
            trades_per_run=total,
            simulations=simulations,
            seed=seed,
        )
    else:
        raise ValueError(f"Unknown Monte Carlo mode: {mode}")
    report = {"mode": mode, **analyze_mc_results(curves)}
    runs = pd.DataFrame({
        "simulation": np.arange(1, len(curves) + 1),
        "final_r": [float(curve[-1]) for curve in curves],
        "max_drawdown_r": [float(compute_max_drawdown(curve)) for curve in curves],
    })
    return report, runs


def build_strategy_profile(
    *,
    df: pd.DataFrame,
    trades: Sequence[Mapping[str, Any]],
    equity: Sequence[float],
    metrics: Mapping[str, Any],
    strategy_params: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    split_config: Mapping[str, Any],
    monte_carlo_config: Mapping[str, Any],
    profile_config: Mapping[str, Any],
    data_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    if not trades:
        raise RuntimeError("Strategy profile requires at least one closed trade")
    margin_leverages = [float(value) for value in profile_config["margin_leverages"]]
    reset_timezones = list(profile_config["equity_reset_timezones"])
    intraday, enriched_trades = build_intraday_equity(
        df, trades, margin_leverages=margin_leverages
    )
    enriched_trades["open_time"] = pd.to_datetime(enriched_trades["open_time"], utc=True)
    enriched_trades["signal_time"] = pd.to_datetime(enriched_trades["signal_time"], utc=True)
    enriched_trades["close_time"] = pd.to_datetime(enriched_trades["close_time"], utc=True)
    daily, daily_summary = build_daily_equity(intraday, reset_timezones)
    mc_report, mc_runs = _monte_carlo(
        enriched_trades.to_dict("records"), metrics, monte_carlo_config
    )

    monthly = (
        enriched_trades.set_index("close_time")["R"]
        .resample("ME")
        .sum()
        .rename("net_r")
        .reset_index()
    )
    trade_equity = pd.DataFrame({
        "close_time": enriched_trades["close_time"],
        "trade_r": enriched_trades["R"],
        "equity_r": enriched_trades["R"].cumsum(),
    })
    dataset_reference = metrics.get("dataset_reference", {})
    manifest = dataset_reference.get("manifest", {}) if isinstance(dataset_reference, Mapping) else {}
    dataset_timeframe = str(manifest.get("timeframe") or "unknown")
    signal_reference = metrics.get("signal_dataset_reference", dataset_reference)
    signal_manifest = signal_reference.get("manifest", {}) if isinstance(signal_reference, Mapping) else {}
    signal_timeframe = str(signal_manifest.get("timeframe") or dataset_timeframe)
    same_bar_mask = enriched_trades["duration_bars"].eq(1)
    same_bar_both_hit = same_bar_mask & enriched_trades["close_reason"].eq("sl_before_tp")
    execution_summary = dict(metrics.get("execution", {}))
    execution_summary["costs"] = dict(metrics.get("execution_costs", {}))
    execution_summary["cost_profile"] = dict(
        metrics.get("execution_cost_profile", {})
    )
    summary = {
        "schema_version": 1,
        "strategy": {
            "strategy_name": profile_config["strategy_name"],
            "symbol": profile_config["symbol"],
            "asset_class": profile_config["asset_class"],
            "strategy_class": profile_config["strategy_class"],
        },
        "dataset": {
            "mode": profile_config["dataset"],
            "bars": len(df),
            "start": pd.Timestamp(df["timestamp"].iloc[0]),
            "end": pd.Timestamp(df["timestamp"].iloc[-1]),
            "reference": dataset_reference,
            "venue": manifest.get("venue"),
            "timezone": manifest.get("timezone"),
            "timeframe": dataset_timeframe,
            "signal_timeframe": signal_timeframe,
            "signal_reference": signal_reference,
            "content_sha256": manifest.get("content_sha256") or manifest.get("sha256"),
            "warnings": list(data_warnings),
        },
        "parameters": dict(strategy_params),
        "management": dict(backtest_config),
        "execution": execution_summary,
        "backtest": {
            "segments": _segment_metrics(enriched_trades, split_config),
            "yearly": _yearly_metrics(enriched_trades),
            "rolling_365d": _rolling_365d(enriched_trades),
            "max_consecutive_wins": _max_streak(enriched_trades["R"], lambda value: value > 0),
            "max_consecutive_losses": _max_streak(enriched_trades["R"], lambda value: value < 0),
            "drawdown_episode": _drawdown_episode(enriched_trades),
            "close_reasons": enriched_trades["close_reason"].value_counts().to_dict(),
        },
        "monte_carlo": mc_report,
        "trade_diagnostics": {
            "mae_r": _distribution(enriched_trades["mae_r"]),
            "mfe_r": _distribution(enriched_trades["mfe_r"]),
            "initial_risk_price": _distribution(enriched_trades["initial_risk_price"]),
            "bar_coverage_hours": _distribution(enriched_trades["bar_coverage_hours"]),
            "available_bars_with_position_pct": float(
                (intraday["active_positions"] > 0).mean() * 100.0
            ),
            "calendar_time_in_market_pct": float(
                (intraday["active_positions"] > 0).sum()
                * pd.Series(pd.to_datetime(df["timestamp"], utc=True)).diff().dropna().loc[
                    lambda values: values > pd.Timedelta(0)
                ].min().total_seconds()
                / max(
                    1.0,
                    (
                        pd.Timestamp(df["timestamp"].iloc[-1])
                        - pd.Timestamp(df["timestamp"].iloc[0])
                    ).total_seconds(),
                )
                * 100.0
            ),
            "max_simultaneous_positions": int(intraday["active_positions"].max()),
            "same_bar_exit_trades": int(same_bar_mask.sum()),
            "same_bar_exit_pct": float(same_bar_mask.mean() * 100.0),
            "same_bar_both_sl_tp_trades": int(same_bar_both_hit.sum()),
        },
        "margin": {
            "model": profile_config["margin_model"],
            "unit": "percent of equity per 1 percent risk while position is open",
            "effective_leverage_scenarios": _margin_summary(enriched_trades, margin_leverages),
        },
        "intraday_equity": {
            "model": f"{dataset_timeframe} conservative OHLC envelope; fixed R costs recognized at entry",
            "reset_timezones": daily_summary,
        },
        "risk_scenarios_pct": list(profile_config["risk_scenarios_pct"]),
        "limitations": [
            f"{dataset_timeframe} OHLC cannot identify the true intrabar high/low order.",
            "Margin uses effective-leverage scenarios, not historical broker order_calc_margin snapshots.",
            "Execution cost is the configured R deduction; spread, swap, and slippage are not reconstructed unless supplied by the backtest.",
            "Shuffle Monte Carlo changes trade order but does not model regime persistence or cross-strategy dependence.",
        ],
    }
    if same_bar_mask.mean() >= 0.20:
        summary["limitations"].append(
            f"{same_bar_mask.mean() * 100.0:.2f}% of trades exit on the entry {dataset_timeframe} bar; "
            "a lower-timeframe replay can materially change trigger-bar path results."
        )
    return {
        "summary": summary,
        "trades": enriched_trades,
        "intraday_equity": intraday,
        "daily_equity": daily,
        "monthly_returns": monthly,
        "equity_curve": trade_equity,
        "monte_carlo_runs": mc_runs,
        "raw_equity": list(equity),
    }
