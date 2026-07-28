"""
Stats sync verification: heartbeat + trades.csv completeness.

Two problems fixed:
  A) heartbeat._stats_status() falsely reported "Synced" when execution_cache
     was incomplete or trades.csv had missing enriched fields.
  B) _record_close_analytics wrote pnl_r=0.0 when risk_usd was unknown,
     masking data loss as a genuine breakeven close.

Heartbeat _stats_status (operator-facing: SYNCED / WARNING):
  1.  state is None                    -> WARNING
  2.  execution_cache not a dict       -> WARNING
  3.  cache entry not a dict           -> WARNING
  4.  cache entry missing mandatory key -> WARNING
  5.  empty cache, no closed trades    -> SYNCED
  6.  full cache, no closed trades      -> SYNCED
  7.  last trade: all numeric enriched   -> SYNCED
  8.  last trade: has N/A fields        -> SYNCED
  9.  last trade: blank enriched field   -> WARNING
  10. last trade: None enriched field   -> WARNING
  11. exception in _stats_status        -> WARNING

TradeLogger _append_csv sentinel (centralised NA conversion):
  12. None enriched field  -> "N/A" in CSV
  13. Numeric enriched field -> value preserved in CSV
  14. None non-enriched field (pnl_usd) -> empty, NOT "N/A"

PositionManager _record_close_analytics (r_multiple initialisation):
  15. No risk_usd in cache -> pnl_r=None (alert still gets 0.0)
  16. With risk_usd        -> pnl_r computed as float

Run:
    python tests/test_trade_stats_sync.py
"""

import csv
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# Make the live_trading package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitoring.heartbeat import Heartbeat
from analytics.trade_logger import TradeLogger, NA, ENRICHED_NA_FIELDS
from analytics.trade_store import TradeStore
from core.position_manager import PositionManager
from core import position_manager as position_manager_module

import analytics.trade_logger as _tl_mod


# =====================================
# MOCKS - heartbeat
# =====================================

class FakeStateManager:
    """Minimal state_manager mock with a programmable .state dict."""
    def __init__(self, state=None):
        self.state = state or {}


class FakeTradeLogger:
    """Minimal trade_logger mock with programmable get_last_closed_trade."""
    def __init__(self, last_trade=None):
        self._last_trade = last_trade

    def get_last_closed_trade(self):
        return self._last_trade


class FakeAlerts:
    def __init__(self):
        self.info = []
        self.warnings = []
        self.critical = []

    def send_info(self, m):
        self.info.append(m)

    def send_warning(self, m):
        self.warnings.append(m)

    def send_critical(self, m):
        self.critical.append(m)

    def alert_heartbeat(self, m):
        pass

    def alert_position_closed(self, **kw):
        pass


_dummy = SimpleNamespace()


def make_heartbeat(state, last_trade=None):
    """Build a Heartbeat with only the deps _stats_status touches."""
    return Heartbeat(
        broker=_dummy,
        market_guard=_dummy,
        session_guard=_dummy,
        execution_guard=_dummy,
        risk_manager=_dummy,
        position_manager=_dummy,
        kill_switch=_dummy,
        state_manager=FakeStateManager(state),
        strategy_config=_dummy,
        alerts=FakeAlerts(),
        trade_logger=FakeTradeLogger(last_trade),
        trade_reconciliation=None,
        account_monitor=None,
    )


# =====================================
# MOCKS - position_manager
# =====================================

class FakeBrokerPM:
    def __init__(self, now=None):
        self._now = now or datetime(2026, 7, 9, 12, 0, 0)

    def broker_now(self):
        return self._now


class FakeStateManagerPM:
    def __init__(self, execution_cache=None):
        self.state = {
            "execution_cache": execution_cache or {},
            "strategy": {"breakeven_done": False},
        }
        self.save_calls = 0

    def get_execution_cache(self, ticket):
        return self.state["execution_cache"].get(str(ticket))

    def clear_execution_cache(self, ticket):
        pid = str(ticket)
        if pid in self.state["execution_cache"]:
            del self.state["execution_cache"][pid]

    def get_strategy(self):
        return self.state["strategy"]

    def save(self):
        self.save_calls += 1


class FakeTradeLoggerPM:
    def __init__(self, trade=None):
        self._trade = trade
        self.closes = []

    def get_trade(self, trade_id):
        return self._trade

    def record_trade_close(self, **kwargs):
        self.closes.append(kwargs)


