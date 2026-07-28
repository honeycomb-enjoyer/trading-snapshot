"""
Daily/weekly SL limit verification.

The old logic compared ONLY realized PnL against the limit:
    abs(realized) >= limit -> block
which lets a new trade open even when its own stop would breach the limit
(realized=$98.79, limit=$100, risk_per_trade=$30 -> projected $128.79).
The new logic blocks when PROJECTED loss (realized + risk_per_trade)
exceeds the limit.

Mock-driven tests:
  Daily guard:
    1. projected breaches limit      -> BLOCK  (-98.79, limit 100, risk 30)
    2. projected fits under limit    -> ALLOW  (-50,    limit 100, risk 30)
    3. profitable day                -> ALLOW  (+20,    limit 100)
    4. fallback when risk unset      -> legacy realized-only + warning
    5. exactly on boundary (> not >=) -> allowed (float noise tolerance)
  Weekly guard: same matrix (one repr case + fallback).
  Status snapshot mirrors can_open_new_trade (no divergence).
  Warning log contains realized / potential / projected / limit values.
  alerts=None (default) does not crash; alerts injected is called once.

Run:
    python tests/test_sl_limit.py
"""

import io
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

# Make the live_trading package importable when run as a script.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.risk_manager import RiskManager


# =====================================
# MOCKS
# =====================================
class FakeBroker:
    """broker_now() is the only method RiskManager touches."""
    def __init__(self, now):
        self._now = now

    def broker_now(self):
        return self._now


class FakeTradeLogger:
    """
    Returns precomputed daily/weekly PnL. Mirrors the real
    trade_logger.get_daily/weekly_strategy_pnl signature.
    """
    def __init__(self, daily_pnl=0.0, weekly_pnl=0.0):
        self.strategy_id = "test_strat"
        self.daily_pnl = daily_pnl
        self.weekly_pnl = weekly_pnl

    def get_daily_strategy_pnl(self, strategy_name, broker_now):
        return self.daily_pnl

    def get_weekly_strategy_pnl(self, strategy_name, broker_now):
        return self.weekly_pnl


class FakeAlerts:
    """Records send_warning calls so tests can assert routing."""
    def __init__(self):
        self.warnings = []

    def send_warning(self, message):
        self.warnings.append(message)


def make_config(
    daily_limit=None,
    weekly_limit=None,
    risk_per_trade=30,
    strategy_name="TEST_STRAT",
):
    """Build a minimal strategy config object (attribute-style, like the
    real configs). RISK_BUFFER / ALLOW_UNDERSIZED_LOT are required by
    PositionSizer init even though SL tests never size a position."""
    return SimpleNamespace(
        STRATEGY_NAME=strategy_name,
        DAILY_SL_LIMIT_USD=daily_limit,
        WEEKLY_SL_LIMIT_USD=weekly_limit,
        RISK_PER_TRADE_USD=risk_per_trade,
        RISK_BUFFER=0.98,
        ALLOW_UNDERSIZED_LOT=True,
        MAX_LOT=None,
    )


def make_manager(
    daily_pnl=0.0,
    weekly_pnl=0.0,
    daily_limit=None,
    weekly_limit=None,
    risk_per_trade=30,
    alerts=None,
):
    broker = FakeBroker(now=SimpleNamespace(date=lambda: None))
    logger = FakeTradeLogger(daily_pnl=daily_pnl, weekly_pnl=weekly_pnl)
    cfg = make_config(
        daily_limit=daily_limit,
        weekly_limit=weekly_limit,
        risk_per_trade=risk_per_trade,
    )
    return RiskManager(
        broker=broker,
        strategy_config=cfg,
        portfolio_config=None,
        trade_logger=logger,
        alerts=alerts,
    )


