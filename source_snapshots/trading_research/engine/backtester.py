"""Causal OHLC backtester with explicit, conservative execution semantics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import time as clock_time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from engine.execution_cost_model import resolve_execution_costs


VALID_EXECUTION_MODES = {"next_bar", "open_bar"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_report_dir(report_dir):
    output = Path(report_dir)
    return output if output.is_absolute() else PROJECT_ROOT / output


@dataclass(frozen=True)
class CostContext:
    """Context passed to a callable execution-cost hook.

    ``spread`` and ``slippage`` hooks return absolute price amounts. ``commission``
    and ``swap`` hooks return R amounts deducted when a trade closes.
    """

    bar_index: int
    timestamp: Any
    side: str
    event: str
    price: float
    position: Mapping[str, Any]


def compute_metrics(trades, equity):
    total_trades = len(trades)
    is_break_even = [
        trade.get("close_reason") == "break_even" or (
            "close_reason" not in trade and trade["R"] == 0
        )
        for trade in trades
    ]
    wins = sum(1 for trade, is_be in zip(trades, is_break_even) if not is_be and trade["R"] > 0)
    losses = sum(1 for trade, is_be in zip(trades, is_break_even) if not is_be and trade["R"] < 0)
    be_trades = sum(is_break_even)
    raw_closed = wins + losses
    trade_returns = np.array([trade["R"] for trade in trades], dtype=float)
    peak = equity[0] if equity else 0.0
    max_dd = 0.0

    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    gross_profit = sum(trade["R"] for trade in trades if trade["R"] > 0)
    gross_loss = abs(sum(trade["R"] for trade in trades if trade["R"] < 0))
    wins_list = [trade["R"] for trade, is_be in zip(trades, is_break_even) if not is_be and trade["R"] > 0]
    losses_list = [trade["R"] for trade, is_be in zip(trades, is_break_even) if not is_be and trade["R"] < 0]
    cost_values = np.array(
        [trade.get("total_costs_r", trade.get("costs_r", 0.0)) for trade in trades],
        dtype=float,
    )
    component_names = (
        "spread_cost_r", "slippage_cost_r",
        "commission_cost_r", "swap_cost_r",
    )
    cost_components = {
        name: float(np.mean([trade.get(name, 0.0) for trade in trades]))
        if trades else 0.0
        for name in component_names
    }

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "be_trades": be_trades,
        "winrate": 0.0 if total_trades == 0 else wins / total_trades * 100,
        "winrate_no_be": 0.0 if raw_closed == 0 else wins / raw_closed * 100,
        "net_r": float(trade_returns.sum()) if len(trade_returns) else 0.0,
        "max_drawdown": max_dd,
        "profit_factor": gross_profit if gross_loss == 0 else gross_profit / gross_loss,
        "expectancy": float(np.mean(trade_returns)) if len(trade_returns) else 0.0,
        "avg_win": float(np.mean(wins_list)) if wins_list else 0.0,
        "avg_loss": float(np.mean(losses_list)) if losses_list else 0.0,
        "best_trade": float(np.max(trade_returns)) if len(trade_returns) else 0.0,
        "worst_trade": float(np.min(trade_returns)) if len(trade_returns) else 0.0,
        "execution_costs": {
            "average_r": float(np.mean(cost_values)) if len(cost_values) else 0.0,
            "median_r": float(np.median(cost_values)) if len(cost_values) else 0.0,
            "p90_r": float(np.quantile(cost_values, 0.90)) if len(cost_values) else 0.0,
            "total_r": float(np.sum(cost_values)) if len(cost_values) else 0.0,
            "average_components_r": cost_components,
            "average_rollover_units": float(np.mean([
                trade.get("swap_rollover_units", 0) for trade in trades
            ])) if trades else 0.0,
        },
    }


def plot_equity(trades, equity, report_dir="reports/backtest"):
    if not trades:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = [pd.Timestamp(trade["close_time"]) for trade in trades]
    plt.style.use("dark_background")
    plt.figure(figsize=(14, 6))
    plt.plot(dates, equity[1:])
    plt.title("Equity Curve")
    plt.xlabel("Date")
    plt.ylabel("R")
    plt.grid(True, axis="x")
    plt.tight_layout()
    output = _resolve_report_dir(report_dir)
    output.mkdir(parents=True, exist_ok=True)
    plt.savefig(output / "equity_curve.png", dpi=150)
    plt.close()


def plot_monthly_returns(trades, report_dir="reports/backtest"):
    if not trades:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    monthly = {}
    for trade in trades:
        month = pd.Timestamp(trade["close_time"]).strftime("%Y-%m")
        monthly[month] = monthly.get(month, 0.0) + trade["R"]
    plt.style.use("dark_background")
    plt.figure(figsize=(16, 5))
    plt.bar(list(monthly), list(monthly.values()))
    plt.xticks(rotation=90)
    plt.title("Monthly Returns (R)")
    plt.grid(True, axis="y")
    plt.tight_layout()
    output = _resolve_report_dir(report_dir)
    output.mkdir(parents=True, exist_ok=True)
    plt.savefig(output / "monthly_returns.png", dpi=150)
    plt.close()


def print_report(metrics):
    print("\n========== BACKTEST RESULTS ==========")
    print(f"Trades:              {metrics['total_trades']}")
    print(f"Wins:                {metrics['wins']}")
    print(f"Losses:              {metrics['losses']}")
    print(f"BE Trades:           {metrics['be_trades']}")
    print()
    print(f"Winrate (raw):       {metrics['winrate']:.2f}%")
    print(f"Winrate without BE:  {metrics['winrate_no_be']:.2f}%")
    print()
    print(f"Net R:               {metrics['net_r']:.2f}")
    print(f"Max DD:              {metrics['max_drawdown']:.2f}R")
    print(f"Profit Factor:       {metrics['profit_factor']:.2f}")
    print(f"Expectancy:          {metrics['expectancy']:.3f}")
    print()
    print(f"Avg Win:             {metrics['avg_win']:.2f}")
    print(f"Avg Loss:            {metrics['avg_loss']:.2f}")
    print(f"Best Trade:          {metrics['best_trade']:.2f}")
    print(f"Worst Trade:         {metrics['worst_trade']:.2f}")
    costs = metrics.get("execution_costs")
    if costs:
        components = costs["average_components_r"]
        print()
        print(f"Avg execution cost:   {costs['average_r']:.3f}R")
        print(f"Median execution cost:{costs['median_r']:>8.3f}R")
        print(f"P90 execution cost:   {costs['p90_r']:.3f}R")
        print(
            "Avg cost components:  "
            f"spread {components['spread_cost_r']:.3f}R | "
            f"slippage {components['slippage_cost_r']:.3f}R | "
            f"commission {components['commission_cost_r']:.3f}R | "
            f"swap {components['swap_cost_r']:.3f}R"
        )
        profile = metrics.get("execution_cost_profile")
        if profile:
            print(
                f"Cost profile:         {profile['symbol']} / {profile['profile']} "
                f"({profile['unit_name']}={profile['price_unit']})"
            )
            print(
                f"Avg initial stop:     {profile['average_initial_risk_units']:.2f} "
                f"{profile['unit_name']}"
            )
            print(f"Avg rollover units:   {costs['average_rollover_units']:.2f}")
    print("======================================")


def _cost_value(costs: Mapping[str, Any], name: str, context: CostContext) -> float:
    value = costs.get(name, 0.0)
    if callable(value):
        value = value(context)
    return float(value)


def _adverse_fill(price: float, side: str, event: str, costs: Mapping[str, Any], context: CostContext):
    spread = _cost_value(costs, "spread", context)
    slippage = _cost_value(costs, "slippage", context)
    direction = 1.0 if side == "BUY" else -1.0
    adverse_direction = direction if event == "entry" else -direction
    return (
        float(price + adverse_direction * (spread / 2.0 + slippage)),
        spread / 2.0,
        slippage,
    )


def _unmanaged_frame_reference(df: pd.DataFrame) -> dict[str, Any]:
    """Keep synthetic tests traceable without claiming DataManager provenance."""
    digest = sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()
    return {"manifest": {"data_kind": "in_memory", "sha256": digest}, "split": None}


def _dataset_reference(df: pd.DataFrame, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    return deepcopy(supplied or df.attrs.get("dataset_reference") or _unmanaged_frame_reference(df))


def _exit_for_bar(position: Mapping[str, Any], open_price: float, high: float, low: float, current_bar: int):
    """Return a conservative OHLC exit: gap SL first, then same-bar SL over TP."""
    side, sl, tp = position["side"], position["sl"], position["tp"]
    has_sl = sl is not None
    has_tp = tp is not None
    if position["fill_timing"] == "same_bar_trigger" and position["open_bar"] == current_bar:
        # The trigger can occur anywhere within the candle. Treat its full OHLC
        # range as post-entry pessimistically, but never price a pre-trigger open
        # as a gap: an entry at the trigger can lose at most to its configured SL.
        high, low = max(open_price, high), min(open_price, low)
        if side == "BUY":
            sl_hit, tp_hit = has_sl and low <= sl, has_tp and high >= tp
        else:
            sl_hit, tp_hit = has_sl and high >= sl, has_tp and low <= tp
        if sl_hit:
            return sl, "sl_before_tp" if tp_hit else "sl_same_bar_trigger"
        if tp_hit:
            return tp, "tp_same_bar_trigger"
        return None, None

    if side == "BUY":
        if has_sl and open_price <= sl:
            return open_price, "sl_gap"
        if has_tp and open_price >= tp:
            return tp, "tp_gap"
        sl_hit, tp_hit = has_sl and low <= sl, has_tp and high >= tp
    else:
        if has_sl and open_price >= sl:
            return open_price, "sl_gap"
        if has_tp and open_price <= tp:
            return tp, "tp_gap"
        sl_hit, tp_hit = has_sl and high >= sl, has_tp and low <= tp
    if sl_hit:
        return sl, "sl_before_tp" if tp_hit else "sl"
    if tp_hit:
        return tp, "tp"
    return None, None


def _make_position(signal, raw_entry, bar_index, timestamp, costs, *, fill_timing="next_bar_open"):
    side = signal["side"]
    raw_tp = signal.get("tp")
    template = {"side": side, "tp": None if raw_tp is None else float(raw_tp)}
    context = CostContext(bar_index, timestamp, side, "entry", float(raw_entry), template)
    entry, entry_spread, entry_slippage = _adverse_fill(
        float(raw_entry), side, "entry", costs, context,
    )
    direction = 1.0 if side == "BUY" else -1.0
    if "sl_distance" in signal:
        risk = float(signal["sl_distance"])
        template["sl"] = entry - direction * risk
    elif signal.get("sl") is None and "risk_reference" in signal:
        template["sl"] = None
        risk = abs(entry - float(signal["risk_reference"]))
    else:
        template["sl"] = float(signal["sl"])
        if (side == "BUY" and template["sl"] >= entry) or (
            side == "SELL" and template["sl"] <= entry
        ):
            return None
        risk = abs(entry - template["sl"])
    if risk <= 0:
        return None
    return {
        **template,
        "initial_sl": template["sl"],
        "initial_tp": template["tp"],
        "entry": entry,
        "signal_entry": float(signal["entry"]),
        "risk": risk,
        "open_time": timestamp,
        "signal_time": timestamp,
        "open_bar": bar_index,
        "fill_timing": fill_timing,
        "moved_to_be": False,
        "pending_be_move": False,
        "entry_spread_price": entry_spread,
        "entry_slippage_price": entry_slippage,
    }


def _trade_costs(
    position, context, costs, exit_spread, exit_slippage,
):
    risk = position["risk"]
    spread_r = (position["entry_spread_price"] + exit_spread) / risk
    slippage_r = (position["entry_slippage_price"] + exit_slippage) / risk
    commission_r = _cost_value(costs, "commission", context)
    swap_r = _cost_value(costs, "swap", context)
    return {
        "spread_cost_r": spread_r,
        "slippage_cost_r": slippage_r,
        "commission_cost_r": commission_r,
        "swap_cost_r": swap_r,
        # Keep costs_r compatible with strategy_profile: price costs are already
        # embedded in entry/exit fills and must not be subtracted there twice.
        "costs_r": commission_r + swap_r,
        "total_costs_r": (
            spread_r + slippage_r + commission_r + swap_r
        ),
        "fees_r": commission_r + swap_r,
    }


def _attach_cost_profile_metrics(metrics, trades, model):
    if model is None:
        return
    metadata = model.metadata()
    metadata["average_initial_risk_units"] = (
        float(np.mean([
            trade["initial_risk_price"] / model.price_unit for trade in trades
        ])) if trades else 0.0
    )
    metrics["execution_cost_profile"] = metadata


def _can_open(open_positions, pending_orders, daily_r, weekly_r, daily_sl_limit, weekly_sl_limit, max_positions):
    if daily_sl_limit is not None and daily_r <= -abs(daily_sl_limit):
        return False
    if weekly_sl_limit is not None and weekly_r <= -abs(weekly_sl_limit):
        return False
    return len(open_positions) + len(pending_orders) < max_positions


def _parse_friday_close_time(value) -> clock_time:
    if isinstance(value, clock_time):
        return value
    try:
        return clock_time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("friday_close_time_utc must use HH:MM or HH:MM:SS") from exc


def _infer_bar_interval(timestamps) -> pd.Timedelta:
    if len(timestamps) < 2:
        return pd.Timedelta(0)
    values = pd.Series(pd.to_datetime(timestamps, utc=True)).diff().dropna()
    positive = values[values > pd.Timedelta(0)]
    return positive.min() if not positive.empty else pd.Timedelta(0)


def _friday_close_state(timestamp, bar_interval, cutoff, next_timestamp=None):
    ts = pd.Timestamp(timestamp)
    if ts.dayofweek != 4:
        return False, False
    cutoff_ts = ts.normalize() + pd.Timedelta(
        hours=cutoff.hour,
        minutes=cutoff.minute,
        seconds=cutoff.second,
    )
    starts_after_cutoff = ts >= cutoff_ts
    contains_cutoff = ts < cutoff_ts <= ts + bar_interval
    next_ts = pd.Timestamp(next_timestamp) if next_timestamp is not None else None
    last_available_friday_bar = next_ts is None or next_ts.date() != ts.date()
    return starts_after_cutoff or contains_cutoff or last_available_friday_bar, starts_after_cutoff


def _replay_bar_ranges(signal_timestamps, execution_timestamps):
    """Map every signal candle to its contained lower-timeframe bar range."""
    signal_index = pd.DatetimeIndex(pd.to_datetime(signal_timestamps, utc=True))
    execution_index = pd.DatetimeIndex(pd.to_datetime(execution_timestamps, utc=True))
    if not signal_index.is_monotonic_increasing or not execution_index.is_monotonic_increasing:
        raise ValueError("Signal and execution timestamps must be increasing")
    interval = _infer_bar_interval(signal_index)
    if interval <= pd.Timedelta(0):
        raise ValueError("Execution replay requires at least two signal bars")
    boundaries = signal_index.append(pd.DatetimeIndex([signal_index[-1] + interval]))
    starts = execution_index.searchsorted(boundaries[:-1], side="left")
    ends = execution_index.searchsorted(boundaries[1:], side="left")
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def _execution_history_is_complete(signal, execution_index, execution_interval):
    """Validate an optional signal-declared, already closed execution window."""
    start = signal.get("execution_history_start")
    end = signal.get("execution_history_end")
    if start is None and end is None:
        return True
    if start is None or end is None:
        raise ValueError("Signals must provide both execution_history_start and execution_history_end")
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")
    if end <= start:
        raise ValueError("Signal execution history window must have positive duration")
    expected = pd.date_range(start, end, freq=execution_interval, inclusive="left")
    first = execution_index.searchsorted(start, side="left")
    actual = execution_index[first:first + len(expected)]
    return len(actual) == len(expected) and actual.equals(expected)


def _is_weekly_market_open_delay(expected, actual, signal_interval):
    """Allow a weekly boundary to precede the first tradable replay bar."""
    expected, actual = pd.Timestamp(expected), pd.Timestamp(actual)
    delay = actual - expected
    return (
        # DST-aligned weekly datasets can contain a 167-hour boundary gap.
        signal_interval >= pd.Timedelta(days=6)
        and expected.dayofweek >= 5
        and pd.Timedelta(0) < delay <= pd.Timedelta(days=3)
        and actual.dayofweek in {6, 0}
    )


def _run_backtest_replay(
    signal_df,
    execution_df,
    strategy,
    *,
    stats_only,
    use_break_even,
    break_even_trigger,
    break_even_offset,
    daily_sl_limit,
    weekly_sl_limit,
    max_simultaneous_positions,
    collect_equity,
    execution_mode,
    close_positions_on_friday,
    friday_close_time_utc,
    warmup_bars,
    plot,
    report_dir,
    execution_costs,
    execution_cost_model,
    dataset_reference,
    replay_entry_bar_offset,
    replay_exit_bar_offset,
):
    """Execute native-timeframe strategy signals on lower-timeframe OHLC bars."""
    if replay_entry_bar_offset < 0 or replay_exit_bar_offset < 0:
        raise ValueError("Replay entry/exit bar offsets must be non-negative")
    required = {"timestamp", "open", "high", "low", "close"}
    for label, frame in (("Signal", signal_df), ("Execution replay", execution_df)):
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{label} data missing required columns: {sorted(missing)}")

    signal_timestamps = signal_df["timestamp"].to_numpy()
    timestamps = execution_df["timestamp"].to_numpy()
    execution_index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    ranges = _replay_bar_ranges(signal_timestamps, timestamps)
    if not any(end > start for start, end in ranges):
        raise ValueError("Execution replay data does not overlap signal data")

    opens = execution_df["open"].to_numpy(dtype=float)
    highs = execution_df["high"].to_numpy(dtype=float)
    lows = execution_df["low"].to_numpy(dtype=float)
    closes = execution_df["close"].to_numpy(dtype=float)
    bar_interval = _infer_bar_interval(timestamps)
    signal_interval = _infer_bar_interval(signal_timestamps)
    signal_reference = _dataset_reference(signal_df, dataset_reference)
    execution_reference = _dataset_reference(execution_df, None)
    symbol = signal_reference.get("manifest", {}).get("symbol")
    costs, cost_model = resolve_execution_costs(
        execution_cost_model, symbol, execution_costs,
    )
    friday_cutoff = _parse_friday_close_time(friday_close_time_utc)

    strategy.bind_data(signal_df)
    scheduled = {}
    exit_bars = {}
    skipped_history_incomplete = 0
    skipped_entry_bar_unavailable = 0
    for native_i in range(warmup_bars, len(signal_df)):
        start, end = ranges[native_i]
        # A last native candle has no observed closing boundary. It may still
        # provide intrabar SL/TP execution, but a time exit cannot be claimed.
        if native_i + 1 < len(signal_df) and end - start > replay_exit_bar_offset:
            exit_bars[end - 1 - replay_exit_bar_offset] = native_i
        signal = strategy.on_bar(native_i, signal_df)
        if signal is None:
            continue
        # This check reads only the session window declared by the signal as
        # completed before entry; it never rejects a trade using future London
        # bars, so it cannot introduce lookahead bias.
        if not _execution_history_is_complete(signal, execution_index, bar_interval):
            skipped_history_incomplete += 1
            continue
        target_native_i = native_i if execution_mode == "open_bar" else native_i + 1
        if target_native_i >= len(ranges):
            continue
        target_start, target_end = ranges[target_native_i]
        first = target_start + replay_entry_bar_offset
        if first >= target_end:
            continue
        if execution_mode == "next_bar":
            expected_entry_time = (
                pd.Timestamp(signal_timestamps[target_native_i])
                + replay_entry_bar_offset * bar_interval
            )
            actual_entry_time = pd.Timestamp(timestamps[first])
            if (
                actual_entry_time != expected_entry_time
                and not _is_weekly_market_open_delay(
                    expected_entry_time, actual_entry_time, signal_interval,
                )
            ):
                skipped_entry_bar_unavailable += 1
                continue
        fill_bar = first
        fill_timing = "next_bar_open"
        if execution_mode == "open_bar":
            entry = float(signal["entry"])
            trigger = signal.get("entry_trigger", "price_touched")
            if trigger == "price_at_or_above":
                hit = highs[first:target_end] >= entry
            elif trigger == "price_at_or_below":
                hit = lows[first:target_end] <= entry
            elif trigger == "price_touched":
                hit = (lows[first:target_end] <= entry) & (highs[first:target_end] >= entry)
            else:
                raise ValueError(f"Unsupported entry_trigger: {trigger!r}")
            candidates = np.flatnonzero(hit)
            if not len(candidates):
                continue
            fill_bar = first + int(candidates[0])
            fill_timing = "same_bar_trigger"
        scheduled.setdefault(fill_bar, []).append({
            "signal": signal,
            "signal_time": signal_timestamps[native_i],
            "native_open_bar": target_native_i,
            "fill_timing": fill_timing,
        })

    open_positions, trades, equity = [], [], [0.0]
    daily_r = weekly_r = 0.0
    current_day = current_week = None
    for i, timestamp in enumerate(timestamps):
        ts = pd.Timestamp(timestamp)
        next_timestamp = timestamps[i + 1] if i + 1 < len(timestamps) else None
        friday_close_bar, friday_after_cutoff = _friday_close_state(
            timestamp, bar_interval, friday_cutoff, next_timestamp
        )
        friday_close_bar = close_positions_on_friday and friday_close_bar
        friday_after_cutoff = close_positions_on_friday and friday_after_cutoff
        if current_day != ts.date():
            current_day, daily_r = ts.date(), 0.0
        iso_week = (ts.isocalendar().year, ts.isocalendar().week)
        if current_week != iso_week:
            current_week, weekly_r = iso_week, 0.0

        for order in (() if friday_after_cutoff else scheduled.get(i, ())):
            if not _can_open(
                open_positions, [], daily_r, weekly_r, daily_sl_limit,
                weekly_sl_limit, max_simultaneous_positions,
            ):
                continue
            raw_entry = order["signal"]["entry"] if execution_mode == "open_bar" else opens[i]
            position = _make_position(
                order["signal"], raw_entry, i, timestamp, costs,
                fill_timing=order["fill_timing"],
            )
            if position is not None:
                position["signal_time"] = order["signal_time"]
                position["native_open_bar"] = order["native_open_bar"]
                open_positions.append(position)

        surviving = []
        for position in open_positions:
            if position["pending_be_move"]:
                direction = 1.0 if position["side"] == "BUY" else -1.0
                position["sl"] = position["entry"] + direction * position["risk"] * break_even_offset
                position["moved_to_be"] = True
                position["pending_be_move"] = False

            raw_exit, close_reason = _exit_for_bar(position, opens[i], highs[i], lows[i], i)
            native_i = exit_bars.get(i)
            strategy_exit = getattr(strategy, "should_exit", None)
            if raw_exit is None and native_i is not None and callable(strategy_exit):
                proxy = dict(position)
                proxy["open_bar"] = position["native_open_bar"]
                if strategy_exit(native_i, proxy, signal_df):
                    raw_exit, close_reason = closes[i], "strategy_exit"
            if raw_exit is None and friday_close_bar:
                raw_exit, close_reason = closes[i], "friday_close"
            if raw_exit is None:
                if use_break_even and not position["moved_to_be"]:
                    move = (
                        (highs[i] - position["entry"]) / position["risk"]
                        if position["side"] == "BUY"
                        else (position["entry"] - lows[i]) / position["risk"]
                    )
                    if move >= break_even_trigger:
                        position["pending_be_move"] = True
                surviving.append(position)
                continue

            if position["moved_to_be"] and raw_exit == position["sl"] and not close_reason.endswith("gap"):
                close_reason = "break_even"
            context = CostContext(i, timestamp, position["side"], "exit", raw_exit, position)
            exit_price, exit_spread, exit_slippage = _adverse_fill(
                raw_exit, position["side"], "exit", costs, context,
            )
            result_r = (
                (exit_price - position["entry"]) / position["risk"]
                if position["side"] == "BUY"
                else (position["entry"] - exit_price) / position["risk"]
            )
            trade_costs = _trade_costs(
                position, context, costs, exit_spread, exit_slippage,
            )
            result_r -= trade_costs["fees_r"]
            trades.append({
                "side": position["side"], "open_time": position["open_time"],
                "signal_time": position["signal_time"], "close_time": timestamp,
                "entry": position["entry"], "exit": exit_price, "R": result_r,
                "close_reason": close_reason, **trade_costs,
                "signal_entry": position["signal_entry"],
                "initial_sl": position["initial_sl"], "initial_tp": position["initial_tp"],
                "initial_risk_price": position["risk"], "open_bar": position["open_bar"],
                "close_bar": i, "duration_bars": i - position["open_bar"] + 1,
                "fill_timing": position["fill_timing"],
                "swap_rollover_units": (
                    cost_model.rollover_units(context) if cost_model else 0
                ),
            })
            equity.append(equity[-1] + result_r)
            daily_r += result_r
            weekly_r += result_r
        open_positions = surviving

    metrics = compute_metrics(trades, equity)
    _attach_cost_profile_metrics(metrics, trades, cost_model)
    metrics["dataset_reference"] = execution_reference
    metrics["signal_dataset_reference"] = signal_reference
    metrics["execution"] = {
        "mode": execution_mode,
        "replay_enabled": True,
        "signal_timeframe": signal_reference.get("manifest", {}).get("timeframe"),
        "execution_timeframe": execution_reference.get("manifest", {}).get("timeframe"),
        "entry_bar_offset": replay_entry_bar_offset,
        "exit_bar_offset": replay_exit_bar_offset,
        "skipped_history_incomplete": skipped_history_incomplete,
        "skipped_entry_bar_unavailable": skipped_entry_bar_unavailable,
        "fill_timing": "lower_timeframe_first_touch" if execution_mode == "open_bar" else "lower_timeframe_open",
        "same_bar_sl_tp_policy": "stop_loss_first",
        "gap_stop_policy": "fill_at_open",
        "break_even_policy": "move_stop_on_next_execution_bar_after_trigger",
        "friday_close": {
            "enabled": bool(close_positions_on_friday),
            "time_utc": friday_cutoff.isoformat(timespec="minutes"),
            "price_policy": "close_of_execution_bar_containing_cutoff",
        },
        "cost_contract": "spread/slippage price hooks; commission/swap R hooks",
    }
    if collect_equity:
        return trades, equity, metrics
    if stats_only:
        return metrics
    print_report(metrics)
    if plot:
        plot_equity(trades, equity, report_dir)
        plot_monthly_returns(trades, report_dir)
    return trades, equity, metrics


def run_backtest(
    df,
    strategy,
    stats_only=False,
    use_break_even=False,
    break_even_trigger=1.0,
    break_even_offset=0.0,
    daily_sl_limit=None,
    weekly_sl_limit=None,
    max_simultaneous_positions=1,
    collect_equity=False,
    execution_mode="next_bar",
    close_positions_on_friday=False,
    friday_close_time_utc="22:00",
    *,
    warmup_bars=0,
    allow_same_bar_entry=None,
    plot=False,
    report_dir="reports/backtest",
    execution_costs: Mapping[str, float | Callable[[CostContext], float]] | None = None,
    execution_cost_model: Mapping[str, Any] | None = None,
    dataset_reference: Mapping[str, Any] | None = None,
    execution_replay_df: pd.DataFrame | None = None,
    replay_entry_bar_offset: int = 0,
    replay_exit_bar_offset: int = 0,
):
    """Run a causal OHLC backtest.

    ``next_bar`` evaluates a signal after bar close and fills it at the following
    bar open. ``open_bar`` approximates an intrabar trigger: it fills at
    ``signal['entry']`` and evaluates the whole triggering OHLC candle with a
    pessimistic stop-loss-first policy. The execution mode itself is the opt-in;
    ``allow_same_bar_entry`` remains only as an ignored legacy keyword. General
    Pass ``execution_replay_df`` to retain native-timeframe signals while fills
    and position management run on lower-timeframe OHLC data.
    """
    if execution_mode not in VALID_EXECUTION_MODES:
        raise ValueError("execution_mode must be 'next_bar' or explicitly allowed 'open_bar'; intrabar OHLC fills are unsupported")
    if not isinstance(warmup_bars, int) or warmup_bars < 0:
        raise ValueError("warmup_bars must be a non-negative integer")
    if max_simultaneous_positions < 1:
        raise ValueError("max_simultaneous_positions must be at least one")
    if execution_replay_df is not None:
        return _run_backtest_replay(
            df, execution_replay_df, strategy,
            stats_only=stats_only,
            use_break_even=use_break_even,
            break_even_trigger=break_even_trigger,
            break_even_offset=break_even_offset,
            daily_sl_limit=daily_sl_limit,
            weekly_sl_limit=weekly_sl_limit,
            max_simultaneous_positions=max_simultaneous_positions,
            collect_equity=collect_equity,
            execution_mode=execution_mode,
            close_positions_on_friday=close_positions_on_friday,
            friday_close_time_utc=friday_close_time_utc,
            warmup_bars=warmup_bars,
            plot=plot,
            report_dir=report_dir,
            execution_costs=execution_costs,
            execution_cost_model=execution_cost_model,
            dataset_reference=dataset_reference,
            replay_entry_bar_offset=int(replay_entry_bar_offset),
            replay_exit_bar_offset=int(replay_exit_bar_offset),
        )
    friday_cutoff = _parse_friday_close_time(friday_close_time_utc)
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Backtest data missing required columns: {sorted(missing)}")

    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    timestamps = df["timestamp"].to_numpy()
    bar_interval = _infer_bar_interval(timestamps)
    reference = _dataset_reference(df, dataset_reference)
    symbol = reference.get("manifest", {}).get("symbol")
    costs, cost_model = resolve_execution_costs(
        execution_cost_model, symbol, execution_costs,
    )
    strategy.bind_data(df)
    open_positions, pending_orders, trades, equity = [], [], [], [0.0]
    daily_r = weekly_r = 0.0
    current_day = current_week = None

    for i in range(warmup_bars, len(df)):
        timestamp = timestamps[i]
        ts = pd.Timestamp(timestamp)
        next_timestamp = timestamps[i + 1] if i + 1 < len(timestamps) else None
        friday_close_bar, friday_after_cutoff = _friday_close_state(
            timestamp, bar_interval, friday_cutoff, next_timestamp
        )
        friday_close_bar = close_positions_on_friday and friday_close_bar
        friday_after_cutoff = close_positions_on_friday and friday_after_cutoff
        if current_day != ts.date():
            current_day, daily_r = ts.date(), 0.0
        if current_week != ts.isocalendar().week:
            current_week, weekly_r = ts.isocalendar().week, 0.0

        due_orders = [order for order in pending_orders if order["fill_bar"] == i]
        pending_orders = [order for order in pending_orders if order["fill_bar"] != i]
        for order in (() if friday_after_cutoff else due_orders):
            position = _make_position(order["signal"], opens[i], i, timestamp, costs)
            if position is not None:
                position["signal_time"] = order["signal_time"]
                open_positions.append(position)

        if execution_mode == "open_bar" and not friday_close_bar and _can_open(
            open_positions, pending_orders, daily_r, weekly_r, daily_sl_limit, weekly_sl_limit, max_simultaneous_positions
        ):
            signal = strategy.on_bar(i, df)
            if signal is not None:
                position = _make_position(
                    signal,
                    signal["entry"],
                    i,
                    timestamp,
                    costs,
                    fill_timing="same_bar_trigger",
                )
                if position is not None:
                    open_positions.append(position)

        surviving = []
        for position in open_positions:
            if position["pending_be_move"]:
                direction = 1.0 if position["side"] == "BUY" else -1.0
                position["sl"] = position["entry"] + direction * position["risk"] * break_even_offset
                position["moved_to_be"] = True
                position["pending_be_move"] = False

            raw_exit, close_reason = _exit_for_bar(position, opens[i], highs[i], lows[i], i)
            strategy_exit = getattr(strategy, "should_exit", None)
            if raw_exit is None and callable(strategy_exit) and strategy_exit(i, position, df):
                raw_exit, close_reason = closes[i], "strategy_exit"
            if raw_exit is None and friday_close_bar:
                raw_exit, close_reason = closes[i], "friday_close"
            if raw_exit is None:
                if use_break_even and not position["moved_to_be"]:
                    move = (highs[i] - position["entry"]) / position["risk"] if position["side"] == "BUY" else (position["entry"] - lows[i]) / position["risk"]
                    if move >= break_even_trigger:
                        position["pending_be_move"] = True
                surviving.append(position)
                continue

            if position["moved_to_be"] and raw_exit == position["sl"] and not close_reason.endswith("gap"):
                close_reason = "break_even"

            context = CostContext(i, timestamp, position["side"], "exit", raw_exit, position)
            exit_price, exit_spread, exit_slippage = _adverse_fill(
                raw_exit, position["side"], "exit", costs, context,
            )
            if position["side"] == "BUY":
                result_r = (exit_price - position["entry"]) / position["risk"]
            else:
                result_r = (position["entry"] - exit_price) / position["risk"]
            trade_costs = _trade_costs(
                position, context, costs, exit_spread, exit_slippage,
            )
            result_r -= trade_costs["fees_r"]
            trades.append({
                "side": position["side"], "open_time": position["open_time"], "signal_time": position["signal_time"],
                "close_time": timestamp, "entry": position["entry"], "exit": exit_price,
                "R": result_r, "close_reason": close_reason, **trade_costs,
                "signal_entry": position["signal_entry"],
                "initial_sl": position["initial_sl"], "initial_tp": position["initial_tp"],
                "initial_risk_price": position["risk"],
                "open_bar": position["open_bar"], "close_bar": i,
                "duration_bars": i - position["open_bar"] + 1,
                "fill_timing": position["fill_timing"],
                "swap_rollover_units": (
                    cost_model.rollover_units(context) if cost_model else 0
                ),
            })
            equity.append(equity[-1] + result_r)
            daily_r += result_r
            weekly_r += result_r
        open_positions = surviving

        if execution_mode == "next_bar" and not friday_close_bar and i + 1 < len(df) and _can_open(
            open_positions, pending_orders, daily_r, weekly_r, daily_sl_limit, weekly_sl_limit, max_simultaneous_positions
        ):
            signal = strategy.on_bar(i, df)
            if signal is not None:
                pending_orders.append({"signal": signal, "signal_time": timestamp, "fill_bar": i + 1})

    metrics = compute_metrics(trades, equity)
    _attach_cost_profile_metrics(metrics, trades, cost_model)
    metrics["dataset_reference"] = reference
    metrics["execution"] = {
        "mode": execution_mode,
        "signal_timing": "bar_close" if execution_mode == "next_bar" else "intrabar_trigger_approximation",
        "fill_timing": "next_bar_open" if execution_mode == "next_bar" else "same_bar_trigger",
        "same_bar_trigger_management": "pessimistic_ohlc_stop_loss_first" if execution_mode == "open_bar" else "not_applicable",
        "same_bar_sl_tp_policy": "stop_loss_first",
        "gap_stop_policy": "fill_at_open",
        "break_even_policy": "move_stop_on_next_bar_after_trigger",
        "friday_close": {
            "enabled": bool(close_positions_on_friday),
            "time_utc": friday_cutoff.isoformat(timespec="minutes"),
            "price_policy": "close_of_bar_containing_cutoff",
        },
        "cost_contract": "spread/slippage price hooks; commission/swap R hooks",
    }

    if collect_equity:
        return trades, equity, metrics
    if stats_only:
        return metrics
    print_report(metrics)
    if plot:
        plot_equity(trades, equity, report_dir)
        plot_monthly_returns(trades, report_dir)
    return trades, equity, metrics


def run_backtest_equity_only(df, strategy, **kwargs):
    trades, equity, metrics = run_backtest(df, strategy, collect_equity=True, **kwargs)
    return [pd.Timestamp(trade["close_time"]) for trade in trades], equity[1:], metrics
