from datetime import datetime, timezone

from data.download_from_mt5 import download_data
from master_config import DOWNLOAD_CONFIG


def main():
    config = DOWNLOAD_CONFIG
    end = config["end"] if config["end"] is not None else datetime.now(timezone.utc)
    return download_data(
        symbol=config["symbol"],
        timeframe_str=config["timeframe"],
        start=config["start"],
        end=end,
        save_path=config["save_path"],
        venue=config["venue"],
        broker_time_profile=config["broker_time_profile"],
    )


if __name__ == "__main__":
    main()
