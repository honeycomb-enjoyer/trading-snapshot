from optimizer.runner import run_optimizer
from engine.backtester import run_backtest
from engine.precompute import precompute_for_params
import pandas as pd


def evaluate_best(
    df,
    strategy_class,
    param_grid,
    execution_grid,
    workers=3
):
    """
    Full pipeline:
    1. Run optimizer
    2. Take best combination
    3. Rebuild strategy with best params
    4. Run backtest once on best config
    5. Return metrics + equity + dates + params
    """

    df = precompute_for_params(df, param_grid, silent=True)

    # ==========================
    # OPTIMIZE
    # ==========================
    results = run_optimizer(
        df=df,
        strategy_class=strategy_class,
        param_grid=param_grid,
        execution_grid=execution_grid,
        top_n=1,
        workers=workers,
        silent=True
    )

    if not results:
        raise RuntimeError("Optimizer returned no results")

    best = results[0]

    strategy_params = best["strategy_params"]
    execution_params = best["execution_params"]

    # ==========================
    # BUILD BEST STRATEGY
    # ==========================
    strategy = strategy_class(**strategy_params)

    # ==========================
    # FULL BACKTEST
    # ==========================
    trades, equity, metrics = run_backtest(
        df=df,
        strategy=strategy,
        collect_equity=True,
        **execution_params,
    )

    # ==========================
    # BUILD EQUITY DATES
    # equity[0] = initial 0
    # dates start from first closed trade
    # ==========================
    equity_dates = [
        pd.Timestamp(t["close_time"])
        for t in trades
    ]

    return {
        "best_result": best,
        "metrics": metrics,
        "trades": trades,
        "equity": equity[1:],      # remove initial zero
        "equity_dates": equity_dates,
        "strategy_params": strategy_params,
        "execution_params": execution_params
    }
