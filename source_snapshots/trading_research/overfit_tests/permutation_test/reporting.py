from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


REPORT_DIR = Path(__file__).resolve().parents[2] / "reports" / "permutation_test"


def save_pf_histogram(original_pf, noise_pfs):
    plt.style.use('dark_background')
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    plt.figure(figsize=(10, 6))

    plt.hist(noise_pfs, bins=20, alpha=0.7)
    plt.axvline(
        original_pf,
        linestyle="--",
        linewidth=2,
        label=f"Original PF = {original_pf:.2f}"
    )

    plt.xlabel("Profit Factor")
    plt.ylabel("Frequency")
    plt.title("Permutation Test Distribution")
    plt.legend()

    path = REPORT_DIR / "pf_distribution.png"
    plt.savefig(path, dpi=150)
    plt.close()

    return path


def save_equity_comparison(original_equity, perm_equity):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    plt.style.use('dark_background')
    plt.figure(figsize=(12, 6))

    plt.plot(original_equity, label="Original Equity")
    plt.plot(perm_equity, alpha=0.7, label="Permuted Equity")

    plt.title("Original vs Permuted Equity Curve")
    plt.xlabel("Trades")
    plt.ylabel("Equity (R)")
    plt.legend()

    path = REPORT_DIR / "equity_comparison.png"
    plt.savefig(path, dpi=150)
    plt.close()

    return path
