import unittest
from unittest.mock import patch

from runners import data_manager


class DataRunnerTests(unittest.TestCase):
    def test_root_data_runner_reads_download_config(self):
        config = {
            "symbol": "TEST",
            "timeframe": "H1",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-02-01T00:00:00Z",
            "save_path": "data/raw/test.csv",
            "venue": "test-broker",
            "broker_time_profile": {"name": "utc", "mode": "utc"},
        }
        with patch.dict(data_manager.DOWNLOAD_CONFIG, config, clear=True), patch(
            "runners.data_manager.download_data", return_value="downloaded"
        ) as download:
            self.assertEqual(data_manager.main(), "downloaded")
        download.assert_called_once_with(
            symbol="TEST",
            timeframe_str="H1",
            start="2024-01-01T00:00:00Z",
            end="2024-02-01T00:00:00Z",
            save_path="data/raw/test.csv",
            venue="test-broker",
            broker_time_profile={"name": "utc", "mode": "utc"},
        )


if __name__ == "__main__":
    unittest.main()
