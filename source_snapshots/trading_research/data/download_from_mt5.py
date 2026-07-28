"""Download MT5 bars as canonical UTC data and write its manifest."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.manifest import build_manifest, write_manifest
from data.schema import DatasetContract, normalize_and_validate, parse_utc, project_root, resolve_project_path
from data.time_normalization import BrokerTimeProfile, normalize_mt5_timestamps


def _mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 package is required only to download MT5 data") from exc
    return mt5


def _timeframe_map(mt5):
    return {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    }


def download_data(
    symbol, timeframe_str, start, end, save_path=None, *,
    venue="broker", broker_time_profile=None,
):
    """Download `[start, end)` in UTC; the end bar is filtered explicitly."""
    mt5 = _mt5()
    timeframe_str = timeframe_str.upper()
    timeframe_map = _timeframe_map(mt5)
    if timeframe_str not in timeframe_map:
        raise ValueError(f"Unsupported timeframe: {timeframe_str}")

    start_utc = parse_utc(start, field_name="start")
    end_utc = parse_utc(end, field_name="end")
    if start_utc >= end_utc:
        raise ValueError("start must be before end")
    time_profile = BrokerTimeProfile.from_mapping(broker_time_profile)

    if not mt5.initialize():
        raise RuntimeError("MT5 initialize failed")
    try:
        # MT5 may treat its upper range bound inclusively; filtering below
        # establishes the contract independently of that implementation detail.
        padding = timedelta(hours=15) if time_profile.mode == "server_wall_clock" else timedelta(0)
        rates = mt5.copy_rates_range(
            symbol, timeframe_map[timeframe_str],
            (start_utc - padding).to_pydatetime(),
            (end_utc + padding).to_pydatetime(),
        )
    finally:
        mt5.shutdown()
    if rates is None:
        raise RuntimeError("No data returned from MT5")

    df = pd.DataFrame(rates)
    if df.empty:
        raise RuntimeError("Downloaded dataframe is empty")
    df["timestamp"] = normalize_mt5_timestamps(df["time"], time_profile)
    contract = DatasetContract(
        symbol=symbol,
        timeframe=timeframe_str,
        source="MetaTrader5",
        venue=venue,
        max_non_trading_gap=(
            timedelta(days=14) if timeframe_str == "W1" else timedelta(hours=72)
        ),
    )
    interval = pd.Timedelta(contract.interval)
    df = df.loc[(df["timestamp"] >= start_utc) & ((df["timestamp"] + interval) <= end_utc), [
        "timestamp", "open", "high", "low", "close", "tick_volume"
    ]]

    df = normalize_and_validate(df, contract)
    if save_path is None:
        suffix = "_UTC" if time_profile.mode == "server_wall_clock" else ""
        filename = f"{symbol}_{timeframe_str}_{start_utc:%Y%m%d}_{end_utc:%Y%m%d}{suffix}.csv"
        save_path = project_root() / "data" / "raw" / "mt5" / filename
    output_path = resolve_project_path(save_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(
        output_path, df, contract, retrieved_at=datetime.now(tz=start_utc.tzinfo),
        extra_metadata={
            "timestamp_source": "mt5_epoch",
            "broker_time_profile": time_profile.metadata(),
            "normalization": "broker_server_wall_clock_to_utc",
        },
    )
    manifest_path = write_manifest(manifest, output_path)
    print(f"Saved dataset: {output_path}")
    print(f"Saved manifest: {manifest_path}")
    print(f"Bars: {len(df)}")
    return df


if __name__ == "__main__":
    raise SystemExit("Run python run_data_manager.py from the trading_research root")
