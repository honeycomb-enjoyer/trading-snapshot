"""
Position sizing verification: XAU x100 risk guard.

Mock-driven tests for the broker-aware position sizing fix:
  1. XAU on a nonstandard broker spec (tick_value per-ounce, no contract_size
     baked in) -> lot must be ~0.02, NOT ~2.71.
  2. XAU on a "good" standard broker (same real risk) -> identical lot.
  3. Forex symbol (EURUSD, stop=0.0010) -> correct lot (~0.3 for $30).
  4. Fallback path: order_calc_profit returns None -> manual tick formula.
  5. Diagnostic log contains all 5 values.
  6. Safety warning fires when formulas disagree >5x.

Run:
    python tests/test_position_sizing.py
"""

import io
import sys
import math
from contextlib import redirect_stdout

# Make the live_trading package importable when run as a script.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.position_sizer import PositionSizer


# =====================================
# MOCKS
# =====================================
class FakeSymbolInfo:
    """Mimics the MT5 SymbolInfo attributes the sizer reads."""

    def __init__(self, **kw):
        self.trade_tick_size = kw.get("tick_size", 0.01)
        self.trade_tick_value = kw.get("tick_value", 1.0)
        self.trade_contract_size = kw.get("contract_size", 100.0)
        self.volume_min = kw.get("volume_min", 0.01)
        self.volume_step = kw.get("volume_step", 0.01)
        self.volume_max = kw.get("volume_max", 100.0)


class FakeBroker:
    """
    Fake broker. `profit_per_lot` is what estimate_profit_per_lot returns
    (i.e. abs of MT5 order_calc_profit). Set to None to simulate the
    fallback path.
    """

    def __init__(self, symbol, symbol_info, profit_per_lot):
        self.symbol = symbol
        self._info = symbol_info
        self._profit_per_lot = profit_per_lot

    def get_symbol_info(self):
        return self._info

    def estimate_profit_per_lot(self, open_price, close_price):
        if self._profit_per_lot is None:
            return None
        return self._profit_per_lot


def run_case(name, broker, entry, stop, risk_usd, expected_lot,
             allow_undersized_lot=True):
    sizer = PositionSizer(
        broker=broker,
        risk_buffer=1.0,  # keep buffer out of expected-lot math
        allow_undersized_lot=allow_undersized_lot
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = sizer.calculate_lot(entry, stop, risk_usd)
    log = buf.getvalue()

    print(f"\n=== {name} ===")
    print(f"  result = {result}")
    print(f"  diagnostic log: {log.strip()}")

    assert result["valid"], f"{name}: expected valid, got {result}"
    lot = result["lot"]
    # Tolerance accounts for rounding down to volume_step (0.01).
    assert abs(lot - expected_lot) <= 0.01, (
        f"{name}: expected lot~{expected_lot}, got {lot}"
    )
    print(f"  PASS - lot={lot} (expected ~{expected_lot})")
    return result, log


# =====================================
# TESTS
# =====================================
def test_xau_nonstandard_symbol_spec():
    """
    Nonstandard symbol spec: tick_value=0.01 (per ounce, no contract_size).
    order_calc_profit returns the TRUE risk ($1094 for 1 lot at a
    $10.94 stop) -> lot must be ~0.02, NOT the buggy ~2.71.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=0.01,      # the bug: per-ounce value
        contract_size=100.0,
    )
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    # entry=2000.00, stop=1989.06 -> stop_distance=10.94
    run_case(
        "XAU nonstandard symbol spec (lot should be ~0.02)",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )


def test_xau_good_broker_identical():
    """
    Good broker: same true risk -> identical lot. Confirms the fix does
    not change behavior where the manual formula was already correct.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=1.0,        # contract_size already baked in
        contract_size=100.0,
    )
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    run_case(
        "XAU good broker (identical lot)",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )


def test_forex_eurusd():
    """
    Forex: EURUSD, stop=0.0010 (10 pips), $30 risk.
    True risk per lot ~ $10 -> lot ~ 3.0.
    """
    info = FakeSymbolInfo(
        tick_size=0.00001,
        tick_value=0.0001,
        contract_size=100000.0,
    )
    # 0.0010 / 0.00001 = 100 ticks * 0.0001 = $0.01?? -> the manual formula
    # would be wildly off; order_calc_profit gives the true ~$10/lot.
    broker = FakeBroker("EURUSD", info, profit_per_lot=10.0)
    run_case(
        "EURUSD forex (lot ~3.0 for $30 risk)",
        broker,
        entry=1.10000,
        stop=1.09900,           # 0.0010 distance
        risk_usd=30,
        expected_lot=3.0,
    )


