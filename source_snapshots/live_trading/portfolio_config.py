# ==========================================
# ACCOUNT STATE STORE (P0-T03)
# ==========================================
# This DB stores account-level risk state only; it must remain on the same
# local volume as the runtime and is intentionally ignored by Git.
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
ACCOUNT_STATE_DB_PATH = _PROJECT_ROOT / "runtime" / "account_state.sqlite3"
ACCOUNT_STATE_BUSY_TIMEOUT_MS = 1_000
ACCOUNT_STATE_LOCK_RETRY_ATTEMPTS = 4
ACCOUNT_STATE_LOCK_RETRY_BASE_SEC = 0.05


# ==========================================
# FEEDBACK SETTINGS
# ==========================================
HEARTBEAT_INTERVAL_SEC = 1200

# Telegram routing is operational policy.  Strategy trade/management messages
# stay in their strategy chats; the hub summary is sent once to the main chat.
ALERT_ROUTING = {
    "heartbeat": {"main": False, "strategy": True},
    "position_opened": {"main": False, "strategy": True},
    "position_closed": {"main": False, "strategy": True},
    "management": {"main": False, "strategy": True},
    "warning": {"main": False, "strategy": True},
    "critical": {"main": True, "strategy": True},
    "terminal": {"main": False, "strategy": False},
}


# ==========================================
# PORTFOLIO RISK MANAGEMENT
# ==========================================
MAX_OPEN_POSITIONS = 25

# Small operational tolerances, expressed as a fraction of one configured R.
# They absorb normal fill/rounding noise without turning the configured risk
# limits into soft suggestions.  At RISK_PER_TRADE_USD=30, 0.05R is $1.50.
POSITION_RISK_VALIDATION_TOLERANCE_R = 0.05
# A broker-confirmed adverse entry fill may increase an already-protected
# position's risk without indicating corrupt SL/volume state.  Such a position
# remains managed only when durable execution metadata proves the excess came
# from slippage and the total risk stays within this additional bound.
POSITION_RISK_SLIPPAGE_TOLERANCE_R = 0.50
DAILY_PROJECTED_LOSS_TOLERANCE_R = 0.05
WEEKLY_PROJECTED_LOSS_TOLERANCE_R = 0.01

# New entries are stress-tested with every open position moved to twice its
# current stop distance. Estimated margin includes a buffer for broker leverage
# tier changes; projected margin may consume at most half of the
# stressed account equity (equivalent to at least 200% margin level).
MAX_MARGIN_UTILIZATION = 0.50
MARGIN_STRESS_STOP_MULTIPLIER = 2.0
MARGIN_ESTIMATE_BUFFER = 1.25


# ==========================================
# SESSION BLOCK
# ==========================================
# All Friday/session values are interpreted in this explicit IANA timezone.
# MT5 tick timestamps are converted from UTC before comparison.  Do not use a
# workstation-local timezone here: it makes the same deployment behave
# differently after a host migration or DST transition.
SESSION_TIMEZONE = "UTC"

FRIDAY_NO_TRADE_HOUR = 21      # Original 21
FRIDAY_NO_TRADE_MINUTE = 30     # Original 30

FRIDAY_FORCE_CLOSE_HOUR = 22   # Original 22
FRIDAY_FORCE_CLOSE_MINUTE = 00 # Original 00


# ==========================================
# EXECUTION SAFETY
# ==========================================
EXECUTION_COOLDOWN_SEC = 2.0   # Original 2.0
PENDING_TIMEOUT_SEC = 5.0      # Original 5.0


# ==========================================
# MARKET GUARD
# ==========================================
# All price thresholds below are in *broker points*: `(ask - bid) / point`.
# A point is symbol-specific, so one global limit is unsafe.  Add an explicit
# symbol override whenever the broker's contract/suffix needs a different
# operational limit.
MARKET_GUARD_DEFAULT = {
    "max_spread_points": 50,
    "max_tick_age_sec": 30,
    "max_tick_jump_points": 500,
    "max_stale_price_sec": 120,
}
MARKET_GUARD_BY_ASSET = {
    "FX": {},
    # XAU/XAG normally have a coarser point and a wider quoted spread than FX.
    "METAL": {
        "max_spread_points": 500,
        "max_tick_jump_points": 1_000,
    },
}
MARKET_GUARD_BY_SYMBOL = {
    # Reserved for broker-specific operational limits, e.g. "XAUUSD.a".
}


def market_guard_settings(asset_class: str, symbol: str) -> dict:
    """Return explicit asset + symbol limits without guessing from a ticker."""
    asset = str(asset_class).strip().upper()
    normalized = str(symbol).upper()
    if asset not in MARKET_GUARD_BY_ASSET:
        raise ValueError(
            f"unknown market-guard asset_class {asset_class!r}; "
            f"configured: {sorted(MARKET_GUARD_BY_ASSET)}"
        )
    settings = dict(MARKET_GUARD_DEFAULT)
    settings.update(MARKET_GUARD_BY_ASSET[asset])
    settings.update(MARKET_GUARD_BY_SYMBOL.get(normalized, {}))
    return settings


# Backwards-compatible generic values. New code must call
# ``market_guard_settings(asset_class, symbol)`` instead of consuming these directly.
MAX_SPREAD_POINTS = MARKET_GUARD_DEFAULT["max_spread_points"]
MAX_TICK_AGE_SEC = MARKET_GUARD_DEFAULT["max_tick_age_sec"]
MAX_TICK_JUMP_POINTS = MARKET_GUARD_DEFAULT["max_tick_jump_points"]


# ==========================================
# BROKER
# ==========================================
MAX_RECONNECT_ATTEMPTS = 3     # Original 3
ORDER_DEVIATION_POINTS = 40    # Max fill deviation in price = deviation_points * symbol.point | Example (GER30): 20 * 0.1 = 2.0 index points

# All delays are explicit operational policy, not sleeps hidden in broker code.
# Reconnect is scheduled by the main loop and therefore never sleeps there.
RECONNECT_INITIAL_BACKOFF_SEC = 1.0
RECONNECT_BACKOFF_MULTIPLIER = 2.0
RECONNECT_MAX_BACKOFF_SEC = 15.0
RECONNECT_CIRCUIT_COOLDOWN_SEC = 60.0
BROKER_OPERATION_RETRY_ATTEMPTS = 3
BROKER_OPERATION_RETRY_BACKOFF_SEC = 0.2
BROKER_HISTORY_RETRY_ATTEMPTS = 2
BROKER_HISTORY_RETRY_BACKOFF_SEC = 0.2
BROKER_POSITION_VISIBILITY_POLL_SEC = 0.2
INTENT_HISTORY_LOOKBACK_SEC = 7 * 24 * 60 * 60
HISTORY_RECONCILIATION_BOOTSTRAP_DAYS = 30
HISTORY_RECONCILIATION_OVERLAP_SEC = 300
LOOP_IDLE_SLEEP_SEC = 0.05
