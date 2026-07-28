"""Compatibility entry point. Configuration lives in master_config.py."""

from master_config import OPTIMIZER_CONFIG
from runners.optimizer import main

PARAM_GRID = OPTIMIZER_CONFIG["param_grid"]
EXECUTION_CONFIG = OPTIMIZER_CONFIG["execution_grid"]

if __name__ == "__main__":
    main()