def test_fallback_order_calc_profit_none():
    """
    Fallback: order_calc_profit unavailable (returns None).
    Sizer must fall back to the manual tick formula and still produce a
    sane lot. Using a "good" broker spec where the manual formula is
    correct (tick_value=1.0, contract_size baked in).
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=1.0,
        contract_size=100.0,
    )
    broker = FakeBroker("XAUUSD", info, profit_per_lot=None)
    result, log = run_case(
        "Fallback (order_calc_profit=None) -> manual formula",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )
    assert result["risk_source"] == "tick_formula", (
        f"expected fallback source, got {result['risk_source']}"
    )
    print(f"  PASS - risk_source={result['risk_source']}")


def test_diagnostic_log_has_all_values():
    """Diagnostic log must contain all 5 key values."""
    info = FakeSymbolInfo(tick_size=0.01, tick_value=1.0)
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    _, log = run_case(
        "Diagnostic log completeness",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )
    for token in ["symbol=", "tick_size=", "tick_value=",
                  "contract_size=", "stop_distance=", "risk_per_1_lot=",
                  "raw_lot="]:
        assert token in log, f"diagnostic log missing '{token}': {log!r}"
    print(f"  PASS - all diagnostic tokens present")


def test_safety_warning_fires_on_desync():
    """
    Safety check: when order_calc_profit and the manual formula disagree
    >5x (the bug scenario), a [LOTSIZE WARNING] line must be emitted.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=0.01,       # bad broker -> manual formula ~$10.94
        contract_size=100.0,
    )
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    sizer = PositionSizer(
        broker=broker, risk_buffer=1.0, allow_undersized_lot=True
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        sizer.calculate_lot(2000.00, 1989.06, 30)
    log = buf.getvalue()
    assert "[LOTSIZE WARNING]" in log, (
        f"expected desync warning, got: {log!r}"
    )
    print("\n=== Safety warning on desync ===")
    print(f"  log: {log.strip()}")
    print("  PASS - desync warning emitted")


def test_signatures_unchanged():
    """Backward-compat: public signatures must not have changed."""
    import inspect
    lot_sig = inspect.signature(PositionSizer.calculate_lot)
    assert list(lot_sig.parameters) == ["self", "entry_price",
                                        "stop_price", "risk_usd"], (
        f"calculate_lot signature changed: {lot_sig}"
    )
    # __init__ gained optional kwargs (max_lot, sanity_multiplier) with
    # defaults - existing callers passing only the old args still work.
    init_params = inspect.signature(PositionSizer.__init__).parameters
    for required in ["self", "broker", "risk_buffer",
                     "allow_undersized_lot"]:
        assert required in init_params, (
            f"__init__ lost required param '{required}'"
        )
    assert init_params["max_lot"].default is None
    assert init_params["sanity_multiplier"].default == 20
    print("\n=== Signatures ===")
    print("  PASS - PositionSizer signatures backward-compatible")


def test_sanity_check_blocks_x100_recurrence():
    """
    Future recurrence scenario on any symbol:
      - order_calc_profit returns a GARBAGE (too small) risk_per_1_lot
        -> raw_lot inflates (the x100 bug signature)
      - manual tick formula (good spec) gives a correct, small lot
    The two methods disagree in the DANGEROUS direction (inflated lot).
    The sanity net must BLOCK the order.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=1.0,          # GOOD spec -> manual formula correct
        contract_size=100.0,
    )
    # Garbage profit_per_lot -> raw_lot = 30/10.94 = 2.74 (x100 bug)
    # Manual formula (tick_value=1.0) -> manual_lot = 30/1094 = 0.027
    # 2.74 / 0.027 ~= 100x -> BLOCKED
    broker = FakeBroker("XAUUSD", info, profit_per_lot=10.94)
    sizer = PositionSizer(
        broker=broker, risk_buffer=1.0, allow_undersized_lot=True
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = sizer.calculate_lot(2000.00, 1989.06, 30)
    log = buf.getvalue()

    print("\n=== Sanity net blocks x100 recurrence ===")
    print(f"  result = {result}")
    print(f"  log: {log.strip()}")

    assert not result["valid"], (
        f"expected order BLOCKED, got valid result {result}"
    )
    assert result["reason"] == "ANOMALOUS_LOT", (
        f"expected ANOMALOUS_LOT, got {result['reason']}"
    )
    assert result["ratio"] >= 20, (
        f"expected ratio>=20, got {result['ratio']}"
    )
    assert "[LOTSIZE BLOCK]" in log
    print("  PASS - anomalous lot blocked (no order sent)")


def test_sanity_check_asymmetric_allows_smaller_lot():
    """
    The sanity net is asymmetric: a lot SMALLER than the manual estimate
    is safe (we just risk less) and must NOT be blocked. Confirms the
    net only trips on the dangerous (inflated) direction.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=0.01,         # bad spec -> manual formula tiny risk
        contract_size=100.0,
    )
    # order_calc_profit gives a LARGE risk_per_1_lot -> small raw_lot
    # manual formula (tick_value=0.01) -> large manual_lot
    # raw_lot << manual_lot -> safe direction, must NOT block
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    sizer = PositionSizer(
        broker=broker, risk_buffer=1.0, allow_undersized_lot=True,
        max_lot=None,
    )
    result, _ = run_case(
        "Sanity net asymmetric (smaller lot allowed)",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )
    assert result["valid"], f"smaller lot should not be blocked: {result}"
    print("  PASS - smaller lot not blocked (asymmetric net)")


