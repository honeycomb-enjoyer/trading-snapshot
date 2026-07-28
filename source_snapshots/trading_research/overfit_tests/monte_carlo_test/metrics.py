from pathlib import Path
import numpy as np

REPORT_DIR = Path(__file__).resolve().parents[2] / "reports" / "monte_carlo"


def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt

# ==========================================
# DRAWDOWN
# ==========================================
def compute_max_drawdown(equity_curve):
    peak = equity_curve[0]
    max_dd = 0

    for x in equity_curve:
        if x > peak:
            peak = x

        dd = peak - x

        if dd > max_dd:
            max_dd = dd

    return max_dd


# ==========================================
# AGGREGATE METRICS
# ==========================================
def analyze_mc_results(equity_curves):
    final_returns = [curve[-1] for curve in equity_curves]
    drawdowns = [compute_max_drawdown(curve) for curve in equity_curves]

    report = {
        "simulations": len(equity_curves),

        # Final equity
        "median_final_r": float(np.median(final_returns)),
        "mean_final_r": float(np.mean(final_returns)),
        "best_final_r": float(np.max(final_returns)),
        "worst_final_r": float(np.min(final_returns)),
        "p5_final_r": float(np.percentile(final_returns, 5)),
        "p95_final_r": float(np.percentile(final_returns, 95)),

        # Drawdown
        "mean_dd": float(np.mean(drawdowns)),
        "median_dd": float(np.median(drawdowns)),
        "best_dd": float(np.min(drawdowns)),
        "worst_dd": float(np.max(drawdowns)),
        "p95_dd": float(np.percentile(drawdowns, 95)),

        # Risk
        "dd_gt_10": sum(dd > 10 for dd in drawdowns) / len(drawdowns) * 100,
        "dd_gt_15": sum(dd > 15 for dd in drawdowns) / len(drawdowns) * 100,
        "dd_gt_20": sum(dd > 20 for dd in drawdowns) / len(drawdowns) * 100,
        "dd_gt_30": sum(dd > 30 for dd in drawdowns) / len(drawdowns) * 100,

        # Tail
        "prob_negative": sum(r < 0 for r in final_returns) / len(final_returns) * 100,
        "prob_ruin": sum(r < -50 for r in final_returns) / len(final_returns) * 100,

        "profitable_runs_pct":
            sum(r > 0 for r in final_returns) / len(final_returns) * 100
    }

    return report


# ==========================================
# TERMINAL REPORT
# ==========================================
def print_mc_report(report):
    print()
    print("=========================================")
    print("MONTE CARLO TEST")
    print("=========================================")
    print()

    print(f"Simulations:      {report['simulations']}")

    print()
    print("========== MONTE CARLO RESULTS ==========")
    print()

    print("Final Equity Distribution:")
    print(f"Mean Final R:      {round(report['mean_final_r'],2)}")
    print(f"Median Final R:    {round(report['median_final_r'],2)}")
    print(f"Best Final R:      {round(report['best_final_r'],2)}")
    print(f"Worst Final R:     {round(report['worst_final_r'],2)}")
    print(f"5% Worst Final:    {round(report['p5_final_r'],2)}")
    print(f"95% Best Final:    {round(report['p95_final_r'],2)}")

    print()
    print("Drawdown Distribution:")
    print(f"Mean Max DD:       {round(report['mean_dd'],2)}R")
    print(f"Median Max DD:     {round(report['median_dd'],2)}R")
    print(f"Best Max DD:       {round(report['best_dd'],2)}R")
    print(f"Worst Max DD:      {round(report['worst_dd'],2)}R")
    print(f"95% Worst DD:      {round(report['p95_dd'],2)}R")

    print()
    print("Risk Metrics:")
    print(f"Prob DD > 10R:     {round(report['dd_gt_10'],1)}%")
    print(f"Prob DD > 15R:     {round(report['dd_gt_15'],1)}%")
    print(f"Prob DD > 20R:     {round(report['dd_gt_20'],1)}%")
    print(f"Prob DD > 30R:     {round(report['dd_gt_30'],1)}%")

    print()
    print("Tail Risk:")
    print(f"Prob Negative:     {round(report['prob_negative'],1)}%")
    print(f"Prob Ruin:         {round(report['prob_ruin'],1)}%")

    print()
    print(f"Profitable Runs:   {round(report['profitable_runs_pct'],1)}%")


# ==========================================
# PLOTS
# ==========================================
def plot_mc_equity_curves(equity_curves, max_curves=200, save=True, report_dir=REPORT_DIR):
    plt = _pyplot()
    plt.style.use('dark_background')
    plt.figure(figsize=(14, 6))

    curves_to_plot = equity_curves[:max_curves]

    for curve in curves_to_plot:
        plt.plot(curve, alpha=0.08)

    plt.title("Monte Carlo Equity Distribution")
    plt.xlabel("Trades")
    plt.ylabel("Equity (R)")
    plt.grid(True, axis="x")
    plt.tight_layout()

    if save:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        plt.savefig(Path(report_dir) / "mc_equity_distribution.png", dpi=200)

    plt.close()


def plot_mc_histogram(equity_curves, save=True, report_dir=REPORT_DIR):
    plt = _pyplot()
    final_returns = [curve[-1] for curve in equity_curves]

    plt.style.use('dark_background')
    plt.figure(figsize=(10, 5))
    plt.hist(final_returns, bins=40)

    plt.axvline(np.median(final_returns), linestyle="--")
    plt.axvline(np.percentile(final_returns, 5), linestyle=":")
    plt.axvline(np.percentile(final_returns, 95), linestyle=":")

    plt.title("Final Equity Distribution")
    plt.xlabel("Final Net R")
    plt.ylabel("Frequency")
    plt.tight_layout()

    if save:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        plt.savefig(Path(report_dir) / "mc_histogram.png", dpi=200)

    plt.close()

def plot_mc_drawdown_histogram(drawdowns, save=True, report_dir=REPORT_DIR):
    plt = _pyplot()

    plt.style.use('dark_background')
    plt.figure(figsize=(10, 5))
    plt.hist(drawdowns, bins=40)

    plt.axvline(np.median(drawdowns), linestyle="--")
    plt.axvline(np.percentile(drawdowns, 95), linestyle=":")

    plt.title("Max Drawdown Distribution")
    plt.xlabel("Max Drawdown (R)")
    plt.ylabel("Frequency")
    plt.tight_layout()

    if save:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        plt.savefig(Path(report_dir) / "mc_drawdown_histogram.png", dpi=200)

    plt.close()
