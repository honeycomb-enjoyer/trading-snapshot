"""Shared data and configuration plumbing for trading-research runners."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from data.data_manager import DataManager
from data.schema import project_root
from engine.precompute import precompute_for_params
from master_config import DATA_CONFIG, EXECUTION_REPLAY_CONFIG, REPORTS_CONFIG, SPLIT_CONFIG


def prepare_data() -> DataManager:
    manager = DataManager(
        DATA_CONFIG,
        split_mode=SPLIT_CONFIG["mode"],
        date_splits=SPLIT_CONFIG["dates"],
        ratio_splits=SPLIT_CONFIG["ratios"],
    )
    manager.prepare()
    return manager


def select_dataset(manager: DataManager, mode: str):
    return manager.select(mode)


def precompute(df, params_or_grid):
    return precompute_for_params(df, params_or_grid, silent=True)


def prepare_execution_replay_data(
    signal_manager: DataManager,
    mode: str = "full",
    signal_df=None,
):
    """Load optional lower-timeframe data for any workflow using replay."""
    config = dict(EXECUTION_REPLAY_CONFIG)
    if not config.pop("enabled", False):
        return None, {}
    entry_offset = int(config.pop("entry_bar_offset", 0))
    exit_offset = int(config.pop("exit_bar_offset", 0))
    manager = DataManager(
        config,
        split_mode=SPLIT_CONFIG["mode"],
        date_splits=SPLIT_CONFIG["dates"],
        ratio_splits=SPLIT_CONFIG["ratios"],
    )
    manager.prepare()
    if manager.contract.symbol.upper() != signal_manager.contract.symbol.upper():
        raise ValueError(
            "Execution replay symbol mismatch: "
            f"signal={signal_manager.contract.symbol}, execution={manager.contract.symbol}"
        )
    if manager.contract.interval >= signal_manager.contract.interval:
        raise ValueError(
            "Execution replay timeframe must be lower than signal timeframe: "
            f"signal={signal_manager.contract.timeframe}, execution={manager.contract.timeframe}"
        )
    replay_df = manager.select("full")
    if signal_df is not None:
        replay_df = slice_execution_replay_data(replay_df, signal_df)
    else:
        replay_df = manager.select(mode)
    return replay_df, {
        "replay_entry_bar_offset": entry_offset,
        "replay_exit_bar_offset": exit_offset,
    }


def slice_execution_replay_data(replay_df, signal_df):
    """Restrict replay OHLC to exactly the native-timeframe window being tested."""
    if replay_df is None:
        return None
    signal_timestamps = pd.to_datetime(signal_df["timestamp"], utc=True)
    if len(signal_timestamps) < 2:
        raise ValueError("Execution replay window requires at least two signal bars")
    interval = signal_timestamps.diff().dropna()
    interval = interval[interval > pd.Timedelta(0)]
    if interval.empty:
        raise ValueError("Signal timestamps must have a positive interval")
    start = signal_timestamps.iloc[0]
    end = signal_timestamps.iloc[-1] + interval.min()
    replay_timestamps = pd.to_datetime(replay_df["timestamp"], utc=True)
    sliced = replay_df.loc[
        (replay_timestamps >= start) & (replay_timestamps < end)
    ].copy()
    if sliced.empty:
        raise ValueError("Execution replay data does not overlap requested signal window")
    sliced.attrs = dict(replay_df.attrs)
    return sliced


def report_dir(name: str) -> Path:
    root = Path(REPORTS_CONFIG["root"])
    if not root.is_absolute():
        root = project_root() / root
    path = root / REPORTS_CONFIG[name]
    path.mkdir(parents=True, exist_ok=True)
    return path
