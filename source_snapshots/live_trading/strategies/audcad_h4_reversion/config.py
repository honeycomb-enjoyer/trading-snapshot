# strategies/audcad_h4_reversion/config.py
#
# Identity (SYMBOL / MAGIC / ACCOUNT) is sourced from strategies.yaml via
# the shared registry  - there is exactly one place to edit it.
# Everything below the identity block is BEHAVIOR/TUNING owned by this
# strategy: timeframe, indicator periods, SL/TP models, risk, MAX_LOT.

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
SIGNAL_TIMEFRAME = "H4"      # Baseline H4
RANGE_LOOKBACK = 16          # Baseline 16
ATR_PERIOD = 20              # Baseline 20

# ==========================================
# UNIVERSAL TRADE STRUCTURE
# ==========================================

# Stop loss = ATR(20) * 1.0
STOP_LOSS_MODEL = "ATR_MULTIPLIER"
STOP_LOSS = 1.0              # Baseline 1.0

# Take profit = fraction of move from entry
# toward range midpoint
TAKE_PROFIT_MODEL = "CUSTOM"
TAKE_PROFIT = 1.0            # Baseline 0.4 (of mean target)

# ==========================================
# EXECUTION TOLERANCE
# ==========================================
MAX_SLIPPAGE_AS_STOP_FRACTION = 0.25
ORDER_RETRY_COOLDOWN_SEC = 3

# ==========================================
# BREAK EVEN
# ==========================================
USE_BREAK_EVEN = False
BREAK_EVEN_MODEL = "R_MULTIPLE"
BREAK_EVEN_TRIGGER = 0.0
BREAK_EVEN_OFFSET = 0.0

# ==========================================
# RISK ENGINE
# ==========================================
RISK_PER_TRADE_USD = 50
DAILY_SL_LIMIT_USD = 100       # Baseline 2R
WEEKLY_SL_LIMIT_USD = 200     # Baseline 4R

RISK_BUFFER = 0.98
ALLOW_UNDERSIZED_LOT = True

# HARD SAFETY NET: absolute lot ceiling for this symbol.
# Normal AUDCAD lot at $30 risk is ~0.3–0.5; 5.0 gives generous headroom
# while blocking any x100-style sizing bug. PositionSizer blocks (does
# not cap) when raw_lot exceeds this.
MAX_LOT = 5.0

# ==========================================
# PRE ALERT
# ==========================================
PRE_ALERT_DISTANCE_POINTS = 3
