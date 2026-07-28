import csv
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from analytics.trade_logger import TradeLogger
from analytics import trade_logger as trade_logger_module
from analytics.trade_store import TradeStoreError
from analytics.trade_reconciliation import TradeReconciliation
from analytics.trade_store import TradeStore
from core.position_manager import PositionManager
from core import position_manager as position_manager_module


UTC = timezone.utc


def _open(trade_id="trade-1", account_id="hub_demo", position_id="100", when=None):
    return {
        "trade_id": trade_id, "account_id": account_id, "strategy_id": "mean_reversion",
        "symbol": "EURUSD", "magic": "42", "order_id": f"order-{position_id}",
        "position_id": position_id, "side": "BUY", "entry_time_utc": when or datetime(2026, 7, 6, 10, tzinfo=UTC),
        "entry_volume": 1.0, "entry_price": 1.10,
    }


def _store(tmp_path):
    return TradeStore(tmp_path / "trade_ledger.sqlite3", lock_retry_attempts=10, lock_retry_base_sec=0.01)


def test_duplicate_and_concurrent_open_are_idempotent(tmp_path):
    store = _store(tmp_path)

    def write(index):
        payload = _open(trade_id=f"try-{index}")
        payload["deal_id"] = "entry-100"
        return store.upsert_open(payload)["trade_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(write, range(16)))

    assert len(set(ids)) == 1
    assert len(store.list_trades("hub_demo")) == 1


def test_reconciliation_issues_are_isolated_by_strategy(tmp_path):
    store = _store(tmp_path)
    store.record_reconciliation_issue("hub_1::42", "alpha", "100", "ENTRY_MISSING", ["d1"])
    store.record_reconciliation_issue("hub_1::42", "beta", "200", "PARITY", ["d2"])

    assert [row["position_id"] for row in store.list_reconciliation_issues("hub_1::42", "alpha")] == ["100"]
    assert [row["position_id"] for row in store.list_reconciliation_issues("hub_1::42", "beta")] == ["200"]

    store.clear_reconciliation_issue("hub_1::42", "alpha", "100")
    assert store.list_reconciliation_issues("hub_1::42", "alpha") == []
    assert len(store.list_reconciliation_issues("hub_1::42", "beta")) == 1


def test_legacy_reconciliation_issue_is_migrated_to_its_strategy(tmp_path):
    store = _store(tmp_path)
    store.upsert_open(_open(account_id="hub_1::42", position_id="100"))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE reconciliation_issues")
        connection.executescript(
            """CREATE TABLE reconciliation_issues (
                   account_id TEXT NOT NULL,
                   position_id TEXT NOT NULL,
                   reason TEXT NOT NULL,
                   broker_deal_ids TEXT NOT NULL,
                   updated_at REAL NOT NULL,
                   PRIMARY KEY (account_id, position_id)
               );
               INSERT INTO reconciliation_issues
                   (account_id, position_id, reason, broker_deal_ids, updated_at)
               VALUES ('hub_1::42', '100', 'ENTRY_MISSING', 'd1', 1);"""
        )

    issues = store.list_reconciliation_issues("hub_1::42", "mean_reversion")
    assert len(issues) == 1
    assert issues[0]["strategy_id"] == "mean_reversion"


def test_added_strategy_column_rebuilds_legacy_issue_primary_key(tmp_path):
    store = _store(tmp_path)
    store.list_trades("hub_1::42")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE reconciliation_issues")
        connection.executescript(
            """CREATE TABLE reconciliation_issues (
                   account_id TEXT NOT NULL,
                   position_id TEXT NOT NULL,
                   reason TEXT NOT NULL,
                   broker_deal_ids TEXT NOT NULL,
                   updated_at REAL NOT NULL,
                   strategy_id TEXT,
                   PRIMARY KEY (account_id, position_id)
               );
               INSERT INTO reconciliation_issues
                   (account_id, position_id, reason, broker_deal_ids, updated_at, strategy_id)
               VALUES ('hub_1::42', '__parity__', 'PARITY', 'd1', 1, 'alpha');"""
        )

    store.record_reconciliation_issue("hub_1::42", "beta", "__parity__", "PARITY", ["d2"])

    assert len(store.list_reconciliation_issues("hub_1::42", "alpha")) == 1
    assert len(store.list_reconciliation_issues("hub_1::42", "beta")) == 1


def test_duplicate_close_replaces_values_instead_of_double_counting(tmp_path):
    store = _store(tmp_path)
    store.upsert_open(_open())
    close = {
        "exit_time_utc": datetime(2026, 7, 6, 12, tzinfo=UTC), "exit_price": 1.12,
        "profit": 20, "commission": -2, "swap": -1, "close_reason": "TP",
    }
    store.upsert_close("trade-1", "hub_demo", close)
    store.upsert_close("trade-1", "hub_demo", close)

    trade = store.get_trade("trade-1", "hub_demo")
    assert trade["profit"] == 20
    assert store.account_pnl("hub_demo", *store.utc_day_window(datetime(2026, 7, 6, 20, tzinfo=UTC))) == 20


def test_multiple_close_deals_and_partial_fill_aggregate_once(tmp_path):
    store = _store(tmp_path)
    payload = _open()
    deals = [
        {"deal_id": "entry-1", "entry_type": "IN", "occurred_at_utc": datetime(2026, 7, 6, 10, tzinfo=UTC), "volume": 0.4, "price": 1.10, "profit": 0},
        {"deal_id": "entry-2", "entry_type": "IN", "occurred_at_utc": datetime(2026, 7, 6, 10, 1, tzinfo=UTC), "volume": 0.6, "price": 1.11, "profit": 0},
        {"deal_id": "exit-1", "entry_type": "OUT", "occurred_at_utc": datetime(2026, 7, 6, 11, tzinfo=UTC), "volume": 0.3, "price": 1.12, "commission": -0.3, "swap": -0.1, "profit": 3},
        {"deal_id": "exit-2", "entry_type": "OUT", "occurred_at_utc": datetime(2026, 7, 6, 12, tzinfo=UTC), "volume": 0.7, "price": 1.13, "commission": -0.7, "swap": -0.2, "profit": 7},
    ]
    store.upsert_recovered_trade(payload, deals)
    store.upsert_recovered_trade(payload, deals)

    trade = store.get_trade("trade-1", "hub_demo")
    assert trade["status"] == "CLOSED"
    assert trade["entry_volume"] == 1.0 and trade["closed_volume"] == 1.0
    assert trade["profit"] == 10.0 and trade["commission"] == -1.0 and round(trade["swap"], 2) == -0.3
    assert trade["exit_price"] == 1.127


def test_account_queries_are_isolated_and_require_scope(tmp_path):
    store = _store(tmp_path)
    for account, pnl in (("hub_demo", 10), ("hub_2", 90)):
        payload = _open(trade_id=f"trade-{account}", account_id=account, position_id=account)
        store.upsert_open(payload)
        store.upsert_close(payload["trade_id"], account, {"exit_time_utc": datetime(2026, 7, 6, 11, tzinfo=UTC), "profit": pnl})
    start, end = store.utc_day_window(datetime(2026, 7, 6, 12, tzinfo=UTC))
    assert store.account_pnl("hub_demo", start, end) == 10
    assert store.account_pnl("hub_2", start, end) == 90


def test_daily_and_weekly_queries_use_utc_boundaries(tmp_path):
    store = _store(tmp_path)
    payload = _open()
    store.upsert_open(payload)
    store.upsert_close("trade-1", "hub_demo", {"exit_time_utc": datetime(2026, 7, 5, 23, 59, tzinfo=UTC), "profit": 7})
    monday = datetime(2026, 7, 6, 0, 1, tzinfo=UTC)
    day_start, day_end = store.utc_day_window(monday)
    week_start, week_end = store.utc_week_window(monday)
    assert store.account_pnl("hub_demo", day_start, day_end) == 0
    assert store.account_pnl("hub_demo", week_start, week_end) == 0


def test_csv_export_matches_account_ledger(tmp_path):
    store = _store(tmp_path)
    export = tmp_path / "trades_hub_demo.csv"
    logger = TradeLogger("hub_demo", store=store, csv_path=export)
    trade_id = logger.record_trade_open(
        ticket="100", magic=42, strategy_name="mean_reversion", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.10, actual_entry=1.10,
        entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12, stop_distance_points=0.01,
        take_distance_points=0.02, target_r=2, risk_usd=10, equity_at_entry=1000,
    )
    logger.record_trade_close(trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.12, "TP", 20, 0.02, 2, 3600)

    rows = export.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and "hub_demo" in rows[1] and ",20.0," in rows[1]


def test_injected_store_keeps_implicit_export_in_its_own_runtime(tmp_path, monkeypatch):
    forbidden_export = tmp_path / "production" / "analytics" / "trades.csv"
    monkeypatch.setattr(trade_logger_module, "CSV_FILE", forbidden_export)
    store = _store(tmp_path / "isolated")
    logger = TradeLogger(
        "hub_demo", store=store, strategy_id="alpha", strategy_name="ALPHA"
    )
    trade_id = logger.record_trade_open(
        ticket="99", magic=7, strategy_name="ALPHA", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1,
        actual_entry=1.1, entry_spread_points=1, volume=1, initial_sl=1.0,
        initial_tp=1.2, stop_distance_points=.1, take_distance_points=.1,
        target_r=1, risk_usd=20, equity_at_entry=1000,
    )

    logger.record_trade_close(
        trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.0, "SL",
        -20, -.1, -1, 3600,
    )

    assert logger.csv_file == store.db_path.parent / "analytics" / "trades.csv"
    assert logger.csv_file.is_file()
    assert not forbidden_export.exists()


def test_global_export_contains_mixed_hubs_strategy_metadata_and_deal_ids(tmp_path):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    demo = TradeLogger(
        "hub_demo", store=store, csv_path=export, strategy_id="aud_mean_reversion",
        strategy_name="AUD Mean Reversion", broker_account_login=11111,
    )
    second = TradeLogger(
        "hub_2", store=store, csv_path=export, strategy_id="xau_breakout_60",
        strategy_name="XAU Breakout 60", broker_account_login=22222,
    )
    for logger, ticket, strategy_name in (
        (demo, "100", "AUD Mean Reversion"),
        (second, "200", "XAU Breakout 60"),
    ):
        trade_id = logger.record_trade_open(
            ticket=ticket, magic=42, strategy_name=strategy_name, symbol="EURUSD", side="BUY",
            entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
            entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
            stop_distance_points=.01, take_distance_points=.02, target_r=2, risk_usd=10,
            equity_at_entry=1000, deal_id=f"entry-{ticket}",
        )
        logger.record_trade_close(
            trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.12, "TP", 20, .02, 2, 3600,
            deal_id=f"exit-{ticket}", volume=1,
        )

    with export.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {(row["hub_id"], row["strategy_id"], row["broker_account_login"]) for row in rows} == {
        ("hub_demo", "aud_mean_reversion", "11111"),
        ("hub_2", "xau_breakout_60", "22222"),
    }
    assert {deal_id for row in rows for deal_id in row["deal_ids"].split(";")} == {
        "entry-100", "exit-100", "entry-200", "exit-200",
    }
    assert len(store.list_all_trades(hub_id="hub_demo")) == 1
    assert len(store.list_all_trades(strategy_id="xau_breakout_60")) == 1
    assert len(store.list_all_trades(broker_account_login=22222)) == 1


def test_reusing_one_hub_with_another_login_cannot_collide_broker_ids(tmp_path):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    first = TradeLogger(
        "hub_1", store=store, csv_path=export, strategy_id="alpha",
        strategy_name="ALPHA", broker_account_login=11111,
    )
    second = TradeLogger(
        "hub_1", store=store, csv_path=export, strategy_id="alpha",
        strategy_name="ALPHA", broker_account_login=22222,
    )
    for logger, pnl in ((first, 10), (second, 20)):
        trade_id = logger.record_trade_open(
            ticket="100", magic=42, strategy_name="ALPHA", symbol="EURUSD", side="BUY",
            entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
            entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
            stop_distance_points=.01, take_distance_points=.02, target_r=2, risk_usd=10,
            equity_at_entry=1000, order_id="500", deal_id="700",
        )
        logger.record_trade_close(
            trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.12, "TP", pnl, .02, 2, 3600,
            deal_id="701", volume=1,
        )

    rows = store.list_all_trades(hub_id="hub_1")
    assert len(rows) == 2
    assert {row["broker_account_login"] for row in rows} == {"11111", "22222"}
    assert {row["account_id"] for row in rows} == {"hub_1::11111", "hub_1::22222"}
    now = datetime(2026, 7, 6, 12, tzinfo=UTC)
    assert first.get_daily_account_pnl("hub_1", now) == 10
    assert second.get_daily_account_pnl("hub_1", now) == 20
    assert first.get_daily_hub_pnl(now) == 30


def test_legacy_rows_get_safe_metadata_without_guessing_broker_login(tmp_path):
    store = _store(tmp_path)
    payload = _open()
    store.upsert_open(payload)
    row = store.list_all_trades()[0]

    assert row["account_id"] == "hub_demo"
    assert row["hub_id"] == "hub_demo"
    assert row["strategy_name"] == "mean_reversion"
    assert row["broker_account_login"] is None


def test_pre_t06a_database_schema_is_migrated_without_guessing_login(tmp_path):
    ledger_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(ledger_path) as connection:
        connection.executescript(
            """CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                entry_time_utc TEXT NOT NULL,
                exit_time_utc TEXT
            );
            CREATE TABLE trade_deals (
                account_id TEXT NOT NULL,
                deal_id TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                volume REAL NOT NULL,
                price REAL,
                commission REAL NOT NULL DEFAULT 0,
                swap REAL NOT NULL DEFAULT 0,
                profit REAL NOT NULL DEFAULT 0,
                reason TEXT,
                PRIMARY KEY (account_id, deal_id)
            );
            INSERT INTO trades (trade_id, account_id, strategy_id, entry_time_utc)
            VALUES ('legacy-1', 'hub_demo', 'old_strategy', '2026-07-06T10:00:00+00:00');"""
        )

    rows = TradeStore(ledger_path).list_all_trades()

    assert rows[0]["hub_id"] == "hub_demo"
    assert rows[0]["strategy_name"] == "old_strategy"
    assert rows[0]["broker_account_login"] is None


def test_concurrent_global_exports_leave_a_complete_csv(tmp_path):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    logger = TradeLogger("hub_demo", store=store, csv_path=export)
    trade_id = logger.record_trade_open(
        ticket="100", magic=42, strategy_name="mean_reversion", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
        entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
        stop_distance_points=.01, take_distance_points=.02, target_r=2, risk_usd=10, equity_at_entry=1000,
    )
    logger.record_trade_close(trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.12, "TP", 20, .02, 2, 3600)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: logger.export_csv(), range(16)))

    with export.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["trade_id"] == trade_id


