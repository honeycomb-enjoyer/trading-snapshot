# overfit_tests/permutation_test/permutation_test_runner.py

import os
import numpy as np
from pathlib import Path

from overfit_tests.permutation_test.permutator import get_permutation
from engine.precompute import precompute_for_params
from engine.backtester import run_backtest_equity_only


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _report_path(report_dir):
    path = Path(report_dir)
    return path if path.is_absolute() else PROJECT_ROOT / path


def permutation_verdict(p_value):
    if p_value <= 0.01:
        return "EXTREMELY STRONG EDGE"
    if p_value <= 0.05:
        return "VERY STRONG EDGE"
    if p_value <= 0.20:
        return "LIKELY REAL EDGE"
    if p_value <= 0.40:
        return "UNCERTAIN"
    return "LIKELY OVERFIT"


def save_permutation_plots(
    original_pf,
    noise_pfs,
    original_equity_dates,
    original_equity,
    noise_equities,
    report_dir="reports/permutation_test",
):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    save_dir = _report_path(report_dir)
    os.makedirs(save_dir, exist_ok=True)

    plt.style.use("dark_background")
    plt.figure(figsize=(10, 5))
    plt.hist(noise_pfs, bins=max(10, min(60, len(noise_pfs) // 10)))
    plt.axvline(
        original_pf,
        linestyle="--",
        linewidth=2,
        label=f"Original PF = {round(original_pf, 3)}"
    )
    plt.title("Permutation Test - Profit Factor Distribution")
    plt.xlabel("Profit Factor")
    plt.ylabel("Frequency")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(save_dir / "pf_histogram.png")
    plt.close()

    plt.style.use("dark_background")
    plt.figure(figsize=(14, 6))

    for dates, eq in noise_equities:
        plt.plot(dates, eq, alpha=0.05, linewidth=0.5)

    plt.plot(
        original_equity_dates,
        original_equity,
        linewidth=3,
        label="Original"
    )

    ax = plt.gca()
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.title("Original vs Permuted Equity Curves")
    plt.xlabel("Date")
    plt.ylabel("Equity (R)")
    plt.legend()
    plt.grid(True, axis="x")
    plt.tight_layout()
    plt.savefig(save_dir / "equity_overlay.png")
    plt.close()


def run_permutation_test(
    train_df,
    strategy_class,
    strategy_params,
    execution_params,
    n_perm=1000,
    skip_equity_plots=False,
    report_dir="reports/permutation_test",
    precompute_fn=None,
):
    noise_pfs = []
    noise_equities = []

    print()
    print("========== PERMUTATION TEST ==========")
    print(f"Permutations: {n_perm}")
    print("======================================")

    # --------------------------------------------------
    # Prepare baseline dataframe
    # --------------------------------------------------

    if precompute_fn is None:
        base_df = precompute_for_params(train_df.copy(), strategy_params, silent=True)
    else:
        base_df = precompute_fn(train_df.copy(), strategy_params)

    # ==========================================
    # Baseline strategy
    # ==========================================

    print("Running baseline strategy...")

    original_strategy = strategy_class(**strategy_params)

    original_equity_dates, original_equity, original_metrics = (
        run_backtest_equity_only(
            df=base_df,
            strategy=original_strategy,
            **execution_params
        )
    )

    original_profit_factor = original_metrics["profit_factor"]

    print(f"Baseline PF: {round(original_profit_factor,3)}")

    # ==========================================
    # Permutations
    # ==========================================

    failed = 0

    for i in range(n_perm):

        if (i + 1) % 10 == 0 or i == 0:
            print(f"[Permutation {i+1}/{n_perm}]")

        try:

            perm_df = get_permutation(
                base_df.set_index("timestamp")[["open", "high", "low", "close"]],
                seed=i
            )

            perm_df["year"] = perm_df["timestamp"].dt.year
            perm_df["month"] = perm_df["timestamp"].dt.month
            perm_df["day"] = perm_df["timestamp"].dt.day
            perm_df["hour"] = perm_df["timestamp"].dt.hour
            perm_df["weekday"] = perm_df["timestamp"].dt.weekday

            if precompute_fn is None:
                perm_df = precompute_for_params(perm_df, strategy_params, silent=True)
            else:
                perm_df = precompute_fn(perm_df, strategy_params)

            perm_strategy = strategy_class(**strategy_params)

            perm_dates, perm_equity, perm_metrics = (
                run_backtest_equity_only(
                    df=perm_df,
                    strategy=perm_strategy,
                    **execution_params
                )
            )

            perm_pf = perm_metrics["profit_factor"]

            if np.isfinite(perm_pf):
                noise_pfs.append(perm_pf)

                if not skip_equity_plots:
                    noise_equities.append(
                        (perm_dates, perm_equity)
                    )

        except Exception as e:
            failed += 1
            print(f"Permutation {i} failed: {e}")

    # ==========================================
    # Final statistics
    # ==========================================

    arr = np.array(noise_pfs)

    if len(arr) == 0:
        raise RuntimeError(f"All {n_perm} permutations failed; inspect the failure messages above")

    median_pf = float(np.median(arr))
    mean_pf = float(np.mean(arr))
    max_pf = float(np.max(arr))

    better_count = np.sum(arr >= original_profit_factor)

    # Add-one correction avoids reporting an impossible exact p=0 from a
    # finite Monte Carlo sample (minimum is 1 / (valid permutations + 1)).
    p_value = (better_count + 1) / (len(arr) + 1)

    percentile = (
        np.sum(arr < original_profit_factor)
        / len(arr)
    ) * 100

    print()
    print("========== PERMUTATION RESULT ==========")
    print(f"Valid permutations: {len(arr)}")
    print(f"Failed:             {failed}")
    print(f"Original PF:        {round(original_profit_factor,3)}")
    print(f"Noise median PF:    {round(median_pf,3)}")
    print(f"Noise mean PF:      {round(mean_pf,3)}")
    print(f"Noise max PF:       {round(max_pf,3)}")
    print(f"Percentile:         {round(percentile,2)}%")
    print(f"p-value:            {round(p_value,4)}")

    verdict = permutation_verdict(p_value)

    print(f"Verdict:            {verdict}")
    print("========================================")

    print("Saving plots...")

    save_permutation_plots(
        original_profit_factor,
        noise_pfs,
        original_equity_dates,
        original_equity,
        noise_equities if len(noise_equities) > 0 else [],
        report_dir=report_dir,
    )

    return {
        "original_pf": original_profit_factor,
        "median": median_pf,
        "mean": mean_pf,
        "max": max_pf,
        "percentile": percentile,
        "p_value": p_value,
        "verdict": verdict,
    }
