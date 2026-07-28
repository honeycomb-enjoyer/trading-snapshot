from master_config import BACKTEST_CONFIG, PERMUTATION_CONFIG, STRATEGY_CLASS, STRATEGY_PARAMS
from overfit_tests.permutation_test.permutation_test_runner import run_permutation_test
from runners.common import precompute, prepare_data, report_dir, select_dataset


def main():
    config = PERMUTATION_CONFIG
    result = run_permutation_test(
        train_df=select_dataset(prepare_data(), config["dataset"]),
        strategy_class=STRATEGY_CLASS,
        strategy_params=STRATEGY_PARAMS,
        execution_params=BACKTEST_CONFIG,
        n_perm=config["permutations"],
        skip_equity_plots=config["skip_equity_plots"],
        report_dir=report_dir("permutation"),
        precompute_fn=precompute,
    )
    print("\nFINAL RESULT:")
    for key, value in result.items():
        if key != "noise_pfs":
            print(f"{key}: {value}")
    return result


if __name__ == "__main__":
    main()
