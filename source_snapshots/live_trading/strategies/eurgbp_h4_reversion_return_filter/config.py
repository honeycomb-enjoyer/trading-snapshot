# strategies/eurgbp_h4_reversion_return_filter/config.py
#
# Identity (SYMBOL / MAGIC / ACCOUNT) is sourced from strategies.yaml via
# the shared registry  - there is exactly one place to edit it.
# Everything below the identity block is BEHAVIOR/TUNING owned by this
# strategy: timeframe, indicator periods, return filter, risk, MAX_LOT.

from shared.registry import registry as _registry
_strategy_id = __name__.split(".")[-2]
_meta = _registry.get_strategy(_strategy_id)

# ==========================================
# STRATEGY IDENTITY  (single source: strategies.yaml)
# ==========================================
STRATEGY_NAME = _strategy_id.upper()
SYMBOL = _meta["symbol"]
ASSET_CLASS = _meta["asset_class"]
MAGIC = _meta["magic"]
ACCOUNT = _meta["account"]

# ==========================================
# ACCOUNT REFERENCE
# ==========================================
# Credentials are resolved from secret_config.ACCOUNTS[ACCOUNT] at runtime.
# Account metadata lives in accounts.py. Never put passwords here.

# ==========================================
# STRATEGY INTERNAL PARAMETERS
# (used only by strategy.py)
# ==========================================
SIGNAL_TIMEFRAME = "H4"              # Baseline H4
RANGE_LOOKBACK = 12                  # Baseline 12
ATR_PERIOD = 20                      # Baseline 20
DIRECTION = "both"                   # Baseline both
USE_RETURN_FILTER = True             # Baseline True
RETURN_FILTER_TIMEFRAME = "W1"       # Baseline W1
RETURN_FILTER_MODE = "reversion"     # Baseline reversion
FILTER_TIMEZONE = "America/New_York"
FILTER_ROLLOVER_HOUR = 17

# ==========================================
# UNIVERSAL TRADE STRUCTURE
# ==========================================

# Stop loss = ATR(20) * 2.0
STOP_LOSS_MODEL = "ATR_MULTIPLIER"
STOP_LOSS = 2.0                      # Baseline 2.0

# Take profit = 0.25 of the path from entry toward range midpoint.
TAKE_PROFIT_MODEL = "CUSTOM"
TAKE_PROFIT = 0.25                   # Baseline 0.25 (of mean target)

# ==========================================
# EXECUTION TOLERANCE
# ==========================================
MAX_SLIPPAGE_AS_STOP_FRACTION = 0.25
ORDER_RETRY_COOLDOWN_SEC = 3

# ==========================================
# BREAK EVEN
# ==========================================
USE_BREAK_EVEN = False               # Baseline False
BREAK_EVEN_MODEL = "R_MULTIPLE"
BREAK_EVEN_TRIGGER = 0.0             # Baseline 0.0
BREAK_EVEN_OFFSET = 0.0              # Baseline 0.0

# ==========================================
# RISK ENGINE
# ==========================================
RISK_PER_TRADE_USD = 30
DAILY_SL_LIMIT_USD = None            # Baseline None
WEEKLY_SL_LIMIT_USD = 120            # Baseline 4R

RISK_BUFFER = 0.98
ALLOW_UNDERSIZED_LOT = True

# HARD SAFETY NET: absolute lot ceiling for this symbol.
# PositionSizer blocks (does not cap) when raw_lot exceeds this value.
MAX_LOT = 5.0

# ==========================================
# PRE ALERT
# ==========================================
PRE_ALERT_DISTANCE_POINTS = 3
