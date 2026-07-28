import os
import tempfile
import unittest
import warnings
from pathlib import Path

import pandas as pd

from data.data_manager import DataManager
from data.manifest import build_manifest, verify_manifest, write_manifest
from data.schema import DataContractError, DataContractWarning, DatasetContract, normalize_and_validate, project_root
from data.download_from_mt5 import _timeframe_map


CONTRACT_CONFIG = {
    "symbol": "TEST",
    "timeframe": "H1",
    "source": "test",
    "venue": "test",
    "timezone": "UTC",
    "data_kind": "raw",
}
DATE_SPLITS = {
    "train_start": "2024-01-01T00:00:00Z", "train_end": "2024-01-02T00:00:00Z",
    "holdout_start": "2024-01-02T00:00:00Z", "holdout_end": "2024-01-04T00:00:00Z",
}


def valid_frame(periods=72):
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=periods, freq="h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
    })


class DataContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = DatasetContract.from_mapping(CONTRACT_CONFIG)

    def test_timezone_aware_input_normalizes_to_utc(self):
        frame = valid_frame(2)
        frame["timestamp"] = ["2024-01-01T03:00:00+03:00", "2024-01-01T04:00:00+03:00"]
        normalized = normalize_and_validate(frame, self.contract)
        self.assertEqual(str(normalized["timestamp"].iloc[0]), "2024-01-01 00:00:00+00:00")

    def test_weekly_timeframe_is_supported_and_inferred_from_filename(self):
        contract = DatasetContract.from_mapping({
            "path": "data/raw/mt5/XAUUSD_W1_20210101_20260715_UTC.csv",
        })
        self.assertEqual(contract.symbol, "XAUUSD")
        self.assertEqual(contract.timeframe, "W1")
        self.assertEqual(contract.interval, pd.Timedelta(weeks=1))
        self.assertGreaterEqual(contract.max_non_trading_gap, contract.interval)

        class FakeMT5:
            TIMEFRAME_M1 = 1
            TIMEFRAME_M5 = 5
            TIMEFRAME_M15 = 15
            TIMEFRAME_M30 = 30
            TIMEFRAME_H1 = 60
            TIMEFRAME_H4 = 240
            TIMEFRAME_D1 = 1440
            TIMEFRAME_W1 = 10080

        self.assertEqual(_timeframe_map(FakeMT5)["W1"], 10080)

    def test_rejects_duplicates_and_out_of_order_rows(self):
        duplicate = valid_frame(2)
        duplicate.loc[1, "timestamp"] = duplicate.loc[0, "timestamp"]
        with self.assertRaisesRegex(DataContractError, "Duplicate"):
            normalize_and_validate(duplicate, self.contract)
        unordered = valid_frame(3).iloc[[1, 0, 2]]
        with self.assertRaisesRegex(DataContractError, "strictly increasing"):
            normalize_and_validate(unordered, self.contract)

    def test_rejects_invalid_ohlc_and_missing_columns(self):
        invalid_ohlc = valid_frame(2)
        invalid_ohlc.loc[0, "high"] = 8.0
        with self.assertRaisesRegex(DataContractError, "OHLC"):
            normalize_and_validate(invalid_ohlc, self.contract)
        with self.assertRaisesRegex(DataContractError, "missing required"):
            normalize_and_validate(valid_frame(2).drop(columns="close"), self.contract)

    def test_suspicious_gap_warns_by_default_and_strict_mode_rejects(self):
        gapped = valid_frame(3).iloc[[0, 2]].copy()
        gapped.loc[gapped.index[-1], "timestamp"] = pd.Timestamp("2024-01-01T04:00:00Z")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            normalized = normalize_and_validate(gapped, self.contract)
        self.assertEqual(len(normalized), 2)
        self.assertTrue(any(issubclass(item.category, DataContractWarning) for item in caught))
        strict_contract = DatasetContract.from_mapping({**CONTRACT_CONFIG, "gap_policy": "error"})
        with self.assertRaisesRegex(DataContractError, "Suspicious gap"):
            normalize_and_validate(gapped, strict_contract)


class DataManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir=project_root() / "data" / "raw")
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "fixture.csv"
        valid_frame().to_csv(self.path, index=False)
        self.relative_path = str(self.path.relative_to(project_root())).replace("\\", "/")

    def manager(self, **overrides):
        config = {**CONTRACT_CONFIG, "path": self.relative_path, **overrides}
        return DataManager(config, date_splits=DATE_SPLITS)

    def test_manual_end_is_exclusive_and_includes_final_intraday_day(self):
        manager = self.manager()
        manager.load()
        manager.validate()
        train = manager.get_train()
        self.assertEqual(len(train), 24)
        self.assertEqual(train["timestamp"].iloc[-1], pd.Timestamp("2024-01-01T23:00:00Z"))
        self.assertEqual(len(manager.get_holdout()), 48)

    def test_rejects_overlapping_manual_splits(self):
        overlapping = dict(DATE_SPLITS, holdout_start="2024-01-01T12:00:00Z")
        manager = DataManager({**CONTRACT_CONFIG, "path": self.relative_path}, date_splits=overlapping)
        manager.load()
        with self.assertRaisesRegex(DataContractError, "Split overlap"):
            manager.validate()

    def test_manifest_hash_detects_mutation(self):
        manager = self.manager()
        frame = manager.load()
        manifest = build_manifest(self.relative_path, frame, manager.contract, retrieved_at="2024-01-04T00:00:00Z")
        write_manifest(manifest, self.relative_path)
        verify_manifest(self.path, manifest)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("2024-01-04T00:00:00Z,10,11,9,10.5\n")
        with self.assertRaisesRegex(ValueError, "hash"):
            verify_manifest(self.path, manifest)

    def test_relative_dataset_path_works_outside_repository_cwd(self):
        previous_cwd = Path.cwd()
        try:
            os.chdir(project_root().parent)
            manager = self.manager()
            self.assertEqual(len(manager.load()), 72)
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
