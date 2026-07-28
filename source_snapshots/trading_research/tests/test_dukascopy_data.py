import unittest

import pandas as pd

from data.download_from_dukascopy import (
    DukascopyDataError,
    _instrument_code,
    build_timeframe,
    decode_minute_payload,
    repair_tiny_ohlc_violations,
)


def compressed_fixture(periods=2):
    return {
        "timestamp": 1609459200000,
        "multiplier": 0.00001,
        "open": 1.00000,
        "high": 1.00020,
        "low": 0.99980,
        "close": 1.00010,
        "shift": 60000,
        "times": [0] + [1] * (periods - 1),
        "opens": [0] * periods,
        "highs": [0] * periods,
        "lows": [0] * periods,
        "closes": [0] * periods,
        "volumes": [1.25] * periods,
    }


class DukascopyDataTests(unittest.TestCase):
    def test_instrument_code_accepts_common_fx_spellings(self):
        self.assertEqual(_instrument_code("AUDCAD"), "AUD-CAD")
        self.assertEqual(_instrument_code("aud/cad"), "AUD-CAD")

    def test_delta_compressed_m1_payload_is_decoded(self):
        frame = decode_minute_payload(compressed_fixture())
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame["timestamp"].iloc[0], pd.Timestamp("2021-01-01T00:00:00Z"))
        self.assertAlmostEqual(frame["high"].iloc[0], 1.00020)
        self.assertEqual(frame["tick_volume"].iloc[0], 1_250_000)

    def test_decoder_rejects_inconsistent_arrays(self):
        payload = compressed_fixture()
        payload["closes"] = []
        with self.assertRaisesRegex(DukascopyDataError, "inconsistent"):
            decode_minute_payload(payload)

    def test_h4_uses_new_york_close_baseline_in_winter(self):
        frame = decode_minute_payload(compressed_fixture(periods=480))
        frame["timestamp"] = pd.date_range("2021-01-03T22:00:00Z", periods=480, freq="min")
        h4 = build_timeframe(frame, "H4", frame["timestamp"].iloc[0], pd.Timestamp("2021-01-04T06:00:00Z"))
        self.assertEqual(list(h4["timestamp"]), [
            pd.Timestamp("2021-01-03T22:00:00Z"),
            pd.Timestamp("2021-01-04T02:00:00Z"),
        ])
        self.assertEqual(list(h4["tick_volume"]), [300_000_000, 300_000_000])

    def test_h4_and_d1_follow_new_york_dst_in_summer(self):
        frame = decode_minute_payload(compressed_fixture(periods=480))
        frame["timestamp"] = pd.date_range("2021-07-04T21:00:00Z", periods=480, freq="min")
        h4 = build_timeframe(frame, "H4", frame["timestamp"].iloc[0], pd.Timestamp("2021-07-05T05:00:00Z"))
        d1 = build_timeframe(frame, "D1", frame["timestamp"].iloc[0], pd.Timestamp("2021-07-06T21:00:00Z"))
        self.assertEqual(list(h4["timestamp"]), [
            pd.Timestamp("2021-07-04T21:00:00Z"),
            pd.Timestamp("2021-07-05T01:00:00Z"),
        ])
        self.assertEqual(d1["timestamp"].iloc[0], pd.Timestamp("2021-07-04T21:00:00Z"))

    def test_tiny_ohlc_violation_is_repaired_but_large_one_fails(self):
        frame = decode_minute_payload(compressed_fixture())
        frame.loc[0, "high"] = frame.loc[0, "close"] - 0.00001
        repaired, stats = repair_tiny_ohlc_violations(frame, max_adjustment=0.0001)
        self.assertEqual(repaired.loc[0, "high"], repaired.loc[0, "close"])
        self.assertEqual(stats["high_rows"], 1)
        frame.loc[0, "high"] = frame.loc[0, "close"] - 0.001
        with self.assertRaisesRegex(DukascopyDataError, "exceeds"):
            repair_tiny_ohlc_violations(frame, max_adjustment=0.0001)


if __name__ == "__main__":
    unittest.main()