def test_csv_export_retries_transient_windows_replace_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    logger = TradeLogger(
        "hub_demo", store=store, csv_path=export,
        export_replace_attempts=3, export_replace_backoff_sec=0,
    )
    trade_id = logger.record_trade_open(
        ticket="100", magic=42, strategy_name="mean_reversion", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
        entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
        stop_distance_points=.01, take_distance_points=.02, target_r=2,
        risk_usd=10, equity_at_entry=1000,
    )
    original_replace = os.replace
    calls = {"count": 0}

    def transient_replace(source, destination):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(5, "sharing violation")
        return original_replace(source, destination)

    monkeypatch.setattr(trade_logger_module.os, "replace", transient_replace)
    assert logger.record_trade_close(
        trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.12, "SL", -10, -.02, -1, 3600,
    ) is True
    assert calls["count"] == 3
    assert logger.export_status_snapshot() == {"status": "SYNCED", "error": None}
    assert export.is_file()


def test_csv_export_clears_read_only_target_before_retry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    logger = TradeLogger(
        "hub_demo", store=store, csv_path=export,
        export_replace_attempts=2, export_replace_backoff_sec=0,
    )
    store.upsert_open(_open())
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text("old export\n", encoding="utf-8")
    export.chmod(export.stat().st_mode & ~stat.S_IWRITE)
    original_replace = os.replace
    calls = {"count": 0}

    def windows_like_replace(source, destination):
        calls["count"] += 1
        target = Path(destination)
        if target.exists() and not target.stat().st_mode & stat.S_IWRITE:
            raise PermissionError(5, "read-only target")
        return original_replace(source, destination)

    monkeypatch.setattr(trade_logger_module.os, "replace", windows_like_replace)
    logger.export_csv()
    assert calls["count"] == 2
    assert export.stat().st_mode & stat.S_IWRITE
    assert "trade-1" in export.read_text(encoding="utf-8")


