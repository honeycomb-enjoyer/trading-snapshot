import unittest

import pandas as pd

from data.schema import DataContractError
from data.time_normalization import BrokerTimeProfile, normalize_mt5_timestamps


METAQUOTES_DEMO = {
    "name": "metaquotes_demo_new_york_close",
    "mode": "server_wall_clock",
    "standard_utc_offset_hours": 2,
    "dst_utc_offset_hours": 3,
    "dst_reference_timezone": "America/New_York",
}


class BrokerTimeNormalizationTests(unittest.TestCase):
    def test_metaquotes_winter_and_summer_offsets_are_applied_per_date(self):
        profile = BrokerTimeProfile.from_mapping(METAQUOTES_DEMO)
        result = normalize_mt5_timestamps(
            pd.Series(["2026-01-12T07:00:00Z", "2026-07-13T07:00:00Z"]), profile,
        )
        self.assertEqual(result.iloc[0], pd.Timestamp("2026-01-12T05:00:00Z"))
        self.assertEqual(result.iloc[1], pd.Timestamp("2026-07-13T04:00:00Z"))

    def test_utc_profile_does_not_shift_a_compliant_broker(self):
        profile = BrokerTimeProfile.from_mapping({"name": "utc", "mode": "utc"})
        result = normalize_mt5_timestamps(pd.Series([1_700_000_000]), profile)
        self.assertEqual(result.iloc[0].timestamp(), 1_700_000_000)

    def test_ambiguous_server_profile_is_rejected(self):
        with self.assertRaisesRegex(DataContractError, "both"):
            BrokerTimeProfile.from_mapping({
                "name": "bad", "mode": "server_wall_clock",
                "standard_utc_offset_hours": 2, "dst_utc_offset_hours": 3,
            })


if __name__ == "__main__":
    unittest.main()
