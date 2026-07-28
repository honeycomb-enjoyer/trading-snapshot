import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from engine.backtester import run_backtest
from engine.precompute import precompute_for_params
from data.data_manager import DataManager
from data.schema import project_root
from strategy.basic_mean_reversion import BasicMeanReversion


def frame(rows):
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=len(rows), freq="h")
    return pd.DataFrame(rows, index=None).assign(timestamp=timestamps)[["timestamp", "open", "high", "low", "close"]]


class SignalAtIndexes:
    def __init__(self, indexes, *, entry=10.0, sl=9.0, tp=11.0, side="BUY"):
        self.indexes = set(indexes)
        self.signal = {"entry": entry, "sl": sl, "tp": tp, "side": side}
        self.calls = []

    def bind_data(self, df):
        self.df = df

    def on_bar(self, i, df):
        self.calls.append(i)
        return dict(self.signal) if i in self.indexes else None


class BacktesterTests(unittest.TestCase):
    def test_lower_timeframe_replay_fills_next_native_bar_and_exits_its_last_bar(self):
        signal_df = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z",
            ]),
            "open": [10, 10, 11], "high": [10, 11, 12],
            "low": [9, 9, 10], "close": [10, 11, 11.5],
        })
        replay_df = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2024-01-01T00:00:00Z", "2024-01-01T12:00:00Z",
                "2024-01-02T00:00:00Z", "2024-01-02T12:00:00Z",
                "2024-01-03T00:00:00Z", "2024-01-03T12:00:00Z",
            ]),
            "open": [10, 10, 11, 11.2, 11.5, 11.7],
            "high": [10.2, 10.2, 11.3, 11.5, 11.8, 12],
            "low": [9.8, 9.8, 10.8, 11, 11.3, 11.5],
            "close": [10, 10, 11.2, 11.4, 11.7, 11.9],
        })

        class NativeTimeExit(SignalAtIndexes):
            def should_exit(self, i, position, frame):
                return i == position["open_bar"]

        trades, _, metrics = run_backtest(
            signal_df,
            NativeTimeExit({0}, entry=10, sl=8, tp=None),
            collect_equity=True,
            execution_replay_df=replay_df,
        )

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["open_time"], replay_df["timestamp"].iloc[2])
        self.assertEqual(trades[0]["close_time"], replay_df["timestamp"].iloc[3])
        self.assertEqual(trades[0]["entry"], replay_df["open"].iloc[2])
        self.assertEqual(trades[0]["exit"], replay_df["close"].iloc[3])
        self.assertTrue(metrics["execution"]["replay_enabled"])

    def test_lower_timeframe_replay_waits_for_open_bar_entry_touch(self):
        signal_df = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2024-01-01T00:00:00Z", "2024-01-01T04:00:00Z",
            ]),
            "open": [10, 10], "high": [10, 11],
            "low": [10, 9], "close": [10, 10],
        })
        replay_df = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z",
                "2024-01-01T04:00:00Z", "2024-01-01T06:00:00Z",
            ]),
            "open": [10, 10, 9.5, 10], "high": [10, 10, 9.8, 11],
            "low": [10, 10, 9, 9.8], "close": [10, 10, 9.6, 10.8],
        })
        trades, _, _ = run_backtest(
            signal_df,
            SignalAtIndexes({1}, entry=10, sl=9, tp=11, side="BUY"),
            collect_equity=True,
            execution_mode="open_bar",
            execution_replay_df=replay_df,
        )

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["open_time"], replay_df["timestamp"].iloc[3])
        self.assertEqual(trades[0]["close_reason"], "tp_same_bar_trigger")

    def test_replay_rejects_signal_with_incomplete_closed_history(self):
        signal_df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
        ])
        timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=36, freq="5min").delete(6)
        replay_df = pd.DataFrame({
            "timestamp": timestamps,
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
        })

        class HistorySignal(SignalAtIndexes):
            def on_bar(self, i, frame):
                if i != 0:
                    return None
                return {
                    "side": "BUY", "entry": 10, "sl": 9, "tp": None,
                    "execution_history_start": pd.Timestamp("2024-01-01T00:00:00Z"),
                    "execution_history_end": pd.Timestamp("2024-01-01T01:00:00Z"),
                }

            def should_exit(self, i, position, frame):
                return i == position["open_bar"]

        trades, _, metrics = run_backtest(
            signal_df, HistorySignal(set()), collect_equity=True,
            execution_replay_df=replay_df,
        )
        self.assertEqual(trades, [])
        self.assertEqual(metrics["execution"]["skipped_history_incomplete"], 1)

    def test_replay_rejects_missing_first_execution_bar_instead_of_delaying_entry(self):
        signal_df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
        ])
        timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=36, freq="5min").delete(12)
        replay_df = pd.DataFrame({
            "timestamp": timestamps,
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
        })

        class EntrySignal(SignalAtIndexes):
            def should_exit(self, i, position, frame):
                return i == position["open_bar"]

        trades, _, metrics = run_backtest(
            signal_df, EntrySignal({0}, tp=None), collect_equity=True,
            execution_replay_df=replay_df,
        )
        self.assertEqual(trades, [])
        self.assertEqual(metrics["execution"]["skipped_entry_bar_unavailable"], 1)

    def test_basic_mean_reversion_uses_only_closed_bar_atr(self):
        native = frame([
            {"open": 10, "high": 11, "low": 9, "close": 10},
            {"open": 10, "high": 11, "low": 9, "close": 10},
            {"open": 10, "high": 11, "low": 9, "close": 10},
            {"open": 10, "high": 12, "low": 9.5, "close": 11},
        ])
        computed = precompute_for_params(native, {"atr_period": 2})
        strategy = BasicMeanReversion(range_lookback=2, atr_period=2, atr_multiplier=1)
        strategy.bind_data(computed)
        signal = strategy.on_bar(3, computed)

        self.assertEqual(signal["entry"], 11)
        self.assertEqual(signal["entry_trigger"], "price_at_or_above")
        self.assertEqual(signal["sl"] - signal["entry"], computed["atr_2"].iloc[2])

    def test_optional_tp_and_strategy_time_exit(self):
        df = frame([
            {"open": 10, "high": 10.2, "low": 9.8, "close": 10},
            {"open": 10, "high": 10.2, "low": 9.8, "close": 10},
            {"open": 11, "high": 11.4, "low": 10.6, "close": 11.2},
        ])

        class TimeExit(SignalAtIndexes):
            def should_exit(self, i, position, frame):
                return i == 2

        strategy = TimeExit({1}, entry=10, sl=9, tp=None)
        trades, _, _ = run_backtest(df, strategy, collect_equity=True)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["close_reason"], "strategy_exit")
        self.assertEqual(trades[0]["exit"], 11.2)

    def test_time_exit_can_use_virtual_risk_without_executing_stop(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 11, "low": 7, "close": 11},
        ])

        class TimeExit(SignalAtIndexes):
            def on_bar(self, i, frame):
                if i not in self.indexes:
                    return None
                return {
                    "side": "BUY", "entry": 10, "sl": None,
                    "risk_reference": 8, "tp": None,
                }

            def should_exit(self, i, position, frame):
                return i == 2

        trades, _, _ = run_backtest(df, TimeExit({1}), collect_equity=True)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["close_reason"], "strategy_exit")
        self.assertEqual(trades[0]["R"], 0.5)

    def test_sl_distance_is_anchored_to_actual_next_open(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 12, "high": 12, "low": 10, "close": 11},
        ])

        class DistanceSignal(SignalAtIndexes):
            def on_bar(self, i, frame):
                if i not in self.indexes:
                    return None
                return {"side": "BUY", "entry": 10, "sl_distance": 1, "tp": None}

        trades, _, _ = run_backtest(df, DistanceSignal({1}), collect_equity=True)
        self.assertEqual(trades[0]["entry"], 12)
        self.assertEqual(trades[0]["exit"], 11)
        self.assertEqual(trades[0]["R"], -1)

    def test_absolute_stop_order_is_skipped_when_next_open_gaps_beyond_stop(self):
        df = frame([
            {"open": 10, "high": 10.2, "low": 9.8, "close": 10},
            {"open": 11, "high": 11.2, "low": 10.8, "close": 11},
            {"open": 11, "high": 11.2, "low": 10.8, "close": 11},
        ])
        strategy = SignalAtIndexes({0}, entry=10, sl=10.5, tp=None, side="SELL")
        trades, _, metrics = run_backtest(df, strategy, collect_equity=True)
        self.assertEqual(trades, [])
        self.assertEqual(metrics["total_trades"], 0)

    def test_plotting_is_explicit_and_headless_by_default(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
        ])
        strategy = SignalAtIndexes(set())
        with patch("engine.backtester.plot_equity") as equity_plot, patch(
            "engine.backtester.plot_monthly_returns"
        ) as monthly_plot:
            run_backtest(df, strategy, warmup_bars=0)
            equity_plot.assert_not_called()
            monthly_plot.assert_not_called()

            run_backtest(df, strategy, warmup_bars=0, plot=True)
            equity_plot.assert_called_once()
            monthly_plot.assert_called_once()

    def test_next_bar_fills_at_following_open_not_signal_price(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 12, "high": 14, "low": 11, "close": 13},
        ])
        trades, _, _ = run_backtest(df, SignalAtIndexes({1}, tp=13), collect_equity=True, warmup_bars=0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry"], 12.0)
        self.assertEqual(trades[0]["open_time"], df["timestamp"].iloc[2])
        self.assertEqual(trades[0]["signal_time"], df["timestamp"].iloc[1])

    def test_open_bar_mode_is_the_opt_in_and_counts_trigger_and_stop_as_stop(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 7, "high": 10.5, "low": 6, "close": 10},
            {"open": 10, "high": 10.5, "low": 8, "close": 9},
        ])
        trades, _, _ = run_backtest(
            df, SignalAtIndexes({1}), collect_equity=True, execution_mode="open_bar"
        )
        self.assertEqual(trades[0]["entry"], 10.0)
        self.assertEqual(trades[0]["open_time"], df["timestamp"].iloc[1])
        self.assertEqual(trades[0]["close_time"], df["timestamp"].iloc[1])
        self.assertEqual(trades[0]["exit"], 9.0)
        self.assertEqual(trades[0]["R"], -1.0)
        self.assertEqual(trades[0]["close_reason"], "sl_same_bar_trigger")

    def test_same_bar_stop_loss_wins_over_take_profit(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 12, "low": 8, "close": 10},
        ])
        trades, _, metrics = run_backtest(
            df, SignalAtIndexes({1}), collect_equity=True
        )
        self.assertEqual(trades[0]["exit"], 9.0)
        self.assertEqual(trades[0]["close_reason"], "sl_before_tp")
        self.assertEqual(metrics["execution"]["same_bar_sl_tp_policy"], "stop_loss_first")

    def test_spread_slippage_commission_and_swap_hooks_are_applied(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10.5, "low": 9.5, "close": 10},
            {"open": 10, "high": 12, "low": 9.5, "close": 11},
        ])
        calls = []

        def swap(context):
            calls.append(context.event)
            return 0.1

        trades, _, _ = run_backtest(
            df,
            SignalAtIndexes({1}),
            collect_equity=True,
            execution_mode="open_bar",
            execution_costs={"spread": 0.2, "slippage": 0.1, "commission": 0.2, "swap": swap},
        )
        self.assertAlmostEqual(trades[0]["entry"], 10.2)
        self.assertAlmostEqual(trades[0]["exit"], 10.8)
        self.assertAlmostEqual(trades[0]["R"], 0.2)
        self.assertEqual(calls, ["exit"])

    def test_break_even_stop_moves_only_on_next_bar(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10.5, "low": 9.5, "close": 10},
            {"open": 10, "high": 12, "low": 9.5, "close": 11},
            {"open": 10, "high": 10.5, "low": 9.5, "close": 10},
        ])
        trades, _, _ = run_backtest(
            df,
            SignalAtIndexes({1}, sl=8, tp=20),
            collect_equity=True,
            execution_mode="open_bar",
            allow_same_bar_entry=True,
            use_break_even=True,
            break_even_trigger=1.0,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["R"], 0.0)

    def test_warmup_is_configurable_and_short_data_is_valid(self):
        df = frame([
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
            {"open": 10, "high": 10, "low": 10, "close": 10},
        ])
        strategy = SignalAtIndexes(set())
        metrics = run_backtest(
            df, strategy, stats_only=True, warmup_bars=2, execution_mode="open_bar"
        )
        self.assertEqual(strategy.calls, [2])
        self.assertEqual(metrics["total_trades"], 0)
        empty = df.iloc[:0].copy()
        self.assertEqual(run_backtest(empty, SignalAtIndexes(set()), stats_only=True)["total_trades"], 0)

    def test_intrabar_mode_is_rejected_for_ohlc(self):
        df = frame([{"open": 10, "high": 10, "low": 10, "close": 10}])
        with self.assertRaisesRegex(ValueError, "intrabar OHLC"):
            run_backtest(df, SignalAtIndexes(set()), stats_only=True, execution_mode="intrabar")

    def test_dataset_reference_is_preserved_in_result(self):
        df = frame([{"open": 10, "high": 10, "low": 10, "close": 10}])
        reference = {"manifest": {"sha256": "test-hash"}, "split": {"name": "train"}}
        df.attrs["dataset_reference"] = reference
        metrics = run_backtest(df, SignalAtIndexes(set()), stats_only=True)
        self.assertEqual(metrics["dataset_reference"], reference)
        self.assertIsNot(metrics["dataset_reference"], reference)

    def test_data_manager_split_reference_is_carried_into_backtest_result(self):
        with tempfile.TemporaryDirectory(dir=project_root() / "data" / "raw") as directory:
            path = Path(directory) / "fixture.csv"
            source = frame([
                {"open": 10, "high": 11, "low": 9, "close": 10}
                for _ in range(72)
            ])
            source.to_csv(path, index=False)
            manager = DataManager(
                {
                    "symbol": "TEST", "timeframe": "H1", "source": "test", "venue": "test",
                    "timezone": "UTC", "data_kind": "raw",
                    "path": str(path.relative_to(project_root())).replace("\\", "/"),
                },
                date_splits={
                    "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-01-02T00:00:00Z",
                    "holdout_start": "2024-01-02T00:00:00Z", "holdout_end": "2024-01-04T00:00:00Z",
                },
            )
            manager.load()
            train = manager.get_train()
            metrics = run_backtest(train, SignalAtIndexes(set()), stats_only=True)
            self.assertEqual(metrics["dataset_reference"], manager.dataset_reference("train"))

    def test_precompute_uses_only_features_requested_by_params_or_grid(self):
        df = frame([
            {"open": 10, "high": 11, "low": 9, "close": 10}
            for _ in range(12)
        ])
        unchanged = precompute_for_params(df, {"lookback": 5, "sl_dollars": 10})
        self.assertEqual(list(unchanged.columns), list(df.columns))

        atr_only = precompute_for_params(df, {"atr_period": [2, 3], "lookback": [5]})
        self.assertIn("atr_2", atr_only)
        self.assertIn("atr_3", atr_only)
        self.assertNotIn("swing_high", atr_only)
        self.assertNotIn("trend", atr_only)

        with_trend = precompute_for_params(
            df, {"atr_period": 2, "swing_window": 2, "use_trend_filter": True}
        )
        self.assertIn("atr_2", with_trend)
        self.assertIn("swing_high", with_trend)
        self.assertIn("trend", with_trend)

    def test_friday_close_uses_close_of_bar_containing_utc_cutoff(self):
        timestamps = pd.to_datetime([
            "2024-01-04T20:00:00Z",
            "2024-01-04T21:00:00Z",
            "2024-01-05T20:00:00Z",
            "2024-01-05T21:00:00Z",
            "2024-01-08T00:00:00Z",
        ])
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": [10, 10, 10, 10, 12],
            "high": [10.5, 10.5, 10.5, 11, 12],
            "low": [9.5, 9.5, 9.5, 9.5, 12],
            "close": [10, 10, 10, 10.5, 12],
        })
        trades, _, metrics = run_backtest(
            df,
            SignalAtIndexes({1}, sl=5, tp=20),
            collect_equity=True,
            execution_mode="open_bar",
            close_positions_on_friday=True,
            friday_close_time_utc="22:00",
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["close_time"], timestamps[3])
        self.assertEqual(trades[0]["exit"], 10.5)
        self.assertEqual(trades[0]["close_reason"], "friday_close")
        self.assertTrue(metrics["execution"]["friday_close"]["enabled"])

        without_close, _, _ = run_backtest(
            df,
            SignalAtIndexes({1}, sl=5, tp=20),
            collect_equity=True,
            execution_mode="open_bar",
            close_positions_on_friday=False,
        )
        self.assertEqual(without_close, [])

    def test_gap_through_stop_explains_losses_below_minus_one_r(self):
        timestamps = pd.to_datetime([
            "2024-01-04T20:00:00Z",
            "2024-01-04T21:00:00Z",
            "2024-01-05T00:00:00Z",
        ])
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": [10, 10, 7], "high": [10, 10.5, 8],
            "low": [10, 9.5, 6], "close": [10, 10, 7],
        })
        trades, _, _ = run_backtest(
            df, SignalAtIndexes({1}, sl=9, tp=20), collect_equity=True,
            execution_mode="open_bar", execution_costs={"commission": 0.1},
        )
        self.assertEqual(trades[0]["close_reason"], "sl_gap")
        self.assertAlmostEqual(trades[0]["R"], -3.1)

if __name__ == "__main__":
    unittest.main()
