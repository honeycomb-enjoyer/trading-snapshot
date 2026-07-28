GLOBAL_DF = None
GLOBAL_STRATEGY = None
GLOBAL_SCORING_CONFIG = None
GLOBAL_EXECUTION_REPLAY_DF = None
GLOBAL_REPLAY_KWARGS = None


def init_worker(
    df,
    strategy_class,
    scoring_config=None,
    execution_replay_df=None,
    replay_kwargs=None,
):
    global GLOBAL_DF
    global GLOBAL_STRATEGY
    global GLOBAL_SCORING_CONFIG
    global GLOBAL_EXECUTION_REPLAY_DF
    global GLOBAL_REPLAY_KWARGS

    GLOBAL_DF = df
    GLOBAL_STRATEGY = strategy_class
    GLOBAL_SCORING_CONFIG = scoring_config
    GLOBAL_EXECUTION_REPLAY_DF = execution_replay_df
    GLOBAL_REPLAY_KWARGS = dict(replay_kwargs or {})


from engine.backtester import run_backtest
from optimizer.scorer import score_result


def evaluate_params(combo):
    strategy_params = combo["strategy_params"]
    execution_params = combo["execution_params"]

    strategy = GLOBAL_STRATEGY(**strategy_params)

    metrics = run_backtest(
        GLOBAL_DF,
        strategy,
        stats_only=True,
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
        execution_replay_df=GLOBAL_EXECUTION_REPLAY_DF,
        **GLOBAL_REPLAY_KWARGS,
    )

    score = score_result(metrics, GLOBAL_SCORING_CONFIG)
    valid = score is not None

    return {
        "strategy_params": strategy_params,
        "execution_params": execution_params,
        **metrics,
        "score": score,
        "valid": valid
    }
