import io
import os
import unittest
from contextlib import redirect_stdout

import pandas as pd

import master_config
from data.data_manager import DataManager
from engine.backtester import print_report, run_backtest
from data.schema import project_root
from runners.common import report_dir


class NoSignalStrategy:
    def bind_data(self, df):
        self.df = df

    def on_bar(self, index, df):
        return None


class MasterConfigTests(unittest.TestCase):
    def test_data_manager_defaults_come_from_master_config(self):
        manager = DataManager()
        self.assertEqual(manager.data_config, master_config.DATA_CONFIG)
        self.assertEqual(manager.split_mode, master_config.SPLIT_CONFIG["mode"])
        self.assertEqual(manager.date_splits, master_config.SPLIT_CONFIG["dates"])
        self.assertEqual(manager.ratio_splits, master_config.SPLIT_CONFIG["ratios"])

    def test_open_bar_needs_no_duplicate_boolean_flag(self):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "open": [10.0, 10.0], "high": [10.0, 10.0],
            "low": [10.0, 10.0], "close": [10.0, 10.0],
        })
        metrics = run_backtest(df, NoSignalStrategy(), stats_only=True, execution_mode="open_bar")
        self.assertEqual(metrics["execution"]["fill_timing"], "same_bar_trigger")

    def test_only_train_holdout_and_full_dataset_modes_are_available(self):
        self.assertEqual(set(master_config.SPLIT_CONFIG["ratios"]), {"train", "holdout"})
        self.assertTrue(all(not key.startswith("sandbox") for key in master_config.SPLIT_CONFIG["dates"]))

    def test_instrument_cost_profiles_are_the_only_configured_cost_model(self):
        self.assertNotIn("execution_cost_r", master_config.BACKTEST_CONFIG)
        self.assertNotIn(
            "execution_cost_r", master_config.OPTIMIZER_CONFIG["execution_grid"],
        )
        profiles = master_config.EXECUTION_COST_MODEL["profiles"]
        self.assertEqual(
            set(profiles),
            {"CADJPY", "USDJPY", "GBPUSD", "USDCHF", "EURGBP", "AUDCAD", "XAUUSD"},
        )
        self.assertIs(
            master_config.BACKTEST_CONFIG["execution_cost_model"],
            master_config.EXECUTION_COST_MODEL,
        )

    def test_report_paths_are_anchored_to_project_root_not_process_cwd(self):
        previous = os.getcwd()
        try:
            os.chdir(project_root().parent)
            self.assertEqual(report_dir("backtest"), project_root() / "reports" / "backtest")
        finally:
            os.chdir(previous)

    def test_backtest_report_keeps_complete_expected_metrics(self):
        metrics = {
            "total_trades": 3, "wins": 1, "losses": 1, "be_trades": 1,
            "winrate": 33.333, "winrate_no_be": 50.0, "net_r": 1.0,
            "max_drawdown": 1.0, "profit_factor": 2.0, "expectancy": 0.333,
            "avg_win": 2.0, "avg_loss": -1.0, "best_trade": 2.0, "worst_trade": -1.0,
        }
        output = io.StringIO()
        with redirect_stdout(output):
            print_report(metrics)
        report = output.getvalue()
        for label in (
            "Winrate (raw)", "Winrate without BE", "Avg Win", "Avg Loss",
            "Best Trade", "Worst Trade",
        ):
            self.assertIn(label, report)


if __name__ == "__main__":
    unittest.main()
