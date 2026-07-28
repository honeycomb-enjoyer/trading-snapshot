"""Compatibility aliases; edit master_config.py, not this module."""

from master_config import DATA_CONFIG, MONTE_CARLO_CONFIG, SPLIT_CONFIG, WALKFORWARD_CONFIG

# ============================================
# SPLIT MODE (CHANGE ONLY THIS LINE)
# ============================================
# Available modes:
# "manual" -> fixed dates
# "ratio"  -> percentage split
SPLIT_MODE = SPLIT_CONFIG["mode"]

# ============================================
# MANUAL SPLIT CONFIG
# Used when SPLIT_MODE = "manual"
# ============================================
DATE_SPLITS = SPLIT_CONFIG["dates"]

# ============================================
# RATIO SPLIT CONFIG
# Used when SPLIT_MODE = "ratio"
# Must sum to 1.0
# ============================================
RATIO_SPLITS = SPLIT_CONFIG["ratios"]

# ============================================
# WALK FORWARD CONFIG
# Used by:
# trading_research/overfit_tests/walkforward_test
#
# IMPORTANT:
# - train/test windows are in BARS (not months)
# - Since GER40 is H1:
#       24 * 30  = ~1 month
#       24 * 365 = ~1 year
#
# mode:
#   rolling  -> train window slides forward each step
#   anchored -> train always starts at train_start,
#               only expands forward
# ============================================
EXPERIMENT_CONFIG = {
    "monte_carlo_runs": MONTE_CARLO_CONFIG["simulations"],
    "random_seed": MONTE_CARLO_CONFIG["random_seed"],
}
