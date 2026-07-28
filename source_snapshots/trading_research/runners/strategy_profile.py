"""Generate a complete isolated-strategy profile from the active config."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from data.schema import DataContractWarning
from engine.backtester import run_backtest
from master_config import (
    BACKTEST_CONFIG,
    DATA_CONFIG,
    MONTE_CARLO_CONFIG,
    SPLIT_CONFIG,
    STRATEGY_CLASS,
    STRATEGY_PARAMS,
    STRATEGY_PROFILE_CONFIG,
)
from runners.common import (
    precompute,
    prepare_data,
    prepare_execution_replay_data,
    report_dir,
    select_dataset,
)
from strategy_profile.analyzer import build_strategy_profile
from strategy_profile.reporting import write_strategy_profile


def _infer_asset_class(symbol: str) -> str:
    upper = symbol.upper()
    if upper.startswith(("XAU", "XAG")):
        return "metal"
    if upper.startswith(("DE", "GER", "US30", "US500", "NAS", "UK100", "SPX")):
        return "index"
    if len(upper) == 6 and upper.isalpha():
        return "fx"
    return "other"


def _dataset_symbol(metrics) -> str:
    reference = metrics.get("dataset_reference", {})
    manifest = reference.get("manifest", {}) if isinstance(reference, dict) else {}
    manifest_symbol = str(manifest.get("symbol") or "").strip().upper()
    path_symbol = Path(DATA_CONFIG["path"]).stem.split("_", 1)[0].upper()
    if manifest_symbol and manifest_symbol != path_symbol:
        raise ValueError(
            f"Dataset symbol mismatch: manifest={manifest_symbol}, DATA_CONFIG={path_symbol}"
        )
    symbol = manifest_symbol or path_symbol
    if not symbol or not re.fullmatch(r"[A-Z0-9._-]+", symbol):
        raise ValueError(f"Cannot derive a safe symbol from DATA_CONFIG: {symbol!r}")
    return symbol


def _validated_profile_config():
    config = dict(STRATEGY_PROFILE_CONFIG)
    required = {
        "dataset", "margin_model",
        "margin_leverages", "equity_reset_timezones", "risk_scenarios_pct",
    }
    missing = required.difference(config)
    if missing:
        raise ValueError(f"STRATEGY_PROFILE_CONFIG missing: {sorted(missing)}")
    if config["dataset"] != "full":
        raise ValueError("Strategy profile must use the full train + holdout dataset")
    if not config["margin_leverages"] or any(float(value) <= 0 for value in config["margin_leverages"]):
        raise ValueError("margin_leverages must contain positive values")
    if not config["equity_reset_timezones"]:
        raise ValueError("equity_reset_timezones cannot be empty")
    return config


def main() -> Path:
    profile_config = _validated_profile_config()
    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always", DataContractWarning)
        manager = prepare_data()
        native_df = select_dataset(manager, profile_config["dataset"])
        replay_df, replay_kwargs = prepare_execution_replay_data(
            manager, signal_df=native_df,
        )
    data_warnings = [
        str(item.message)
        for item in captured_warnings
        if issubclass(item.category, DataContractWarning)
    ]
    df = precompute(native_df, STRATEGY_PARAMS)
    trades, equity, metrics = run_backtest(
        df=df,
        strategy=STRATEGY_CLASS(**STRATEGY_PARAMS),
        collect_equity=True,
        plot=False,
        execution_replay_df=replay_df,
        **replay_kwargs,
        **BACKTEST_CONFIG,
    )
    symbol = _dataset_symbol(metrics)
    strategy_class = STRATEGY_CLASS.__name__
    strategy_name = f"{strategy_class}_{symbol}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", strategy_name):
        raise ValueError(f"Derived strategy profile name is unsafe: {strategy_name!r}")
    profile_config.update({
        "strategy_name": strategy_name,
        "symbol": symbol,
        "asset_class": _infer_asset_class(symbol),
        "strategy_class": strategy_class,
    })
    profile = build_strategy_profile(
        df=replay_df if replay_df is not None else df,
        trades=trades,
        equity=equity,
        metrics=metrics,
        strategy_params=STRATEGY_PARAMS,
        backtest_config=BACKTEST_CONFIG,
        split_config=SPLIT_CONFIG,
        monte_carlo_config=MONTE_CARLO_CONFIG,
        profile_config=profile_config,
        data_warnings=data_warnings,
    )
    output = report_dir("strategy_profile") / profile_config["strategy_name"]
    summary_path = write_strategy_profile(profile, output)
    print("\n========== STRATEGY PROFILE ==========")
    print(f"Strategy: {profile_config['strategy_name']}")
    print(f"Trades:   {metrics['total_trades']}")
    print(f"Net R:    {metrics['net_r']:.2f}")
    print(f"Max DD:   {metrics['max_drawdown']:.2f}R")
    print(f"Warnings: {len(data_warnings)}")
    print(f"Report:   {summary_path}")
    print("======================================")
    return summary_path


if __name__ == "__main__":
    main()