def test_max_lot_ceiling_blocks():
    """
    Absolute MAX_LOT ceiling: a lot above the configured ceiling must
    be blocked (not silently capped), regardless of the sanity check.
    """
    info = FakeSymbolInfo(
        tick_size=0.01,
        tick_value=1.0,
        contract_size=100.0,
    )
    # Set MAX_LOT below the computed lot. risk_per_1_lot=1094, $30 risk
    # -> raw_lot 0.0274. With max_lot=0.01 that is below raw_lot -> block.
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    sizer = PositionSizer(
        broker=broker,
        risk_buffer=1.0,
        allow_undersized_lot=True,
        max_lot=0.01,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = sizer.calculate_lot(2000.00, 1989.06, 30)
    log = buf.getvalue()

    print("\n=== MAX_LOT ceiling blocks ===")
    print(f"  result = {result}")
    assert not result["valid"], f"expected block, got {result}"
    assert result["reason"] == "LOT_ABOVE_MAX", (
        f"expected LOT_ABOVE_MAX, got {result['reason']}"
    )
    assert "[LOTSIZE BLOCK]" in log
    print("  PASS - lot above MAX_LOT blocked")


def test_max_lot_none_backward_compatible():
    """
    When max_lot is None (default; strategies that don't define MAX_LOT),
    behavior is unchanged - no ceiling enforced.
    """
    info = FakeSymbolInfo(tick_size=0.01, tick_value=1.0)
    broker = FakeBroker("XAUUSD", info, profit_per_lot=1094.0)
    sizer = PositionSizer(
        broker=broker,
        risk_buffer=1.0,
        allow_undersized_lot=True,
        max_lot=None,  # default
    )
    result, _ = run_case(
        "max_lot=None (backward compat)",
        broker,
        entry=2000.00,
        stop=1989.06,
        risk_usd=30,
        expected_lot=0.02,
    )
    assert result["valid"]
    print("  PASS - no ceiling enforced when max_lot=None")


# =====================================
# MAIN
# =====================================
if __name__ == "__main__":
    print("=" * 60)
    print("XAU position sizing guard verification")
    print("=" * 60)

    test_xau_nonstandard_symbol_spec()
    test_xau_good_broker_identical()
    test_forex_eurusd()
    test_fallback_order_calc_profit_none()
    test_diagnostic_log_has_all_values()
    test_safety_warning_fires_on_desync()
    test_signatures_unchanged()
    test_sanity_check_blocks_x100_recurrence()
    test_sanity_check_asymmetric_allows_smaller_lot()
    test_max_lot_ceiling_blocks()
    test_max_lot_none_backward_compatible()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
