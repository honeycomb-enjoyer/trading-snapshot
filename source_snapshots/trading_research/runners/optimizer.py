from master_config import OPTIMIZER_CONFIG, STRATEGY_CLASS
from optimizer.runner import run_optimizer
from runners.common import (
    precompute,
    prepare_data,
    prepare_execution_replay_data,
    report_dir,
)


def main():
    manager = prepare_data()
    grid = OPTIMIZER_CONFIG["param_grid"]
    train_df = precompute(manager.get_train(), grid)
    replay_df, replay_kwargs = prepare_execution_replay_data(
        manager, mode="train", signal_df=train_df,
    )

    print("\n========== OPTIMIZATION DATASET ==========")
    print("Optimizing on TRAIN dataset...")
    print(f"Train bars: {len(train_df)}")
    if replay_df is not None:
        print(f"Execution replay bars: {len(replay_df)}")
    print("==========================================")

    print("\n========== STRATEGY PARAM CONFIG ==========")
    for key, value in grid.items():
        print(f"{key}: {value}")
    print("===========================================")

    execution_grid = OPTIMIZER_CONFIG["execution_grid"]
    print("\n========== EXECUTION CONFIG ==========")
    for key, value in execution_grid.items():
        if key == "execution_cost_model" and len(value) == 1:
            model = value[0]
            profiles = ", ".join(model.get("profiles", {}))
            value = [f"enabled={model.get('enabled', False)}, profiles=[{profiles}]"]
        print(f"{key}: {value}")
    print("======================================")

    results = run_optimizer(
        df=train_df,
        strategy_class=STRATEGY_CLASS,
        param_grid=grid,
        execution_grid=execution_grid,
        top_n=OPTIMIZER_CONFIG["top_n"],
        workers=OPTIMIZER_CONFIG["workers"],
        report_dir=report_dir("optimizer"),
        scoring_config=OPTIMIZER_CONFIG["scoring"],
        execution_replay_df=replay_df,
        replay_kwargs=replay_kwargs,
    )
    if not results:
        print("No optimization results.")
        return results

    best = results[0]
    print("\n========== BEST RESULT ==========")
    print(f"Score: {round(best['score'], 2)}")
    print(f"Net R: {round(best['net_r'], 2)}")
    print(f"Profit Factor: {round(best['profit_factor'], 2)}")
    print(f"Max DD: {round(best['max_drawdown'], 2)}")
    print(f"Trades: {best['total_trades']}")
    print(
        "Avg execution cost: "
        f"{best.get('execution_costs', {}).get('average_r', 0.0):.3f}R"
    )
    print("\nBest Strategy Params:")
    for key, value in best["strategy_params"].items():
        print(f"  {key}: {value}")
    print("\nBest Execution Params:")
    for key, value in best["execution_params"].items():
        if key == "execution_cost_model":
            profiles = ", ".join(value.get("profiles", {}))
            value = f"enabled={value.get('enabled', False)}, profiles=[{profiles}]"
        print(f"  {key}: {value}")
    print("=================================")
    return results


if __name__ == "__main__":
    main()
