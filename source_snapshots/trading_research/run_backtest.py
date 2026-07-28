"""Compatibility entry point. Configuration lives in master_config.py."""

from master_config import BACKTEST_CONFIG, STRATEGY_PARAMS as PARAMS
from runners.backtest import main


if __name__ == "__main__":
    main()
