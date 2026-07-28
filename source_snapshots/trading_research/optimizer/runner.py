import os
import csv
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from optimizer.grid import generate_grid
from optimizer.worker import evaluate_params, init_worker


PARAM_ALIASES = {
    # strategy params
    "lookback": "LB",
    "range_lookback": "LB",
    "swing_window": "SW",
    "atr_period": "ATRP",
    "atr_multiplier": "ATRx",
    "rr": "RR",
    "tp_fraction": "TP",
    "compression_threshold": "COMP",

    # execution params
    "daily_sl_limit": "DSL",
    "weekly_sl_limit": "WSL",
    "use_break_even": "BE",
    "break_even_trigger": "BEtr",
    "break_even_offset": "BEoff",
}


def merge_grids(strategy_grid, execution_grid):
    merged = []

    for strat in strategy_grid:
        for exec_cfg in execution_grid:
            merged.append({
                "strategy_params": strat,
                "execution_params": exec_cfg
            })

    return merged


def _comparable_value(value):
    """Make nested fixed config values hashable for variation detection."""
    if isinstance(value, dict):
        return tuple(sorted((key, _comparable_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_comparable_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_comparable_value(item) for item in value))
    return value


def get_varying_params(results):
    if not results:
        return [], []

    strategy_keys = list(results[0]["strategy_params"].keys())
    execution_keys = list(results[0]["execution_params"].keys())

    varying_strategy = []
    varying_execution = []

    for key in strategy_keys:
        vals = {_comparable_value(r["strategy_params"][key]) for r in results}
        if len(vals) > 1:
            varying_strategy.append(key)

    for key in execution_keys:
        vals = {_comparable_value(r["execution_params"][key]) for r in results}
        if len(vals) > 1:
            varying_execution.append(key)

    return varying_strategy, varying_execution


# ==========================================
# UNIVERSAL TOP TABLE
# ==========================================
def print_top(results, top_n=15):
    if not results:
        print("\nNo valid results.")
        return

    print()
    print(f"========== TOP {min(top_n, len(results))} ==========")

    varying_strategy, varying_execution = get_varying_params(results)

    columns = ["Rank"]

    columns += [
        PARAM_ALIASES.get(k, k)
        for k in varying_strategy
    ]

    columns += [
        PARAM_ALIASES.get(k, k)
        for k in varying_execution
    ]

    columns += [
        "Trades",
        "NetR",
        "DD",
        "PF",
        "AvgCostR",
        "Score"
    ]

    widths = {
        col: max(len(col), 8)
        for col in columns
    }

    header = ""
    for col in columns:
        header += f"{col:<{widths[col]}} "

    print(header)
    print("-" * len(header))

    for rank, r in enumerate(results[:top_n], 1):
        line = f"{rank:<{widths['Rank']}} "

        for key in varying_strategy:
            alias = PARAM_ALIASES.get(key, key)
            val = r["strategy_params"][key]
            line += f"{str(val):<{widths[alias]}} "

        for key in varying_execution:
            alias = PARAM_ALIASES.get(key, key)
            val = r["execution_params"][key]
            line += f"{str(val):<{widths[alias]}} "

        line += f"{r['total_trades']:<{widths['Trades']}} "
        line += f"{round(r['net_r'], 1):<{widths['NetR']}} "
        line += f"{round(r['max_drawdown'], 1):<{widths['DD']}} "
        line += f"{round(r['profit_factor'], 3):<{widths['PF']}} "
        average_cost = r.get("execution_costs", {}).get("average_r", 0.0)
        line += f"{round(average_cost, 3):<{widths['AvgCostR']}} "

        score = r["score"]
        score_str = "N/A" if score is None else round(score, 2)
        line += f"{str(score_str):<{widths['Score']}} "

        print(line)

    print("=" * len(header))


# ==========================================
# PARAM ANALYSIS
# ==========================================
def print_parameter_analysis(results, grid, section_name):
    if not results:
        return

    print()
    print(f"========== {section_name} ANALYSIS ==========")

    for param_name, values in grid.items():
        # Instrument profiles are fixed execution assumptions, not parameters
        # whose optimizer score should be interpreted as an analysis dimension.
        if param_name == "execution_cost_model":
            continue
        print()
        print(param_name.upper())

        stats = []

        for value in values:
            subset = []

            for r in results:
                source = (
                    r["strategy_params"]
                    if param_name in r["strategy_params"]
                    else r["execution_params"]
                )

                if source[param_name] == value:
                    subset.append(r)

            if not subset:
                continue

            scores = [r["score"] for r in subset if r["score"] is not None]

            if not scores:
                continue

            avg_score = sum(scores) / len(scores)
            sorted_scores = sorted(scores)
            n = len(sorted_scores)

            if n % 2 == 1:
                median_score = sorted_scores[n // 2]
            else:
                median_score = (
                    sorted_scores[n // 2 - 1]
                    + sorted_scores[n // 2]
                ) / 2

            best_score = max(scores)

            stats.append({
                "value": value,
                "avg": avg_score,
                "median": median_score,
                "best": best_score
            })

        if not stats:
            continue

        winner = max(stats, key=lambda x: x["avg"])

        for s in stats:
            line = (
                f"{s['value']} -> "
                f"avg {round(s['avg'],1)} | "
                f"median {round(s['median'],1)} | "
                f"best {round(s['best'],1)}"
            )

            if s["value"] == winner["value"]:
                line += "  <-- winner"

            print(line)

    print()
    print("========================================")


# ==========================================
# CSV EXPORT
# ==========================================
def export_csv(results, report_dir="reports/optimizer"):
    if not results:
        return

    output = Path(report_dir)
    if not output.is_absolute():
        output = Path(__file__).resolve().parents[1] / output
    output.mkdir(parents=True, exist_ok=True)
    path = output / "optimization_results.csv"

    rows = []

    for r in results:
        row = {
            "score": r["score"],
            "net_r": r["net_r"],
            "profit_factor": r["profit_factor"],
            "max_drawdown": r["max_drawdown"],
            "total_trades": r["total_trades"],
            "average_execution_cost_r": r.get("execution_costs", {}).get(
                "average_r", 0.0,
            ),
        }

        for k, v in r["strategy_params"].items():
            row[k] = v

        for k, v in r["execution_params"].items():
            row[k] = v

        rows.append(row)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"CSV saved -> {path}")


# ==========================================
# MAIN OPTIMIZER
# ==========================================
def run_optimizer(
    df,
    strategy_class,
    param_grid,
    execution_grid,
    top_n=15,
    workers=None,
    silent=False,
    report_dir="reports/optimizer",
    scoring_config=None,
    execution_replay_df=None,
    replay_kwargs=None,
):
    strategy_combos = generate_grid(param_grid)
    execution_combos = generate_grid(execution_grid)
    full_grid = merge_grids(strategy_combos, execution_combos)

    total = len(full_grid)

    if workers is None:
        workers = max(1, os.cpu_count() - 1)

    if not silent:
        print()
        print("========== OPTIMIZER START ==========")
        print(f"CPU cores: {os.cpu_count()}")
        print(f"Workers: {workers}")
        print(f"Combinations: {total}")
        print("=====================================")

    results = []
    completed = 0

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=init_worker,
        initargs=(
            df, strategy_class, scoring_config,
            execution_replay_df, dict(replay_kwargs or {}),
        )
    ) as executor:

        futures = [
            executor.submit(evaluate_params, combo)
            for combo in full_grid
        ]

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

            completed += 1

            if not silent:
                if completed % 25 == 0 or completed == total:
                    print(f"Progress: {completed}/{total}")

    valid_results = [r for r in results if r["valid"]]
    valid_results.sort(key=lambda x: x["score"], reverse=True)

    if not silent:
        print_top(valid_results, top_n)
        print_parameter_analysis(valid_results, param_grid, "STRATEGY")
        print_parameter_analysis(valid_results, execution_grid, "EXECUTION")
        export_csv(valid_results, report_dir)

    return valid_results
