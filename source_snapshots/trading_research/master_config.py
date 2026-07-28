"""Single configuration surface for the trading-research workflow.

This package is a cleaned application snapshot, not the full private research
workspace. It keeps the core pipeline and three representative strategies:

- BasicMeanReversion on AUDCAD;
- MeanReversionWithReturnFilter on EURGBP;
- ContinuationBreakout on XAUUSD.
"""

from strategy.continuation_breakout import ContinuationBreakout

# ================= DATA =================
DATA_CONFIG = {
    # No market-data dump is committed with the application package. The path
    # documents the original normalized dataset contract used by the workflow.
    "path": "data/normalized/XAUUSD_H4_20210101_20260723_UTC.csv",
    "gap_policy": "warn",
}

DOWNLOAD_CONFIG = {
    "symbol": "XAUUSD",
    "timeframe": "H4",
    "start": "2021-01-01T00:00:00Z",
    "end": None,
    "save_path": None,
    "venue": "Dukascopy",
    "broker_time_profile": {"name": "utc", "mode": "utc"},
}

SPLIT_CONFIG = {
    "mode": "manual",
    "dates": {
        "train_start": "2021-01-01T00:00:00Z",
        "train_end": "2025-01-01T00:00:00Z",
        "holdout_start": "2025-01-01T00:00:00Z",
        "holdout_end": "2027-01-01T00:00:00Z",
    },
    "ratios": {"train": 0.80, "holdout": 0.20},
}

# ================= STRATEGY =================
STRATEGY_CLASS = ContinuationBreakout

STRATEGY_PARAMS = {
    "lookback": 24,
    "atr_period": 20,
    "sl_atr": 1.25,
    "rr": 1.5,
    "direction": "both",
    "use_return_filter": True,
    "return_filter_timeframe": "W1",
    "return_filter_mode": "continuation",
}

# ================= BACKTEST =================
BACKTEST_DATASET = "train"  # train / holdout / full

BACKTEST_CONFIG = {
    "use_break_even": False,
    "break_even_trigger": 0.0,
    "break_even_offset": 0.0,
    "daily_sl_limit": None,
    "weekly_sl_limit": None,
    "max_simultaneous_positions": 1,
    "execution_mode": "open_bar",
    "close_positions_on_friday": True,
    "friday_close_time_utc": "22:00",
}

# Optional final validation: strategy signals stay on DATA_CONFIG timeframe,
# while entries, protective exits and time exits are replayed on lower-timeframe
# OHLC. Search/robustness workflows remain native-timeframe.
EXECUTION_REPLAY_CONFIG = {
    "enabled": True,
    "path": "data/normalized/XAUUSD_M5_20210101_20260723_UTC.csv",
    "gap_policy": "warn",
    "entry_bar_offset": 0,
    "exit_bar_offset": 0,
}

# ================= OPTIMIZER =================
OPTIMIZER_CONFIG = {
    "param_grid": {
        "lookback": [12, 18, 24, 30],
        "atr_period": [14, 20],
        "sl_atr": [1.0, 1.25, 1.5],
        "rr": [1.25, 1.5, 2.0],
        "direction": ["both"],
        "use_return_filter": [True],
        "return_filter_timeframe": ["W1"],
        "return_filter_mode": ["continuation"],
    },
    "execution_grid": {
        "use_break_even": [False],
        "break_even_trigger": [0.0],
        "break_even_offset": [0.0],
        "daily_sl_limit": [None],
        "weekly_sl_limit": [None],
        "max_simultaneous_positions": [1],
        "execution_mode": ["open_bar"],
        "close_positions_on_friday": [True],
        "friday_close_time_utc": ["22:00"],
    },
    "top_n": 15,
    "workers": 2,
    "scoring": {
        "min_trades": 50,
        "min_profit_factor": None,
        "min_net_r": None,
        "max_drawdown": None,
    },
}

# ================= ROBUSTNESS TESTS =================
MONTE_CARLO_CONFIG = {
    "mode": "shuffle",
    "dataset": "full",
    "simulations": 1000,
    "random_seed": 42,
}

PERMUTATION_CONFIG = {
    "dataset": "train",
    "permutations": 1000,
    "skip_equity_plots": False,
}

WALKFORWARD_CONFIG = {
    "dataset": "train",
    "mode": "rolling",
    "train_window": 1 * 30 * 12,
    "test_window": 1 * 30 * 6,
    "step": 1 * 30 * 6,
    "optimizer_workers": 3,
    "scoring": {
        "min_trades": 20,
        "min_profit_factor": None,
        "min_net_r": None,
        "max_drawdown": None,
    },
}

# ================= REPORTS =================
REPORTS_CONFIG = {
    "root": "reports",
    "backtest": "backtest",
    "optimizer": "optimizer",
    "monte_carlo": "monte_carlo",
    "permutation": "permutation_test",
    "walk_forward": "walk_forward_test",
    "strategy_profile": "strategy_profile",
    "portfolio_profile": "portfolio_profile",
}