def test_permanent_csv_lock_does_not_undo_durable_close(tmp_path, monkeypatch):
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    logger = TradeLogger(
        "hub_demo", store=store, csv_path=export,
        export_replace_attempts=2, export_replace_backoff_sec=0,
    )
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text("previous export\n", encoding="utf-8")
    trade_id = logger.record_trade_open(
        ticket="100", magic=42, strategy_name="mean_reversion", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
        entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
        stop_distance_points=.01, take_distance_points=.02, target_r=2,
        risk_usd=10, equity_at_entry=1000,
    )

    monkeypatch.setattr(
        trade_logger_module.os, "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError(5, "persistent lock")),
    )
    assert logger.record_trade_close(
        trade_id, datetime(2026, 7, 6, 11, tzinfo=UTC), 1.09, "SL", -10, -.01, -1, 3600,
    ) is True
    assert store.get_trade(trade_id, "hub_demo")["status"] == "CLOSED"
    assert logger.export_status_snapshot()["status"] == "WARNING"
    assert export.read_text(encoding="utf-8") == "previous export\n"
    assert list(export.parent.glob(".trades.csv.*.tmp")) == []


def test_external_sl_close_clears_cache_even_when_csv_is_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(position_manager_module.mt5, "DEAL_REASON_TP", 1, raising=False)
    monkeypatch.setattr(position_manager_module.mt5, "DEAL_REASON_SL", 2, raising=False)
    monkeypatch.setattr(position_manager_module.mt5, "DEAL_REASON_SO", 3, raising=False)
    monkeypatch.setattr(position_manager_module.mt5, "DEAL_REASON_CLIENT", 4, raising=False)
    monkeypatch.setattr(position_manager_module.mt5, "DEAL_REASON_EXPERT", 5, raising=False)
    store = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    logger = TradeLogger(
        "hub_demo", store=store, csv_path=export, strategy_id="mean_reversion",
        export_replace_attempts=1, export_replace_backoff_sec=0,
    )
    trade_id = logger.record_trade_open(
        ticket="100", magic=42, strategy_name="MEAN_REVERSION", symbol="EURUSD", side="BUY",
        entry_time=datetime(2026, 7, 6, 10, tzinfo=UTC), expected_entry=1.1, actual_entry=1.1,
        entry_spread_points=1, volume=1, initial_sl=1.09, initial_tp=1.12,
        stop_distance_points=.01, take_distance_points=.02, target_r=2,
        risk_usd=10, equity_at_entry=1000,
    )

    class State:
        def __init__(self):
            self.state = {
                "execution_cache": {"100": {
                    "trade_id": trade_id, "risk_usd": 10,
                    "actual_entry_price": 1.1, "expected_entry_price": 1.1,
                }},
                "strategy": {"breakeven_done": False},
            }

        def clear_execution_cache(self, ticket):
            self.state["execution_cache"].pop(str(ticket), None)

        def get_strategy(self):
            return self.state["strategy"]

        def save(self):
            pass

    class Broker:
        def get_positions(self):
            return []

        def get_deal_profit(self, _ticket):
            return {
                "profit": -10.0, "price": 1.09,
                "time": datetime(2026, 7, 6, 11, tzinfo=UTC).timestamp(),
                "reason": 2,
            }

        def utc_from_broker_epoch(self, timestamp):
            return datetime.fromtimestamp(timestamp, tz=UTC)

        def decode_close_reason(self, _deal):
            return "SL"

    class Alerts:
        def __init__(self):
            self.closed = []

        def send_terminal_throttled(self, **_kwargs):
            pass

        def alert_position_closed(self, **kwargs):
            self.closed.append(kwargs)

        def send_info(self, _message):
            pass

    state = State()
    manager = PositionManager(
        broker=Broker(), config=SimpleNamespace(STRATEGY_NAME="MEAN_REVERSION"),
        state_manager=state, trade_logger=logger, alerts=Alerts(),
    )
    monkeypatch.setattr(
        trade_logger_module.os, "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError(5, "persistent lock")),
    )

    assert manager.handle_external_position_close() is True
    assert state.state["execution_cache"] == {}
    assert store.get_trade(trade_id, "hub_demo")["status"] == "CLOSED"
    assert logger.export_status_snapshot()["status"] == "WARNING"
    assert manager.handle_external_position_close() is False


