import tempfile
import unittest
import warnings
from pathlib import Path

import pandas as pd

from data.normalize_market_data import (
    MarketDataNormalizationError,
    load_histdata_m1,
    normalize_m1_sources,
)


def minute_frame(periods=180, *, scale=1.0):
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=periods, freq="min")
    close = pd.Series([scale * (1.0 + position * 0.000001) for position in range(periods)])
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": close - scale * 0.000001,
        "high": close + scale * 0.000002,
        "low": close - scale * 0.000002,
        "close": close,
        "tick_volume": 1.0,
    })


class HistDataLoaderTests(unittest.TestCase):
    def test_new_york_time_uses_winter_and_summer_offsets_and_removes_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "AUDCAD"
            source.mkdir()
            (source / "DAT_MT_AUDCAD_M1_2024.csv").write_text(
                "2024.01.01,17:00,1.00000,1.00020,0.99980,1.00010,0\n"
                "2024.01.01,17:00,1.00000,1.00020,0.99980,1.00010,0\n"
                "2024.01.01,17:01,1.00010,1.00030,1.00000,1.00020,0\n"
                "2024.07.01,17:00,1.10000,1.10020,1.09980,1.10010,0\n",
                encoding="ascii",
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                frame, metadata = load_histdata_m1(
                    "AUDCAD",
                    pd.Timestamp("2024-01-01T00:00:00Z"),
                    pd.Timestamp("2024-07-03T00:00:00Z"),
                    directory,
                )
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame["timestamp"].iloc[0], pd.Timestamp("2024-01-01T22:00:00Z"))
        self.assertEqual(frame["timestamp"].iloc[-1], pd.Timestamp("2024-07-01T21:00:00Z"))
        self.assertEqual(metadata["identical_duplicate_rows_removed"], 1)

    def test_conflicting_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "AUDCAD"
            source.mkdir()
            (source / "DAT_MT_AUDCAD_M1_2024.csv").write_text(
                "2024.01.01,17:00,1.00000,1.00020,0.99980,1.00010,0\n"
                "2024.01.01,17:00,1.10000,1.10020,1.09980,1.10010,0\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(MarketDataNormalizationError, "Conflicting HistData duplicate"):
                load_histdata_m1(
                    "AUDCAD",
                    pd.Timestamp("2024-01-01T00:00:00Z"),
                    pd.Timestamp("2024-01-03T00:00:00Z"),
                    directory,
                )


class DualSourceNormalizationTests(unittest.TestCase):
    def test_missing_primary_minute_is_filled_from_locally_aligned_secondary(self):
        primary = minute_frame().drop(index=90).reset_index(drop=True)
        secondary = minute_frame(scale=0.999)
        normalized, audit, report = normalize_m1_sources(primary, secondary, "AUDCAD")
        timestamp = pd.Timestamp("2024-01-01T01:30:00Z")
        restored = normalized.set_index("timestamp").loc[timestamp]
        self.assertEqual(len(normalized), 180)
        self.assertAlmostEqual(restored["close"], 1.00009, places=6)
        self.assertEqual(audit.iloc[0]["action"], "fill_missing_primary_bar")
        self.assertEqual(report["missing_primary_bars_filled"], 1)

    def test_isolated_primary_spike_is_replaced_but_persistent_difference_is_not(self):
        primary = minute_frame()
        secondary = minute_frame(scale=0.999)
        primary.loc[90, ["open", "high", "low", "close"]] += 0.01
        primary.loc[120:125, ["open", "high", "low", "close"]] += 0.01
        normalized, audit, report = normalize_m1_sources(primary, secondary, "AUDCAD")
        normalized = normalized.set_index("timestamp")
        isolated = pd.Timestamp("2024-01-01T01:30:00Z")
        persistent = pd.Timestamp("2024-01-01T02:02:00Z")
        self.assertAlmostEqual(normalized.loc[isolated, "close"], 1.00009, places=6)
        self.assertGreater(normalized.loc[persistent, "close"], 1.01)
        self.assertEqual(report["isolated_primary_spikes_replaced"], 1)
        self.assertIn("replace_isolated_primary_spike", set(audit["action"]))

    def test_persistent_primary_defect_is_replaced_when_histdata_and_mt5_agree(self):
        primary = minute_frame()
        secondary = minute_frame()
        reference_m1 = minute_frame()
        primary.loc[60:69, ["open", "high", "low", "close"]] += 0.01
        reference_m1["m5_timestamp"] = reference_m1["timestamp"].dt.floor("5min")
        reference_m5 = reference_m1.groupby("m5_timestamp", as_index=False).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
        }).rename(columns={"m5_timestamp": "timestamp"})

        normalized, audit, report = normalize_m1_sources(
            primary,
            secondary,
            "AUDCAD",
            reference_m5=reference_m5,
        )

        normalized = normalized.set_index("timestamp")
        self.assertAlmostEqual(normalized.loc[pd.Timestamp("2024-01-01T01:02:00Z"), "close"], 1.000062, places=6)
        self.assertEqual(report["reference_confirmed_m5_blocks_replaced"], 2)
        self.assertEqual(report["reference_confirmed_primary_bars_replaced"], 10)
        self.assertEqual(
            set(audit.loc[audit["action"].eq("replace_primary_bar_confirmed_by_mt5"), "timestamp"]),
            set(pd.date_range("2024-01-01T01:00:00Z", periods=10, freq="min")),
        )

    def test_m30_reference_can_confirm_persistent_primary_defect(self):
        primary = minute_frame(periods=700)
        secondary = minute_frame(periods=700)
        reference_m1 = minute_frame(periods=700)
        primary.loc[60:89, ["open", "high", "low", "close"]] += 0.01
        reference_m1["m30_timestamp"] = reference_m1["timestamp"].dt.floor("30min")
        reference_m30 = reference_m1.groupby("m30_timestamp", as_index=False).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
        }).rename(columns={"m30_timestamp": "timestamp"})

        normalized, audit, report = normalize_m1_sources(
            primary,
            secondary,
            "AUDCAD",
            reference_bars=("M30", reference_m30),
        )

        normalized = normalized.set_index("timestamp")
        self.assertAlmostEqual(normalized.loc[pd.Timestamp("2024-01-01T01:15:00Z"), "close"], 1.000075, places=6)
        self.assertEqual(report["reference_timeframe"], "M30")
        self.assertEqual(report["reference_confirmed_blocks_replaced"], 1)
        self.assertEqual(report["reference_confirmed_primary_bars_replaced"], 30)
        self.assertEqual(
            set(audit.loc[audit["action"].eq("replace_primary_bar_confirmed_by_mt5"), "timestamp"]),
            set(pd.date_range("2024-01-01T01:00:00Z", periods=30, freq="min")),
        )


if __name__ == "__main__":
    unittest.main()