# =====================================
# DAILY GUARD TESTS
# =====================================
def test_daily_projected_breaches_limit_blocks():
    """
    The core bug from the task: realized=$98.79 < limit=$100, but the new
    trade's $30 risk would push projected to $128.79 -> must BLOCK.
    """
    rm = make_manager(
        daily_pnl=-98.79,
        daily_limit=100,
        risk_per_trade=30,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        allowed, reason = rm.can_open_new_trade()
    log = buf.getvalue()

    print(f"\n=== daily projected breach (-98.79, risk 30, limit 100) ===")
    print(f"  allowed={allowed} reason={reason}")
    print(f"  log: {log.strip()}")

    assert allowed is False, f"expected block, got allowed={allowed}"
    assert reason == "DAILY_LOSS_LIMIT_PROJECTED", (
        f"expected DAILY_LOSS_LIMIT_PROJECTED, got {reason}"
    )
    assert "[SL GUARD]" in log
    print("  PASS - daily projected breach blocked")


def test_daily_projected_fits_under_limit_allowed():
    """realized=$50 + risk=$30 = $80 < $100 -> ALLOW."""
    rm = make_manager(
        daily_pnl=-50.0,
        daily_limit=100,
        risk_per_trade=30,
    )
    allowed, reason = rm.can_open_new_trade()

    print(f"\n=== daily projected fits (-50, risk 30, limit 100) ===")
    print(f"  allowed={allowed} reason={reason}")
    assert allowed is True, f"expected allow, got block ({reason})"
    assert reason is None
    print("  PASS - daily projected under limit allowed")


def test_daily_projected_small_overrun_uses_fraction_of_one_r_tolerance():
    """The runtime allows ordinary fill noise, not another trade-sized loss."""
    rm = make_manager(
        daily_pnl=-30.50,
        daily_limit=60,
        risk_per_trade=30,
    )
    rm.portfolio_config = SimpleNamespace(
        MAX_OPEN_POSITIONS=None,
        DAILY_PROJECTED_LOSS_TOLERANCE_R=0.05,
        WEEKLY_PROJECTED_LOSS_TOLERANCE_R=0.0,
    )
    assert rm.can_open_new_trade() == (True, None)

    rm.trade_logger.daily_pnl = -59.0
    allowed, reason = rm.can_open_new_trade()
    assert allowed is False
    assert reason == "DAILY_LOSS_LIMIT_PROJECTED"


def test_daily_profitable_allowed():
    """A winning day (+$20) must never block on the SL guard."""
    rm = make_manager(
        daily_pnl=+20.0,
        daily_limit=100,
        risk_per_trade=30,
    )
    allowed, reason = rm.can_open_new_trade()

    print(f"\n=== daily profitable (+20, risk 30, limit 100) ===")
    print(f"  allowed={allowed} reason={reason}")
    assert allowed is True, f"profitable day blocked: {reason}"
    print("  PASS - profitable day allowed")


def test_daily_fallback_when_risk_unset_warning():
    """
    RISK_PER_TRADE_USD=None -> legacy realized-only check + a warning
    that explains the weaker guarantee (fallback tag in log).
    realized=$98.79 >= limit=$100? No (98.79 < 100) -> allowed under legacy,
    but if realized=$101 -> blocked with legacy reason + fallback tag.
    """
    # Under limit on legacy -> allowed, but the guard should NOT emit a
    # warning when nothing is blocked (no spam).
    rm_ok = make_manager(
        daily_pnl=-50.0,
        daily_limit=100,
        risk_per_trade=None,
    )
    allowed, reason = rm_ok.can_open_new_trade()
    print(f"\n=== daily fallback under limit (-50, risk None, limit 100) ===")
    print(f"  allowed={allowed} reason={reason}")
    assert allowed is True
    assert reason is None

    # Over limit on legacy -> blocked with legacy reason + fallback tag.
    rm_block = make_manager(
        daily_pnl=-101.0,
        daily_limit=100,
        risk_per_trade=None,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        allowed, reason = rm_block.can_open_new_trade()
    log = buf.getvalue()

    print(f"  allowed={allowed} reason={reason}")
    print(f"  log: {log.strip()}")
    assert allowed is False
    assert reason == "DAILY_LOSS_LIMIT", f"expected legacy reason, got {reason}"
    assert "fallback" in log, "expected fallback tag in warning"
    print("  PASS - daily fallback uses legacy realized-only + warning")


def test_daily_boundary_strict_greater_allowed():
    """
    projected == limit exactly (realized=70 + risk=30 = 100 == limit 100)
    must be ALLOWED: we use strict '>' so float noise on the boundary does
    not over-block. The limit is breached only when projected EXCEEDS it.
    """
    rm = make_manager(
        daily_pnl=-70.0,
        daily_limit=100,
        risk_per_trade=30,
    )
    allowed, reason = rm.can_open_new_trade()

    print(f"\n=== daily boundary (projected == limit, strict >) ===")
    print(f"  allowed={allowed} reason={reason}")
    assert allowed is True, (
        f"projected==limit should be allowed (strict >), got block ({reason})"
    )
    print("  PASS - boundary exactly at limit is allowed (strict >)")


# =====================================
# WEEKLY GUARD TESTS
# =====================================
def test_weekly_projected_breaches_limit_blocks():
    """Weekly mirror of the daily projected-breach case."""
    rm = make_manager(
        weekly_pnl=-130.0,
        weekly_limit=150,
        risk_per_trade=30,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        allowed, reason = rm.can_open_new_trade()
    log = buf.getvalue()

    print(f"\n=== weekly projected breach (-130, risk 30, limit 150) ===")
    print(f"  allowed={allowed} reason={reason}")
    print(f"  log: {log.strip()}")
    assert allowed is False
    assert reason == "WEEKLY_LOSS_LIMIT_PROJECTED", (
        f"expected WEEKLY_LOSS_LIMIT_PROJECTED, got {reason}"
    )
    assert "[SL GUARD] WEEKLY" in log
    print("  PASS - weekly projected breach blocked")


def test_weekly_fallback_when_risk_unset():
    """Weekly fallback to legacy realized-only when risk unset."""
    rm = make_manager(
        weekly_pnl=-160.0,
        weekly_limit=150,
        risk_per_trade=None,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        allowed, reason = rm.can_open_new_trade()
    log = buf.getvalue()

    print(f"\n=== weekly fallback over limit (-160, risk None, limit 150) ===")
    print(f"  allowed={allowed} reason={reason}")
    print(f"  log: {log.strip()}")
    assert allowed is False
    assert reason == "WEEKLY_LOSS_LIMIT", f"expected legacy reason, got {reason}"
    assert "fallback" in log
    print("  PASS - weekly fallback legacy realized-only + warning")


# =====================================
# STATUS SNAPSHOT MIRRORS GUARD
# =====================================
def test_status_snapshot_mirrors_can_open_new_trade():
    """
    Heartbeat reads status_snapshot()["daily_locked"]. It MUST agree with
    can_open_new_trade() - the bug was that the old status_snapshot used a
    different (realized-only) check and would show locked=False while the
    new guard was actually blocking (or vice versa).
    """
    rm = make_manager(
        daily_pnl=-98.79,
        daily_limit=100,
        risk_per_trade=30,
        weekly_pnl=-130.0,
        weekly_limit=150,
    )
    # status_snapshot must NOT print warnings (heartbeat calls it often).
    buf = io.StringIO()
    with redirect_stdout(buf):
        snap = rm.status_snapshot()
    log = buf.getvalue()

    print(f"\n=== status_snapshot mirrors guard ===")
    print(f"  snap={snap}")
    print(f"  log (must be empty): {log.strip()!r}")
    assert snap["daily_locked"] is True, (
        f"daily_locked should be True (projected breach), got {snap}"
    )
    assert snap["weekly_locked"] is True, (
        f"weekly_locked should be True (projected breach), got {snap}"
    )
    assert log == "", (
        "status_snapshot must not emit warnings (called from heartbeat)"
    )

    # And the same rm must actually block new trades.
    allowed, reason = rm.can_open_new_trade()
    assert allowed is False, "can_open_new_trade disagrees with snapshot"
    print("  PASS - snapshot and can_open_new_trade agree; no warning spam")


# =====================================
# WARNING CONTENT + ALERTS ROUTING
# =====================================
def test_warning_log_contains_all_values():
    """The [SL GUARD] line must include realized, potential, projected
    and limit so the block reason is self-explanatory."""
    rm = make_manager(
        daily_pnl=-98.79,
        daily_limit=100,
        risk_per_trade=30,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rm.can_open_new_trade()
    log = buf.getvalue()

    print(f"\n=== warning log content ===")
    print(f"  log: {log.strip()}")
    assert "realized=$98.79" in log
    assert "potential=$30.00" in log
    assert "projected=$128.79" in log
    assert "limit=$100.00" in log
    assert "DAILY_LOSS_LIMIT_PROJECTED" in log
    print("  PASS - log contains realized/potential/projected/limit")


def test_alerts_none_does_not_crash():
    """Default wiring (runners don't pass alerts) must not raise."""
    rm = make_manager(
        daily_pnl=-98.79,
        daily_limit=100,
        risk_per_trade=30,
        alerts=None,
    )
    # No exception expected.
    allowed, reason = rm.can_open_new_trade()
    assert allowed is False
    print(f"\n=== alerts=None ===\n  PASS - no crash, block reason={reason}")


def test_alerts_injected_called_once():
    """When alerts is provided, send_warning fires exactly once per block
    (not zero, not twice for daily+weekly when daily already blocks)."""
    alerts = FakeAlerts()
    rm = make_manager(
        daily_pnl=-98.79,   # daily alone blocks
        daily_limit=100,
        weekly_pnl=-130.0,  # weekly would also block, but daily wins
        weekly_limit=150,
        risk_per_trade=30,
        alerts=alerts,
    )
    allowed, reason = rm.can_open_new_trade()

    print(f"\n=== alerts injected (daily wins, single send) ===")
    print(f"  allowed={allowed} reason={reason}")
    print(f"  warnings={alerts.warnings}")
    assert allowed is False
    assert reason == "DAILY_LOSS_LIMIT_PROJECTED"
    assert len(alerts.warnings) == 1, (
        f"expected exactly 1 warning (short-circuit), got {len(alerts.warnings)}"
    )
    assert "DAILY_LOSS_LIMIT_PROJECTED" in alerts.warnings[0]
    print("  PASS - single warning sent (daily short-circuits weekly)")


# =====================================
# NO LIMIT CONFIGURED
# =====================================
def test_no_limit_never_blocks():
    """DAILY/WEEKLY_SL_LIMIT_USD=None (e.g. XAU, GER40) must never block
    and never emit a warning, even at large losses."""
    rm = make_manager(
        daily_pnl=-500.0,
        weekly_pnl=-1000.0,
        daily_limit=None,
        weekly_limit=None,
        risk_per_trade=30,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        allowed, reason = rm.can_open_new_trade()
        snap = rm.status_snapshot()
    log = buf.getvalue()

    print(f"\n=== no limit configured ===")
    print(f"  allowed={allowed} reason={reason} snap={snap}")
    print(f"  log (must be empty): {log.strip()!r}")
    assert allowed is True
    assert reason is None
    assert snap["daily_locked"] is False
    assert snap["weekly_locked"] is False
    assert log == ""
    print("  PASS - None limits never block or warn")


# =====================================
# BACKWARD COMPAT: signature without alerts
# =====================================
def test_signature_backward_compatible():
    """Existing runners construct RiskManager WITHOUT alerts kwarg.
    The 4-positional / 4-kwarg form must still work unchanged."""
    broker = FakeBroker(now=SimpleNamespace(date=lambda: None))
    logger = FakeTradeLogger(daily_pnl=0.0, weekly_pnl=0.0)
    cfg = make_config(daily_limit=100, weekly_limit=150, risk_per_trade=30)
    # Mirrors the runtime call sites.
    rm = RiskManager(
        broker=broker,
        strategy_config=cfg,
        portfolio_config=None,
        trade_logger=logger,
    )
    assert rm.alerts is None
    allowed, reason = rm.can_open_new_trade()
    assert allowed is True
    print(f"\n=== backward-compatible constructor ===\n  PASS - alerts=None default, no alerts kwarg needed")


# =====================================
# MAIN
# =====================================
if __name__ == "__main__":
    print("=" * 60)
    print("Daily/weekly SL limit verification")
    print("=" * 60)

    test_daily_projected_breaches_limit_blocks()
    test_daily_projected_fits_under_limit_allowed()
    test_daily_profitable_allowed()
    test_daily_fallback_when_risk_unset_warning()
    test_daily_boundary_strict_greater_allowed()
    test_weekly_projected_breaches_limit_blocks()
    test_weekly_fallback_when_risk_unset()
    test_status_snapshot_mirrors_can_open_new_trade()
    test_warning_log_contains_all_values()
    test_alerts_none_does_not_crash()
    test_alerts_injected_called_once()
    test_no_limit_never_blocks()
    test_signature_backward_compatible()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
