from pathlib import Path

import numpy as np

from master_config import OPTIMIZER_CONFIG, STRATEGY_CLASS, WALKFORWARD_CONFIG
from overfit_tests.walkforward_test.metrics import aggregate_walkforward_results, print_walkforward_report
from overfit_tests.walkforward_test.window_generator import generate_walkforward_windows, print_windows_summary
from overfit_tests.walkforward_test.window_runner import run_single_window, print_window_result
from runners.common import (
    precompute,
    prepare_data,
    prepare_execution_replay_data,
    report_dir,
    select_dataset,
)


def plot_oos_equity(results, output: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    full_equity = [0]
    for result in results:
        window_equity = result["oos_equity"]
        if len(window_equity) > 1:
            offset = full_equity[-1]
            full_equity.extend(offset + value for value in window_equity[1:])
    plt.style.use("dark_background")
    plt.figure(figsize=(14, 6))
    plt.plot(full_equity, linewidth=2)
    plt.title("Walk Forward OOS Equity Curve")
    plt.xlabel("Trade")
    plt.ylabel("Net R")
    plt.grid(True, axis="x")
    plt.tight_layout()
    path = output / "oos_equity_curve.png"
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Saved: {path}")


def plot_pf_histogram(results, output: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    pfs = [result["oos_pf"] for result in results]
    plt.figure(figsize=(8, 4))
    plt.hist(pfs, bins=12)
    plt.axvline(np.median(pfs), linestyle="--")
    plt.title("OOS PF Distribution")
    plt.xlabel("Profit Factor")
    plt.ylabel("Count")
    plt.tight_layout()
    path = output / "pf_histogram.png"
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Saved: {path}")


def main():
    config = WALKFORWARD_CONFIG
    # Features are computed inside each train/OOS window so no calculation can
    # cross a window boundary. Avoid precomputing the whole dataset twice.
    manager = prepare_data()
    df = select_dataset(manager, config["dataset"])
    print("\n========== WALKFORWARD DATASET ==========")
    print(f"{config['dataset'].title()} bars: {len(df)}")
    print("=========================================")
    windows = generate_walkforward_windows(
        df=df,
        mode=config["mode"],
        train_window=config["train_window"],
        test_window=config["test_window"],
        step_window=config["step"],
    )
    print_windows_summary(df, windows)
    if not windows:
        print("\nNo valid walk-forward windows produced results.")
        return [], None
    replay_df, replay_kwargs = prepare_execution_replay_data(
        manager, mode=config["dataset"], signal_df=df,
    )
    if replay_df is not None:
        print(f"Execution replay bars: {len(replay_df)}")
    results = []
    for index, window in enumerate(windows, 1):
        print(f"\nRunning window {index}/{len(windows)}...")
        result = run_single_window(
            df=df,
            window=window,
            strategy_class=STRATEGY_CLASS,
            param_grid=OPTIMIZER_CONFIG["param_grid"],
            execution_grid=OPTIMIZER_CONFIG["execution_grid"],
            optimizer_workers=config["optimizer_workers"],
            scoring_config=config["scoring"],
            precompute_fn=precompute,
            execution_replay_df=replay_df,
            replay_kwargs=replay_kwargs,
        )
        if result is not None:
            results.append(result)
            print_window_result(result, index)
    if not results:
        print("\nNo valid walk-forward windows produced results.")
        return [], None
    report = aggregate_walkforward_results(results)
    print_walkforward_report(report)
    output = report_dir("walk_forward")
    plot_oos_equity(results, output)
    plot_pf_histogram(results, output)
    return results, report


if __name__ == "__main__":
    main()