def test_legacy_csv_is_imported_before_first_authoritative_export(tmp_path):
    ledger = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    export.parent.mkdir(parents=True)
    headers = [
        "trade_id", "account_id", "strategy_name", "symbol", "ticket", "side",
        "entry_time", "exit_time", "volume", "actual_entry", "exit_price",
        "pnl_usd", "pnl_points", "pnl_r", "trade_duration_sec", "close_reason",
    ]
    with export.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow({
            "trade_id": "old-1", "account_id": "hub_1", "strategy_name": "OLD_ALPHA",
            "symbol": "EURUSD", "ticket": "100", "side": "BUY",
            "entry_time": "2026-07-01T10:00:00+00:00", "exit_time": "2026-07-01T11:00:00+00:00",
            "volume": "0.1", "actual_entry": "1.1", "exit_price": "1.2",
            "pnl_usd": "15", "pnl_points": "0.1", "pnl_r": "1.5",
            "trade_duration_sec": "3600", "close_reason": "TP",
        })
        writer.writerow({
            "trade_id": "old-2", "account_id": "hub_2", "strategy_name": "OLD_BETA",
            "symbol": "XAUUSD", "ticket": "100", "side": "SELL",
            "entry_time": "2026-07-02T10:00:00+00:00", "exit_time": "2026-07-02T11:00:00+00:00",
            "volume": "0.2", "actual_entry": "2400", "exit_price": "2390",
            "pnl_usd": "20", "pnl_points": "10", "pnl_r": "2",
            "trade_duration_sec": "3600", "close_reason": "TP",
        })

    logger = TradeLogger("hub_demo", store=ledger, csv_path=export)
    assert {row["trade_id"] for row in ledger.list_all_trades()} == {"old-1", "old-2"}
    assert {row["hub_id"] for row in ledger.list_all_trades()} == {"N/A"}
    TradeLogger("hub_demo", store=ledger, csv_path=export)
    connection = sqlite3.connect(ledger.db_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_csv_migrations"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    logger.export_csv()
    with export.open(newline="", encoding="utf-8") as stream:
        exported = list(csv.DictReader(stream))
    assert {row["trade_id"] for row in exported} == {"old-1", "old-2"}
    assert {row["pnl_usd"] for row in exported} == {"15.0", "20.0"}
    assert {row["volume"] for row in exported} == {"0.1", "0.2"}


def test_invalid_legacy_csv_is_preserved_and_blocks_startup_export(tmp_path):
    ledger = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    export.parent.mkdir(parents=True)
    original = (
        "trade_id,account_id,strategy_name,ticket,side,entry_time,volume\n"
        "broken,hub_1,ALPHA,100,BUY,2026-07-01T10:00:00+00:00,0.1\n"
    ).encode()
    export.write_bytes(original)
    try:
        TradeLogger("hub_demo", store=ledger, csv_path=export)
    except TradeStoreError as exc:
        assert "source preserved" in str(exc)
    else:
        raise AssertionError("invalid legacy CSV must block startup")
    assert export.read_bytes() == original


def test_oldest_csv_without_account_columns_uses_unknown_hub(tmp_path):
    ledger = _store(tmp_path)
    export = tmp_path / "analytics" / "trades.csv"
    export.parent.mkdir(parents=True)
    export.write_text(
        "trade_id,ticket,magic,strategy_name,symbol,side,entry_time,exit_time,volume,actual_entry,exit_price,pnl_usd\n"
        "old-no-account,9386002056,44001,AUD_MEAN_REVERSION,AUDUSD,BUY,2026-07-01T10:00:00+00:00,2026-07-01T11:00:00+00:00,0.1,0.65,0.66,12.5\n",
        encoding="utf-8",
    )
    TradeLogger("hub_demo", store=ledger, csv_path=export)
    row = ledger.list_all_trades()[0]
    assert row["trade_id"] == "old-no-account"
    assert row["hub_id"] == "N/A"
    assert row["account_id"] == "N/A"
    assert row["broker_account_login"] is None
    assert row["profit"] == 12.5


def test_reconciliation_filename_scope_is_windows_safe():
    assert TradeReconciliation._safe_filename_component("hub_demo::52944617") == "hub_demo_52944617"
    assert TradeReconciliation._safe_filename_component("alpha/beta:one") == "alpha_beta_one"


def test_reconciliation_adopts_matching_legacy_csv_trade_instead_of_duplicating(tmp_path):
    store = _store(tmp_path)
    legacy = _open(
        trade_id="AUD_MEAN_REVERSION_legacy", account_id="hub_demo",
        position_id="1788474092", when=datetime(2026, 7, 3, 7, 4, tzinfo=UTC),
    )
    legacy.update({
        "hub_id": "N/A", "strategy_id": "AUD_MEAN_REVERSION",
        "strategy_name": "AUD_MEAN_REVERSION", "symbol": "AUDUSD",
        "magic": "44001", "entry_price": 0.6935, "entry_volume": 1.0,
        "side": "BUY", "risk_usd": 100.0,
    })
    row = store.upsert_open(legacy)
    store.upsert_close(row["trade_id"], row["account_id"], {
        "exit_time_utc": datetime(2026, 7, 3, 7, 18, tzinfo=UTC),
        "exit_price": 0.69302, "profit": -99.54, "commission": 0,
        "swap": 0, "pnl_r": -0.9954, "trade_duration_sec": 850,
    })
    recovered = {
        **legacy,
        "trade_id": "new-random-id",
        "account_id": "hub_demo::52945683",
        "hub_id": "hub_demo",
        "broker_account_login": "52945683",
        "strategy_id": "aud_mean_reversion",
        "entry_time_utc": datetime(2026, 7, 3, 4, 4, tzinfo=UTC),
    }
    deals = [
        {"deal_id": "in-1", "entry_type": "IN", "occurred_at_utc": datetime(2026, 7, 3, 4, 4, tzinfo=UTC), "volume": 1.0, "price": 0.6935, "profit": 0},
        {"deal_id": "out-1", "entry_type": "OUT", "occurred_at_utc": datetime(2026, 7, 3, 4, 18, tzinfo=UTC), "volume": 1.0, "price": 0.69302, "profit": -99.54},
    ]
    adopted = store.upsert_recovered_trade(recovered, deals)
    rows = store.list_all_trades(hub_id="hub_demo")
    assert len(rows) == 1
    assert adopted["trade_id"] == "AUD_MEAN_REVERSION_legacy"
    assert adopted["account_id"] == "hub_demo::52945683"
    assert adopted["broker_account_login"] == "52945683"
    assert adopted["strategy_id"] == "aud_mean_reversion"
    assert adopted["entry_time_utc"] == "2026-07-03T04:04:00+00:00"
    assert adopted["profit"] == -99.54


def test_logger_repairs_previously_invented_unscoped_hub_label(tmp_path):
    store = _store(tmp_path)
    store.upsert_open({
        **_open(trade_id="legacy-hub", account_id="hub_demo", position_id="900"),
        "hub_id": "hub_demo",
    })
    TradeLogger(
        "hub_demo", store=store,
        csv_path=tmp_path / "missing.csv",
        strategy_id="mean_reversion",
        broker_account_login="52945683",
    )
    row = store.list_all_trades()[0]
    assert row["account_id"] == "hub_demo"
    assert row["hub_id"] == "N/A"


def test_existing_scoped_duplicate_is_collapsed_and_keeps_legacy_analytics(tmp_path):
    store = _store(tmp_path)
    common = {
        "hub_id": "hub_demo", "strategy_name": "AUD_MEAN_REVERSION",
        "symbol": "AUDUSD", "magic": "44001", "position_id": "1788875318",
        "side": "BUY", "entry_time_utc": datetime(2026, 7, 3, 12, 6, tzinfo=UTC),
        "entry_volume": 1.0, "entry_price": 0.6930,
    }
    legacy = store.upsert_open({
        **common, "trade_id": "legacy-id", "account_id": "hub_demo",
        "strategy_id": "AUD_MEAN_REVERSION", "risk_usd": 100.0,
    })
    store.upsert_close(legacy["trade_id"], legacy["account_id"], {
        "exit_time_utc": datetime(2026, 7, 3, 19, 12, tzinfo=UTC),
        "exit_price": 0.69358, "profit": 102.87, "commission": 0,
        "swap": 0, "pnl_r": 1.0287, "trade_duration_sec": 25568,
    })
    scoped = store.upsert_open({
        **common, "trade_id": "scoped-id", "account_id": "hub_demo::52945683",
        "broker_account_login": "52945683", "strategy_id": "aud_mean_reversion",
    })
    store.upsert_close(scoped["trade_id"], scoped["account_id"], {
        "exit_time_utc": datetime(2026, 7, 3, 16, 12, tzinfo=UTC),
        "exit_price": 0.69358, "profit": 102.87, "commission": 0, "swap": 0,
    })
    collapsed = store.reconcile_legacy_duplicates(
        "hub_demo::52945683", "aud_mean_reversion"
    )
    assert collapsed == 1
    merged = store.get_by_position("hub_demo::52945683", "1788875318")
    assert merged["trade_id"] == "scoped-id"
    assert merged["risk_usd"] == 100.0
    assert merged["pnl_r"] == 1.0287
    assert merged["trade_duration_sec"] == 25568
    assert len(store.list_all_trades(hub_id="hub_demo")) == 1