class FakeAlertsPM:
    def __init__(self):
        self.closed_alerts = []

    def alert_position_closed(self, **kw):
        self.closed_alerts.append(kw)


def test_break_even_success_sends_management_alert(monkeypatch):
    monkeypatch.setattr(position_manager_module.mt5, "POSITION_TYPE_BUY", 0, raising=False)
    monkeypatch.setattr(position_manager_module.mt5, "TRADE_RETCODE_DONE", 10009, raising=False)
    position = SimpleNamespace(
        ticket=321,
        type=0,
        price_open=1.1000,
        sl=1.0900,
        tp=1.1300,
    )

    class Broker:
        def get_position(self, _magic):
            return position

        def get_tick(self):
            return SimpleNamespace(bid=1.1110, ask=1.1112)

        def modify_sl(self, _position, _new_sl):
            return SimpleNamespace(retcode=position_manager_module.mt5.TRADE_RETCODE_DONE)

    class Alerts(FakeAlertsPM):
        def __init__(self):
            super().__init__()
            self.info = []
            self.break_even = []

        def send_info(self, message):
            self.info.append(message)

        def alert_break_even(self, **kwargs):
            self.break_even.append(kwargs)

    alerts = Alerts()
    manager = PositionManager(
        broker=Broker(),
        config=SimpleNamespace(
            STRATEGY_NAME="TEST", USE_BREAK_EVEN=True,
            BREAK_EVEN_MODEL="R_MULTIPLE", BREAK_EVEN_TRIGGER=1.0,
            BREAK_EVEN_OFFSET=0.0,
        ),
        state_manager=FakeStateManagerPM(),
        trade_logger=FakeTradeLoggerPM(),
        alerts=alerts,
    )

    assert manager.manage_break_even(42) is True
    assert alerts.break_even == [{
        "strategy_name": "TEST", "side": "BUY", "ticket": 321,
        "entry": 1.1000, "new_sl": 1.1000,
    }]


# =====================================
# 1–11: heartbeat._stats_status
# =====================================

def test_01_state_none_returns_warning():
    hb = make_heartbeat(state=None)
    assert hb._stats_status() == "WARNING"


def test_02_cache_not_dict_returns_warning():
    hb = make_heartbeat(state={"execution_cache": "oops"})
    assert hb._stats_status() == "WARNING"


def test_03_cache_entry_not_dict_returns_warning():
    hb = make_heartbeat(state={"execution_cache": {"123": "bad"}})
    assert hb._stats_status() == "WARNING"


def test_04_cache_entry_missing_key_returns_warning():
    hb = make_heartbeat(state={
        "execution_cache": {
            "123": {
                "trade_id": "t1",
                "risk_usd": 30.0,
                # missing actual_entry_price, expected_entry_price
            }
        }
    })
    assert hb._stats_status() == "WARNING"


def test_05_empty_cache_no_closes_returns_synced():
    hb = make_heartbeat(state={"execution_cache": {}})
    assert hb._stats_status() == "SYNCED"


def test_06_full_cache_no_closes_returns_synced():
    hb = make_heartbeat(state={
        "execution_cache": {
            "123": {
                "trade_id": "t1",
                "risk_usd": 30.0,
                "actual_entry_price": 1.1000,
                "expected_entry_price": 1.1005,
            }
        }
    })
    assert hb._stats_status() == "SYNCED"


def test_07_last_trade_all_numeric_returns_synced():
    last = {
        "exit_time": "2026-07-09T12:00:00",
        "pnl_r": "1.5",
        "pnl_points": "0.0050",
        "trade_duration_sec": "3600",
    }
    hb = make_heartbeat(
        state={"execution_cache": {"999": {
            "trade_id": "t1", "risk_usd": 30.0,
            "actual_entry_price": 1.1, "expected_entry_price": 1.1,
        }}},
        last_trade=last,
    )
    assert hb._stats_status() == "SYNCED"


def test_08_last_trade_has_na_returns_synced():
    last = {
        "exit_time": "2026-07-09T12:00:00",
        "pnl_r": "N/A",
        "pnl_points": "0.0050",
        "trade_duration_sec": "N/A",
    }
    hb = make_heartbeat(
        state={"execution_cache": {}},
        last_trade=last,
    )
    assert hb._stats_status() == "SYNCED"


def test_09_last_trade_blank_enriched_returns_warning():
    last = {
        "exit_time": "2026-07-09T12:00:00",
        "pnl_r": "",
        "pnl_points": "0.0050",
        "trade_duration_sec": "3600",
    }
    hb = make_heartbeat(state={"execution_cache": {}}, last_trade=last)
    assert hb._stats_status() == "WARNING"


