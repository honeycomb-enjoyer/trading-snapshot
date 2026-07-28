# trading_research/overfit_tests/walkforward_test/metrics.py

import numpy as np
from collections import Counter


PF_CAP = 10


def normalize_pf(pf):
    if pf >= 999:
        return PF_CAP
    return min(pf, PF_CAP)


def compute_max_drawdown(equity_curve):
    if not equity_curve:
        return 0

    peak = equity_curve[0]
    max_dd = 0

    for x in equity_curve:
        if x > peak:
            peak = x

        dd = peak - x

        if dd > max_dd:
            max_dd = dd

    return max_dd


def build_full_oos_equity(results):
    equity = [0]

    for r in results:
        if "oos_equity" not in r:
            equity.append(equity[-1] + r["oos_net_r"])
            continue

        window_equity = r["oos_equity"]

        if len(window_equity) < 2:
            continue

        prev = window_equity[0]

        for current in window_equity[1:]:
            delta = current - prev
            equity.append(equity[-1] + delta)
            prev = current

    return equity


def window_passed(window_result):
    pf = normalize_pf(window_result["oos_pf"])

    if pf < 1.15:
        return False

    if window_result["pf_retention"] < 55:
        return False

    return True


def _hashable_parameter(value):
    """Recursively freeze nested configuration values for Counter keys."""
    if isinstance(value, dict):
        return tuple(sorted(
            (key, _hashable_parameter(item)) for key, item in value.items()
        ))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable_parameter(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_hashable_parameter(item) for item in value))
    return value


def compute_parameter_stability(results):
    if not results:
        return 0

    all_params = []

    for r in results:
        params = tuple(sorted(
            (key, _hashable_parameter(value))
            for key, value in r["strategy_params"].items()
        ))
        all_params.append(params)

    counts = Counter(all_params)
    most_common = counts.most_common(1)[0][1]

    return most_common / len(results) * 100


def compute_robustness_score(
    pass_rate,
    pf_retention,
    median_oos_pf,
    parameter_stability
):
    score = 0
    score += pass_rate * 0.35
    score += min(pf_retention, 100) * 0.30
    score += min((median_oos_pf / 2.0) * 100, 100) * 0.20
    score += parameter_stability * 0.15
    return round(score)


def verdict_from_score(score):
    if score >= 80:
        return "LIKELY ROBUST"
    elif score >= 65:
        return "PROMISING BUT NEEDS OOS"
    elif score >= 50:
        return "UNCERTAIN"
    else:
        return "LIKELY OVERFIT"


def aggregate_walkforward_results(results):
    if not results:
        raise RuntimeError("No WFT results")

    windows_total = len(results)
    windows_passed = sum(window_passed(r) for r in results)
    pass_rate = windows_passed / windows_total * 100

    oos_pfs = [normalize_pf(r["oos_pf"]) for r in results]
    median_oos_pf = float(np.median(oos_pfs))

    oos_net_r_total = sum(r["oos_net_r"] for r in results)

    full_equity = build_full_oos_equity(results)
    oos_max_dd = compute_max_drawdown(full_equity)

    baseline_train_pf = float(
        np.median([normalize_pf(r["train_pf"]) for r in results])
    )

    pf_retention = (
        median_oos_pf / baseline_train_pf * 100
        if baseline_train_pf > 0 else 0
    )

    parameter_stability = compute_parameter_stability(results)

    robustness_score = compute_robustness_score(
        pass_rate,
        pf_retention,
        median_oos_pf,
        parameter_stability
    )

    verdict = verdict_from_score(robustness_score)

    return {
        "windows_total": windows_total,
        "windows_passed": windows_passed,
        "pass_rate": pass_rate,
        "median_oos_pf": median_oos_pf,
        "oos_net_r_total": oos_net_r_total,
        "oos_max_dd": oos_max_dd,
        "baseline_train_pf": baseline_train_pf,
        "pf_retention": pf_retention,
        "parameter_stability": parameter_stability,
        "robustness_score": robustness_score,
        "verdict": verdict
    }


def print_walkforward_report(report):
    print()
    print("========== WALK FORWARD RESULT ==========")
    print()
    print(f"Windows total:   {report['windows_total']}")
    print(f"Windows passed:  {report['windows_passed']} / {report['windows_total']}")
    print(f"Pass rate:       {round(report['pass_rate'], 1)}%")
    print()
    print("Metrics:")
    print(f"OOS PF Median:   {round(report['median_oos_pf'], 3)}")
    print(f"OOS Net R total: {round(report['oos_net_r_total'], 2)}")
    print(f"OOS Max DD:      {round(report['oos_max_dd'], 2)}")
    print()
    print(f"Baseline train PF:  {round(report['baseline_train_pf'], 3)}")
    print(f"OOS PF retention:   {round(report['pf_retention'], 1)}%")
    print()
    print(f"Overall parameter stability: {round(report['parameter_stability'], 1)}%")
    print()
    print(f"Robustness score:   {report['robustness_score']} / 100")
    print()
    print("Verdict:")
    print(report["verdict"])
    print("=========================================")
