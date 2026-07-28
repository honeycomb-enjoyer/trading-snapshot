from optimizer.runner import run_optimizer
from engine.backtester import run_backtest
from engine.precompute import precompute_for_params
from runners.common import slice_execution_replay_data


def slice_window_df(df, window):
    train_df = df.iloc[
        window["train_start_idx"]:window["train_end_idx"]
    ].copy()

    test_df = df.iloc[
        window["test_start_idx"]:window["test_end_idx"]
    ].copy()

    return train_df, test_df


def run_single_window(
    df,
    window,
    strategy_class,
    param_grid,
    execution_grid,
    optimizer_workers=3,
    scoring_config=None,
    precompute_fn=None,
    execution_replay_df=None,
    replay_kwargs=None,
):
    """
    Run one WFT window:
    optimize on train
    validate on OOS
    """

    train_df, test_df = slice_window_df(df, window)
    train_replay_df = slice_execution_replay_data(execution_replay_df, train_df)
    test_replay_df = slice_execution_replay_data(execution_replay_df, test_df)
    replay_kwargs = dict(replay_kwargs or {})

    # =====================================
    # PRECOMPUTE FEATURES
    # =====================================

    if precompute_fn is None:
        train_df = precompute_for_params(train_df, param_grid, silent=True)
        test_df = precompute_for_params(test_df, param_grid, silent=True)
    else:
        train_df = precompute_fn(train_df, param_grid)
        test_df = precompute_fn(test_df, param_grid)

    # =====================================
    # OPTIMIZE TRAIN
    # =====================================

    optimization_results = run_optimizer(
        df=train_df,
        strategy_class=strategy_class,
        param_grid=param_grid,
        execution_grid=execution_grid,
        top_n=1,
        workers=optimizer_workers,
        silent=True,
        scoring_config=scoring_config,
        execution_replay_df=train_replay_df,
        replay_kwargs=replay_kwargs,
    )

    print(
        f"Window train results: {len(optimization_results)}"
    )

    if not optimization_results:
        return None

    best = optimization_results[0]

    strategy_params = best["strategy_params"]
    execution_params = best["execution_params"]

    train_pf = best["profit_factor"]

    strategy = strategy_class(**strategy_params)

    # =====================================
    # OOS BACKTEST
    # =====================================

    trades, equity, oos_metrics = run_backtest(
        df=test_df,
        strategy=strategy,
        collect_equity=True,
        use_break_even=execution_params["use_break_even"],
        break_even_trigger=execution_params["break_even_trigger"],
        break_even_offset=execution_params["break_even_offset"],
        daily_sl_limit=execution_params["daily_sl_limit"],
        weekly_sl_limit=execution_params["weekly_sl_limit"],
        execution_cost_model=execution_params.get("execution_cost_model"),
        max_simultaneous_positions=execution_params["max_simultaneous_positions"],
        execution_mode=execution_params["execution_mode"],
        close_positions_on_friday=execution_params.get("close_positions_on_friday", False),
        friday_close_time_utc=execution_params.get("friday_close_time_utc", "22:00"),
        execution_replay_df=test_replay_df,
        **replay_kwargs,
    )

    trade_count = len(trades)

    # Skip useless windows
    if trade_count < 2:
        return None

    raw_oos_pf = oos_metrics["profit_factor"]

    # Cap insane PF spikes
    capped_oos_pf = min(raw_oos_pf, 10)

    oos_net_r = oos_metrics["net_r"]
    oos_dd = oos_metrics["max_drawdown"]

    pf_retention = 0
    if train_pf > 0:
        pf_retention = (capped_oos_pf / train_pf) * 100

    equity_dates = [
        t["close_time"] for t in trades
    ]

    return {
        "window": window,
        "train_pf": train_pf,
        "oos_pf": capped_oos_pf,
        "raw_oos_pf": raw_oos_pf,
        "oos_net_r": oos_net_r,
        "oos_dd": oos_dd,
        "trade_count": trade_count,
        "pf_retention": pf_retention,
        "strategy_params": strategy_params,
        "execution_params": execution_params,
        "oos_equity": equity,
        "oos_equity_dates": equity_dates,
        "trades_count": len(trades)
    }


def print_window_result(result, idx):
    print()
    print(f"========== WINDOW {idx} ==========")

    print(f"Trades:         {result['trade_count']}")
    print(f"Train PF:       {round(result['train_pf'], 3)}")
    print(f"OOS PF:         {round(result['oos_pf'], 3)}", end="")

    if result["raw_oos_pf"] > result["oos_pf"]:
        print(f" (raw={round(result['raw_oos_pf'], 3)})")
    else:
        print()

    print(f"OOS Net R:      {round(result['oos_net_r'], 2)}")
    print(f"OOS Max DD:     {round(result['oos_dd'], 2)}")
    print(f"PF Retention:   {round(result['pf_retention'], 1)}%")

    print("Best Params:")
    for k, v in result["strategy_params"].items():
        print(f"  {k}: {v}")

    print("==============================")