def test_10_last_trade_none_enriched_returns_warning():
    last = {
        "exit_time": "2026-07-09T12:00:00",
        "pnl_r": None,
        "pnl_points": None,
        "trade_duration_sec": None,
    }
    hb = make_heartbeat(state={"execution_cache": {}}, last_trade=last)
    assert hb._stats_status() == "WARNING"


def test_11_exception_returns_warning():
    hb = make_heartbeat(state={"execution_cache": {}})

    def boom():
        raise RuntimeError("test exception")

    hb.trade_logger.get_last_closed_trade = boom
    assert hb._stats_status() == "WARNING"


def test_reconciliation_degraded_returns_warning():
    hb = make_heartbeat(state={"execution_cache": {}})
    hb.trade_reconciliation = SimpleNamespace(
        health_snapshot=lambda: {"status": "DEGRADED", "issues": [{"position_id": "1"}]}
    )
    assert hb._stats_status() == "WARNING"


def test_csv_export_failure_returns_warning():
    hb = make_heartbeat(state={"execution_cache": {}})
    hb.trade_logger.export_status_snapshot = lambda: {
        "status": "WARNING", "error": "sharing violation",
    }
    assert hb._stats_status() == "WARNING"


# =====================================
# 12–14: trade_logger._append_csv sentinel
# =====================================

def _make_csv_and_logger(tmpdir, headers):
    """Helper: create CSV with header and a TradeLogger patched to it."""
    csv_path = Path(tmpdir) / "trades.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=headers).writeheader()

    orig = _tl_mod.CSV_FILE
    _tl_mod.CSV_FILE = csv_path

    logger = TradeLogger(
        "hub_demo",
        store=TradeStore(Path(tmpdir) / "trade_ledger.sqlite3"),
        csv_path=csv_path,
    )
    logger.csv_file = csv_path
    logger.csv_headers = headers
    return csv_path, logger, orig