STRATEGY_PROFILE_CONFIG = {
    "dataset": "full",
    "margin_model": "linear_price_risk",
    "margin_leverages": [20, 30, 50, 100, 200, 500],
    "equity_reset_timezones": ["UTC", "Europe/Prague", "America/New_York"],
    "risk_scenarios_pct": [0.25, 0.33, 0.5, 0.75, 1.0, 1.5, 2.0],
}

# Portfolio profiles are assembled from existing reports/strategy_profile/*
# artifacts. No strategy is re-backtested here.
PORTFOLIO_PROFILE_CONFIG = {
    "name": "ThreeStrategy_Portfolio_Profile",
    "components": [
        {
            "label": "B",
            "profile": "BasicMeanReversion_AUDCAD",
            "risk_pct": 1.00,
        },
        {
            "label": "E",
            "profile": "MeanReversionWithReturnFilter_EURGBP",
            "risk_pct": 0.75,
        },
        {
            "label": "X",
            "profile": "ContinuationBreakout_XAUUSD",
            "risk_pct": 0.65,
        },
    ],
    "equity_reset_timezones": ["UTC", "Europe/Prague", "Etc/GMT-3"],
    "margin_scenarios": {
        "baseline_1_30": {"default_leverage": 30, "overrides": {}},
        "baseline_1_50": {"default_leverage": 50, "overrides": {}},
        "gold_1_30_fx_1_100": {
            "default_leverage": 100,
            "overrides": {"X": 30},
        },
    },
    "prop_rules": {
        "name": "Generic two-step evaluation",
        "reset_timezone": "Etc/GMT-3",
        "phase_targets_pct": [10.0, 6.0],
        "daily_loss_limit_pct": 4.0,
        "max_loss_limit_pct": 12.0,
        "daily_loss_baseline": "max_opening_balance_opening_equity",
        "target_requires_flat": True,
        "rolling_start_weekday": 0,
        "horizons_days": [365, 730],
    },
    "monte_carlo": {
        "mode": "trade_shuffle",
        "simulations": 1000,
        "random_seed": 42,
    },
}

# ================= EXECUTION COST PROFILES =================
# Baseline retail-FX/CFD assumptions converted into each symbol's price unit.
# These are fixed research inputs, never optimizer dimensions.
EXECUTION_COST_MODEL = {
    "enabled": True,
    "rollover_timezone": "America/New_York",
    "rollover_time": "17:00",
    "triple_swap_weekday": 2,
    "profiles": {
        "CADJPY": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.01,
            "spread_units": 0.48,
            "slippage_units_per_side": 0.05,
            "commission_units_round_turn": 1.10,
            "swap_long_units_per_roll": 0.25,
            "swap_short_units_per_roll": 0.25,
        },
        "USDJPY": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.01,
            "spread_units": 0.60,
            "slippage_units_per_side": 0.05,
            "commission_units_round_turn": 1.05,
            "swap_long_units_per_roll": 0.25,
            "swap_short_units_per_roll": 0.25,
        },
        "GBPUSD": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.0001,
            "spread_units": 0.04,
            "slippage_units_per_side": 0.05,
            "commission_units_round_turn": 0.70,
            "swap_long_units_per_roll": 0.20,
            "swap_short_units_per_roll": 0.20,
        },
        "USDCHF": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.0001,
            "spread_units": 0.09,
            "slippage_units_per_side": 0.05,
            "commission_units_round_turn": 0.60,
            "swap_long_units_per_roll": 0.20,
            "swap_short_units_per_roll": 0.20,
        },
        "EURGBP": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.0001,
            "spread_units": 0.27,
            "slippage_units_per_side": 0.05,
            "commission_units_round_turn": 0.53,
            "swap_long_units_per_roll": 0.20,
            "swap_short_units_per_roll": 0.20,
        },
        "AUDCAD": {
            "name": "retail_fx_raw_spread_baseline",
            "unit_name": "pip",
            "price_unit": 0.0001,
            "spread_units": 0.68,
            "slippage_units_per_side": 0.07,
            "commission_units_round_turn": 0.96,
            "swap_long_units_per_roll": 0.25,
            "swap_short_units_per_roll": 0.25,
        },
        "XAUUSD": {
            "name": "retail_gold_cfd_baseline",
            "unit_name": "cent",
            "price_unit": 0.01,
            "spread_units": 7.0,
            "slippage_units_per_side": 2.0,
            "commission_units_round_turn": 7.0,
            "swap_long_units_per_roll": 40.0,
            "swap_short_units_per_roll": 15.0,
        },
    },
}

BACKTEST_CONFIG["execution_cost_model"] = EXECUTION_COST_MODEL
OPTIMIZER_CONFIG["execution_grid"]["execution_cost_model"] = [EXECUTION_COST_MODEL]