def test_12_none_enriched_becomes_na_in_csv():
    """None enriched fields -> 'N/A' in CSV output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        headers = [
            "trade_id", "ticket", "pnl_r", "pnl_points",
            "trade_duration_sec", "pnl_usd", "exit_time",
        ]
        csv_path, logger, orig = _make_csv_and_logger(tmpdir, headers)

        try:
            logger._append_csv({
                "trade_id": "t1", "ticket": "123",
                "pnl_r": None, "pnl_points": None,
                "trade_duration_sec": None,
                "pnl_usd": 50.0,
                "exit_time": "2026-07-09T12:00:00",
            })

            with open(csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            assert len(rows) == 1
            assert rows[0]["pnl_r"] == NA
            assert rows[0]["pnl_points"] == NA
            assert rows[0]["trade_duration_sec"] == NA
            assert rows[0]["pnl_usd"] == "50.0"
        finally:
            _tl_mod.CSV_FILE = orig


def test_13_numeric_enriched_preserved_in_csv():
    """Numeric enriched fields -> value preserved in CSV output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        headers = [
            "trade_id", "pnl_r", "pnl_points", "trade_duration_sec",
        ]
        csv_path, logger, orig = _make_csv_and_logger(tmpdir, headers)

        try:
            logger._append_csv({
                "trade_id": "t2",
                "pnl_r": 1.5,
                "pnl_points": 0.005,
                "trade_duration_sec": 3600,
            })

            with open(csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            assert len(rows) == 1
            assert rows[0]["pnl_r"] == "1.5"
            assert rows[0]["pnl_points"] == "0.005"
            assert rows[0]["trade_duration_sec"] == "3600"
        finally:
            _tl_mod.CSV_FILE = orig


def test_14_none_non_enriched_stays_empty_not_na():
    """None for non-enriched fields (e.g. pnl_usd) -> empty cell,
    NOT the N/A sentinel. This protects float() aggregates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        headers = [
            "trade_id", "pnl_usd", "pnl_r", "pnl_points",
            "trade_duration_sec",
        ]
        csv_path, logger, orig = _make_csv_and_logger(tmpdir, headers)

        try:
            logger._append_csv({
                "trade_id": "t3",
                "pnl_usd": None,    # NOT in ENRICHED_NA_FIELDS
                "pnl_r": None,      # IN ENRICHED_NA_FIELDS
                "pnl_points": None,
                "trade_duration_sec": None,
            })

            with open(csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            assert len(rows) == 1
            assert rows[0]["pnl_usd"] == ""
            assert rows[0]["pnl_r"] == NA
            assert rows[0]["pnl_points"] == NA
            assert rows[0]["trade_duration_sec"] == NA
        finally:
            _tl_mod.CSV_FILE = orig


# =====================================
# 15–16: position_manager _record_close_analytics
# =====================================

def test_15_no_risk_usd_r_multiple_close_is_none():
    """When risk_usd is missing from execution_cache, r_multiple_close
    stays None (previously 0.0, masking data loss).
    trade_logger.record_trade_close receives pnl_r=None -> writes N/A.
    The alert still receives 0.0 as fallback."""
    pm = PositionManager(
        broker=FakeBrokerPM(),
        config=SimpleNamespace(STRATEGY_NAME="TEST"),
        state_manager=FakeStateManagerPM(execution_cache={
            "100": {
                "trade_id": "t1",
                "actual_entry_price": 1.1000,
                # risk_usd intentionally missing
            }
        }),
        trade_logger=FakeTradeLoggerPM(trade={
            "side": "BUY",
            "entry_time": "2026-07-09T10:00:00",
        }),
        alerts=FakeAlertsPM(),
    )

    position = SimpleNamespace(ticket=100, profit=50.0, type="BUY")
    pm._record_close_analytics(position, exit_price=1.1050,
                               reason="WEEKEND_CLOSE")

    # pnl_r must be None (not 0.0)
    assert len(pm.trade_logger.closes) == 1
    assert pm.trade_logger.closes[0]["pnl_r"] is None

    # pnl_points and trade_duration_sec should be computed (data available)
    assert pm.trade_logger.closes[0]["pnl_points"] is not None
    assert abs(pm.trade_logger.closes[0]["pnl_points"] - 0.005) < 1e-9
    assert pm.trade_logger.closes[0]["trade_duration_sec"] is not None
    assert abs(pm.trade_logger.closes[0]["trade_duration_sec"] - 7200) < 1e-9

    # alert still gets 0.0 fallback
    assert len(pm.alerts.closed_alerts) == 1
    assert pm.alerts.closed_alerts[0]["r_multiple"] == 0.0


def test_16_with_risk_usd_r_multiple_computed():
    """When risk_usd is present and > 0, r_multiple_close is computed
    as a float (not None)."""
    pm = PositionManager(
        broker=FakeBrokerPM(),
        config=SimpleNamespace(STRATEGY_NAME="TEST"),
        state_manager=FakeStateManagerPM(execution_cache={
            "100": {
                "trade_id": "t1",
                "risk_usd": 30.0,
                "actual_entry_price": 1.1000,
                "expected_entry_price": 1.1005,
            }
        }),
        trade_logger=FakeTradeLoggerPM(trade={
            "side": "BUY",
            "entry_time": "2026-07-09T10:00:00",
        }),
        alerts=FakeAlertsPM(),
    )

    position = SimpleNamespace(ticket=100, profit=45.0, type="BUY")
    pm._record_close_analytics(position, exit_price=1.1050, reason="SL")

    # pnl_r = 45.0 / 30.0 = 1.5
    assert len(pm.trade_logger.closes) == 1
    close = pm.trade_logger.closes[0]
    assert close["pnl_r"] is not None
    assert abs(close["pnl_r"] - 1.5) < 1e-9

    # pnl_points = 1.1050 - 1.1000 = 0.005
    assert close["pnl_points"] is not None
    assert abs(close["pnl_points"] - 0.005) < 1e-9

    # trade_duration_sec = 2 hours = 7200
    assert close["trade_duration_sec"] is not None
    assert abs(close["trade_duration_sec"] - 7200) < 1e-9

    # alert also gets the correct r_multiple
    assert len(pm.alerts.closed_alerts) == 1
    assert abs(pm.alerts.closed_alerts[0]["r_multiple"] - 1.5) < 1e-9


# =====================================
# RUNNER
# =====================================

if __name__ == "__main__":
    passed = 0
    failed = 0
    errors = []

    for name in sorted(globals()):
        if not name.startswith("test_"):
            continue
        fn = globals()[name]
        if not callable(fn):
            continue

        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, e))

    print(f"\nStats sync: {passed} passed, {failed} failed "
          f"({passed + failed} total)\n")

    for name, err in errors:
        print(f"  FAIL: {name}")
        print(f"        {err}\n")

    sys.exit(1 if failed else 0)
