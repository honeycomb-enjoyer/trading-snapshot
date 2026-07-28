"""Durable, account-scoped trade and broker-deal ledger.

SQLite is deliberately used directly here, matching the account-state and
order-intent stores.  Each write owns a short transaction and uses broker IDs
as idempotency keys; the exported CSV is never read by this module.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "runtime" / "trade_ledger.sqlite3"


class TradeStoreError(RuntimeError):
    """The ledger could not safely complete an operation."""


class TradeStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY,
        -- account_id is retained for backwards-compatible callers.  It is
        -- a durable hub+broker-login scope for new rows.  Legacy rows may
        -- contain only the logical hub ID.
        account_id TEXT NOT NULL,
        hub_id TEXT NOT NULL,
        broker_account_login TEXT,
        strategy_id TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        magic TEXT,
        order_id TEXT,
        position_id TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_time_utc TEXT NOT NULL,
        exit_time_utc TEXT,
        entry_volume REAL NOT NULL DEFAULT 0,
        closed_volume REAL NOT NULL DEFAULT 0,
        entry_price REAL,
        exit_price REAL,
        expected_entry REAL,
        entry_spread_points REAL,
        initial_sl REAL,
        initial_tp REAL,
        stop_distance_points REAL,
        take_distance_points REAL,
        target_r REAL,
        risk_usd REAL,
        equity_at_entry REAL,
        commission REAL NOT NULL DEFAULT 0,
        swap REAL NOT NULL DEFAULT 0,
        profit REAL NOT NULL DEFAULT 0,
        pnl_points REAL,
        pnl_r REAL,
        trade_duration_sec REAL,
        close_reason TEXT,
        code_version TEXT,
        config_version TEXT,
        data_version TEXT,
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (account_id, position_id),
        UNIQUE (account_id, order_id)
    );

    CREATE TABLE IF NOT EXISTS trade_deals (
        account_id TEXT NOT NULL,
        deal_id TEXT NOT NULL,
        trade_id TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
        entry_type TEXT NOT NULL CHECK (entry_type IN ('IN', 'OUT')),
        occurred_at_utc TEXT NOT NULL,
        volume REAL NOT NULL CHECK (volume >= 0),
        price REAL,
        commission REAL NOT NULL DEFAULT 0,
        swap REAL NOT NULL DEFAULT 0,
        profit REAL NOT NULL DEFAULT 0,
        reason TEXT,
        PRIMARY KEY (account_id, deal_id)
    );

    CREATE INDEX IF NOT EXISTS idx_trades_account_exit
        ON trades(account_id, exit_time_utc, strategy_id);
    CREATE INDEX IF NOT EXISTS idx_trade_deals_trade
        ON trade_deals(trade_id);

    CREATE TABLE IF NOT EXISTS reconciliation_issues (
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        position_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        broker_deal_ids TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (account_id, strategy_id, position_id)
    );

    CREATE TABLE IF NOT EXISTS legacy_csv_migrations (
        fingerprint TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        imported_at REAL NOT NULL
    );
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        busy_timeout_ms: int = 1_000,
        lock_retry_attempts: int = 4,
        lock_retry_base_sec: float = 0.05,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        if busy_timeout_ms < 1 or lock_retry_attempts < 1 or lock_retry_base_sec < 0:
            raise ValueError("invalid SQLite retry configuration")
        self.db_path = Path(db_path or DEFAULT_DB_PATH).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retry_attempts = lock_retry_attempts
        self.lock_retry_base_sec = lock_retry_base_sec
        self._now = now_fn

    def upsert_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert an open trade once, keyed by account and broker position."""
        record = self._normalise_open(payload)
        entry_deal_id = payload.get("deal_id")

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM trades WHERE account_id = ? AND position_id = ?",
                (record["account_id"], record["position_id"]),
            ).fetchone()
            if existing is not None:
                return self._row(existing)
            now = self._now()
            columns = tuple(record)
            conn.execute(
                f"INSERT INTO trades ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in columns)}, ?, ?) "
                "ON CONFLICT(account_id, position_id) DO NOTHING",
                (*[record[column] for column in columns], now, now),
            )
            row = conn.execute(
                "SELECT * FROM trades WHERE account_id = ? AND position_id = ?",
                (record["account_id"], record["position_id"]),
            ).fetchone()
            if entry_deal_id is not None:
                self._upsert_deal(conn, row["trade_id"], record["account_id"], str(entry_deal_id), "IN", record["entry_time_utc"], record["entry_volume"], record.get("entry_price"), 0, 0, 0, None)
            return self._row(row)

        return self._transaction(operation)

    def upsert_close(self, trade_id: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace close values, never append them, so repeated closes are safe."""
        account_id = self._required_text(account_id, "account_id")
        close = self._normalise_close(payload)

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM trades WHERE trade_id = ? AND account_id = ?", (trade_id, account_id)
            ).fetchone()
            if row is None:
                raise TradeStoreError("cannot close an unknown trade in this account")
            if close.get("deal_id") is not None:
                self._upsert_deal(conn, trade_id, account_id, close["deal_id"], "OUT", close["exit_time_utc"], close.get("volume", 0), close.get("exit_price"), close["commission"], close["swap"], close["profit"], close.get("close_reason"))
                self._aggregate_deals(conn, trade_id)
            else:
                conn.execute(
                    """UPDATE trades SET exit_time_utc = ?, exit_price = ?, profit = ?, commission = ?,
                       swap = ?, pnl_points = ?, pnl_r = ?, trade_duration_sec = ?, close_reason = ?,
                       closed_volume = CASE WHEN closed_volume > 0 THEN closed_volume ELSE entry_volume END,
                       status = 'CLOSED', updated_at = ? WHERE trade_id = ?""",
                    (close["exit_time_utc"], close.get("exit_price"), close["profit"], close["commission"], close["swap"], close.get("pnl_points"), close.get("pnl_r"), close.get("trade_duration_sec"), close.get("close_reason"), self._now(), trade_id),
                )
            return self._row(conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone())

        return self._transaction(operation)

    def upsert_recovered_trade(self, payload: dict[str, Any], deals: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Upsert broker history, aggregating every partial entry/exit deal."""
        deals = list(deals)
        self.adopt_legacy_trade(payload, deals)
        trade = self.upsert_open(payload)
        account_id, trade_id = trade["account_id"], trade["trade_id"]

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            for deal in deals:
                self._upsert_deal(
                    conn, trade_id, account_id, self._required_text(deal.get("deal_id"), "deal_id"),
                    deal["entry_type"], self._utc(deal["occurred_at_utc"]), self._number(deal.get("volume", 0), "volume"),
                    deal.get("price"), self._number(deal.get("commission", 0), "commission"),
                    self._number(deal.get("swap", 0), "swap"), self._number(deal.get("profit", 0), "profit"), deal.get("reason"),
                )
            self._aggregate_deals(conn, trade_id, payload.get("close_reason"))
            return self._row(conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone())

        return self._transaction(operation)

    def upsert_recovered_close_by_position(
        self, account_id: str, position_id: str, deals: Iterable[dict[str, Any]], close_reason: str | None,
    ) -> dict[str, Any]:
        """Attach exit deals to an already durable open trade idempotently."""
        account_id = self._required_text(account_id, "account_id")
        position_id = self._required_text(position_id, "position_id")

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT trade_id FROM trades WHERE account_id = ? AND position_id = ?",
                (account_id, position_id),
            ).fetchone()
            if row is None:
                raise TradeStoreError("cannot recover a close without a durable open trade")
            trade_id = row["trade_id"]
            for deal in deals:
                self._upsert_deal(
                    conn, trade_id, account_id, self._required_text(deal.get("deal_id"), "deal_id"),
                    deal["entry_type"], self._utc(deal["occurred_at_utc"]), self._number(deal.get("volume", 0), "volume"),
                    deal.get("price"), self._number(deal.get("commission", 0), "commission"),
                    self._number(deal.get("swap", 0), "swap"), self._number(deal.get("profit", 0), "profit"), deal.get("reason"),
                )
            self._aggregate_deals(conn, trade_id, close_reason)
            return self._row(conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone())

        return self._transaction(operation)

    def get_trade(self, trade_id: str, account_id: str) -> dict[str, Any] | None:
        return self._read_one("SELECT * FROM trades WHERE trade_id = ? AND account_id = ?", (trade_id, account_id))

    def get_by_position(self, account_id: str, position_id: str) -> dict[str, Any] | None:
        if "::" in account_id:
            self._adopt_duplicate_for_scoped_position(account_id, str(position_id))
        return self._read_one("SELECT * FROM trades WHERE account_id = ? AND position_id = ?", (account_id, str(position_id)))

    def adopt_legacy_trade(
        self, payload: dict[str, Any], deals: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any] | None:
        """Promote or merge one unscoped CSV trade proven to be this broker trade.

        A broker position ID alone is not sufficient because another login may
        reuse it.  Identity fields and at least two available economic fields
        must agree.  Ambiguous matches fail closed.
        """
        account_id = self._required_text(payload.get("account_id"), "account_id")
        hub_id = self._required_text(
            payload.get("hub_id") or account_id.split("::", 1)[0], "hub_id"
        )
        if "::" not in account_id:
            return None
        broker_login = self._optional_text(
            payload.get("broker_account_login"), "broker_account_login"
        )
        position_id = self._required_text(payload.get("position_id"), "position_id")
        evidence = dict(payload)
        deal_rows = list(deals)
        entries = [deal for deal in deal_rows if deal.get("entry_type") == "IN"]
        exits = [deal for deal in deal_rows if deal.get("entry_type") == "OUT"]
        if entries:
            evidence["entry_price"] = self._weighted_mapping_price(entries)
            evidence["entry_volume"] = sum(float(deal.get("volume") or 0) for deal in entries)
        if exits:
            evidence["exit_price"] = self._weighted_mapping_price(exits)
            evidence["profit"] = sum(float(deal.get("profit") or 0) for deal in exits)

        def operation(conn: sqlite3.Connection) -> dict[str, Any] | None:
            current = conn.execute(
                "SELECT * FROM trades WHERE account_id = ? AND position_id = ?",
                (account_id, position_id),
            ).fetchone()
            comparison = dict(current) if current is not None else evidence
            candidates = conn.execute(
                "SELECT * FROM trades WHERE hub_id IN (?, 'N/A') "
                "AND broker_account_login IS NULL "
                "AND position_id = ? AND account_id <> ?",
                (hub_id, position_id, account_id),
            ).fetchall()
            matches = [row for row in candidates if self._same_broker_trade(dict(row), comparison)]
            if len(matches) > 1:
                raise TradeStoreError(
                    f"ambiguous legacy trade match: hub={hub_id}, position={position_id}"
                )
            if not matches:
                return self._row(current) if current is not None else None
            legacy = matches[0]
            if current is None:
                conn.execute(
                    "UPDATE trade_deals SET account_id = ? WHERE trade_id = ?",
                    (account_id, legacy["trade_id"]),
                )
                conn.execute(
                    "UPDATE reconciliation_issues SET account_id = ? "
                    "WHERE account_id = ? AND position_id = ?",
                    (account_id, legacy["account_id"], position_id),
                )
                conn.execute(
                    """UPDATE trades SET account_id = ?, hub_id = ?, broker_account_login = ?,
                       strategy_id = ?, strategy_name = ?, order_id = COALESCE(order_id, ?),
                       magic = COALESCE(magic, ?), updated_at = ? WHERE trade_id = ?""",
                    (
                        account_id, hub_id, broker_login,
                        payload.get("strategy_id") or legacy["strategy_id"],
                        payload.get("strategy_name") or legacy["strategy_name"],
                        payload.get("order_id"), payload.get("magic"),
                        self._now(), legacy["trade_id"],
                    ),
                )
                return self._row(conn.execute(
                    "SELECT * FROM trades WHERE trade_id = ?", (legacy["trade_id"],)
                ).fetchone())

            for deal in conn.execute(
                "SELECT * FROM trade_deals WHERE trade_id = ?", (legacy["trade_id"],)
            ).fetchall():
                self._upsert_deal(
                    conn, current["trade_id"], account_id, deal["deal_id"],
                    deal["entry_type"], deal["occurred_at_utc"], deal["volume"],
                    deal["price"], deal["commission"], deal["swap"], deal["profit"], deal["reason"],
                )
            merge_fields = (
                "expected_entry", "entry_spread_points", "initial_sl", "initial_tp",
                "stop_distance_points", "take_distance_points", "target_r", "risk_usd",
                "equity_at_entry", "pnl_points", "pnl_r", "trade_duration_sec",
                "code_version", "config_version", "data_version",
            )
            assignments = ", ".join(
                f"{field} = COALESCE({field}, ?)" for field in merge_fields
            )
            conn.execute(
                f"UPDATE trades SET {assignments}, updated_at = ? WHERE trade_id = ?",
                (*[legacy[field] for field in merge_fields], self._now(), current["trade_id"]),
            )
            conn.execute("DELETE FROM trade_deals WHERE trade_id = ?", (legacy["trade_id"],))
            conn.execute(
                "DELETE FROM reconciliation_issues WHERE account_id = ? AND position_id = ?",
                (legacy["account_id"], position_id),
            )
            conn.execute("DELETE FROM trades WHERE trade_id = ?", (legacy["trade_id"],))
            return self._row(conn.execute(
                "SELECT * FROM trades WHERE trade_id = ?", (current["trade_id"],)
            ).fetchone())

        return self._transaction(operation)

    def mark_unscoped_legacy_hubs_unknown(self) -> int:
        """Replace importer-invented legacy hub labels with explicit N/A.

        New runtime trades always include a broker login.  An old row whose
        account scope is only the same logical hub has no durable evidence of
        where it originally traded, so it must not contribute to that hub's
        PnL or be displayed as known history.
        """
        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE trades SET hub_id = 'N/A', updated_at = ? "
                "WHERE broker_account_login IS NULL AND account_id = hub_id "
                "AND hub_id <> 'N/A'",
                (self._now(),),
            )
            return cursor.rowcount

        return self._transaction(operation)

    def reconcile_legacy_duplicates(
        self, account_id: str, strategy_id: str | None = None,
    ) -> int:
        """Collapse already-persisted legacy/scoped pairs outside history windows."""
        account_id = self._required_text(account_id, "account_id")
        if "::" not in account_id:
            return 0
        query = "SELECT position_id FROM trades WHERE account_id = ?"
        params: list[Any] = [account_id]
        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(self._required_text(strategy_id, "strategy_id"))
        positions = [row["position_id"] for row in self._read_many(query, tuple(params))]
        before = len(self.list_all_trades())
        for position_id in positions:
            self._adopt_duplicate_for_scoped_position(account_id, position_id)
        return before - len(self.list_all_trades())

    def _adopt_duplicate_for_scoped_position(self, account_id: str, position_id: str) -> None:
        hub_id, broker_login = account_id.split("::", 1)
        current = self._read_one(
            "SELECT * FROM trades WHERE account_id = ? AND position_id = ?",
            (account_id, position_id),
        )
        if current is not None:
            self.adopt_legacy_trade({
                **current,
                "hub_id": hub_id,
                "broker_account_login": broker_login,
            })

    @classmethod
    def _same_broker_trade(cls, legacy: dict[str, Any], current: dict[str, Any]) -> bool:
        def canonical(value: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

        if canonical(legacy.get("symbol")) != canonical(current.get("symbol")):
            return False
        if canonical(legacy.get("side")) != canonical(current.get("side")):
            return False
        legacy_strategies = {
            canonical(legacy.get("strategy_id")), canonical(legacy.get("strategy_name"))
        } - {""}
        current_strategies = {
            canonical(current.get("strategy_id")), canonical(current.get("strategy_name"))
        } - {""}
        if not legacy_strategies.intersection(current_strategies):
            return False
        compared = 0
        for field in ("magic", "entry_volume", "entry_price", "exit_price", "profit"):
            left, right = legacy.get(field), current.get(field)
            if left in (None, "") or right in (None, ""):
                continue
            if field == "entry_volume" and (
                float(left or 0) <= 0 or float(right or 0) <= 0
            ):
                # Older authoritative exports accidentally left `volume`
                # blank.  Their imported zero means unavailable, never a real
                # broker size, and must not veto otherwise strict evidence.
                continue
            compared += 1
            try:
                if not math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-6):
                    return False
            except (TypeError, ValueError):
                if canonical(left) != canonical(right):
                    return False
        return compared >= 2

    @staticmethod
    def _weighted_mapping_price(deals: Iterable[dict[str, Any]]) -> float | None:
        priced = [deal for deal in deals if deal.get("price") is not None and float(deal.get("volume") or 0) > 0]
        volume = sum(float(deal["volume"]) for deal in priced)
        if volume <= 0:
            return None
        return sum(float(deal["price"]) * float(deal["volume"]) for deal in priced) / volume

    def list_trades(self, account_id: str, *, closed_only: bool = False) -> list[dict[str, Any]]:
        account_id = self._required_text(account_id, "account_id")
        query = "SELECT * FROM trades WHERE account_id = ?"
        if closed_only:
            query += " AND status = 'CLOSED'"
        query += " ORDER BY COALESCE(exit_time_utc, entry_time_utc), trade_id"
        return self._read_many(query, (account_id,))

    def list_open_position_ids(self, account_id: str, strategy_id: str) -> set[str]:
        rows = self._read_many(
            "SELECT position_id FROM trades WHERE account_id = ? AND strategy_id = ? "
            "AND status = 'OPEN' AND position_id IS NOT NULL",
            (
                self._required_text(account_id, "account_id"),
                self._required_text(strategy_id, "strategy_id"),
            ),
        )
        return {str(row["position_id"]) for row in rows}

    def list_all_trades(
        self,
        *,
        closed_only: bool = False,
        hub_id: str | None = None,
        broker_account_login: str | int | None = None,
        strategy_id: str | None = None,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return a ledger view for exports and strategy-first reports.

        No caller receives an implicit account scope: all hubs are returned
        unless it deliberately supplies a hub, broker-login, or strategy
        filter.  CSV exporters consume this method; risk/execution paths do
        not.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if closed_only:
            clauses.append("status = 'CLOSED'")
        if hub_id is not None:
            clauses.append("hub_id = ?")
            params.append(self._required_text(hub_id, "hub_id"))
        if broker_account_login is not None:
            clauses.append("broker_account_login = ?")
            params.append(self._optional_text(broker_account_login, "broker_account_login"))
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(self._required_text(strategy_id, "strategy_id"))
        if (start_utc is None) != (end_utc is None):
            raise ValueError("start_utc and end_utc must be supplied together")
        if start_utc is not None and end_utc is not None:
            start, end = self._utc(start_utc), self._utc(end_utc)
            if start >= end:
                raise ValueError("invalid trade time window")
            clauses.extend(("COALESCE(exit_time_utc, entry_time_utc) >= ?", "COALESCE(exit_time_utc, entry_time_utc) < ?"))
            params.extend((start, end))
        query = "SELECT * FROM trades"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(exit_time_utc, entry_time_utc), trade_id"
        return self._read_many(query, tuple(params))

    def list_trade_deals(self, account_id: str, trade_id: str) -> list[dict[str, Any]]:
        """Return broker deals for one trade, ordered deterministically."""
        return self._read_many(
            "SELECT * FROM trade_deals WHERE account_id = ? AND trade_id = ? ORDER BY occurred_at_utc, deal_id",
            (self._required_text(account_id, "account_id"), self._required_text(trade_id, "trade_id")),
        )

    def list_deal_ids(
        self, account_id: str, start_utc: datetime, end_utc: datetime, *, entry_type: str, strategy_id: str,
    ) -> set[str]:
        if entry_type not in {"IN", "OUT"}:
            raise ValueError("invalid deal entry type")
        rows = self._read_many(
            """SELECT d.deal_id FROM trade_deals AS d
               JOIN trades AS t ON t.trade_id = d.trade_id
               WHERE d.account_id = ? AND d.entry_type = ? AND t.strategy_id = ?
                 AND d.occurred_at_utc >= ? AND d.occurred_at_utc < ?""",
            (
                self._required_text(account_id, "account_id"), entry_type,
                self._required_text(strategy_id, "strategy_id"), self._utc(start_utc), self._utc(end_utc),
            ),
        )
        return {row["deal_id"] for row in rows}

    def missing_deal_ids(
        self, account_id: str, strategy_id: str, *, entry_type: str, deal_ids: Iterable[str],
    ) -> set[str]:
        """Return broker deal IDs which are not attached to this strategy."""
        if entry_type not in {"IN", "OUT"}:
            raise ValueError("invalid deal entry type")
        requested = {self._required_text(value, "deal_id") for value in deal_ids}
        if not requested:
            return set()
        placeholders = ", ".join("?" for _ in requested)
        rows = self._read_many(
            f"""SELECT d.deal_id FROM trade_deals AS d
                JOIN trades AS t ON t.trade_id = d.trade_id
                WHERE d.account_id = ? AND d.entry_type = ? AND t.strategy_id = ?
                  AND d.deal_id IN ({placeholders})""",
            (
                self._required_text(account_id, "account_id"), entry_type,
                self._required_text(strategy_id, "strategy_id"), *sorted(requested),
            ),
        )
        return requested - {row["deal_id"] for row in rows}

    def record_reconciliation_issue(
        self, account_id: str, strategy_id: str, position_id: str,
        reason: str, broker_deal_ids: Iterable[str],
    ) -> None:
        account_id = self._required_text(account_id, "account_id")
        strategy_id = self._required_text(strategy_id, "strategy_id")
        position_id = self._required_text(position_id, "position_id")
        reason = self._required_text(reason, "reason")
        deal_ids = ";".join(sorted({self._required_text(value, "deal_id") for value in broker_deal_ids}))
        self._transaction(lambda conn: conn.execute(
            """INSERT INTO reconciliation_issues
               (account_id, strategy_id, position_id, reason, broker_deal_ids, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT DO UPDATE SET strategy_id = excluded.strategy_id,
               reason = excluded.reason, broker_deal_ids = excluded.broker_deal_ids,
               updated_at = excluded.updated_at""",
            (account_id, strategy_id, position_id, reason, deal_ids, self._now()),
        ))

    def clear_reconciliation_issue(self, account_id: str, strategy_id: str, position_id: str) -> None:
        self._transaction(lambda conn: conn.execute(
            """DELETE FROM reconciliation_issues
               WHERE account_id = ? AND strategy_id = ? AND position_id = ?""",
            (
                self._required_text(account_id, "account_id"),
                self._required_text(strategy_id, "strategy_id"),
                self._required_text(position_id, "position_id"),
            ),
        ))

    def list_reconciliation_issues(self, account_id: str, strategy_id: str) -> list[dict[str, Any]]:
        return self._read_many(
            """SELECT * FROM reconciliation_issues
               WHERE account_id = ? AND strategy_id = ? ORDER BY position_id""",
            (
                self._required_text(account_id, "account_id"),
                self._required_text(strategy_id, "strategy_id"),
            ),
        )

    def write_export_snapshot(self, writer: Callable[[list[dict[str, Any]]], None]) -> None:
        """Run ``writer`` against one export snapshot while holding the ledger lock.

        Holding the short SQLite transaction until ``os.replace`` completes
        prevents two runtime processes from replacing the global CSV with
        snapshots observed in the opposite order.  The callback must only
        write the supplied data and must never call the ledger again.
        """
        def operation(conn: sqlite3.Connection) -> None:
            rows = conn.execute(
                """SELECT t.*, COALESCE((
                    SELECT group_concat(deal_id, ';') FROM (
                        SELECT deal_id FROM trade_deals
                        WHERE trade_id = t.trade_id
                        ORDER BY occurred_at_utc, deal_id
                    )
                ), '') AS deal_ids
                FROM trades AS t
                ORDER BY COALESCE(t.exit_time_utc, t.entry_time_utc), t.trade_id"""
            ).fetchall()
            writer([self._row(row) for row in rows])

        self._transaction(operation)

    def legacy_csv_migration_applied(self, fingerprint: str) -> bool:
        fingerprint = self._required_text(fingerprint, "fingerprint")
        return self._read_one(
            "SELECT fingerprint FROM legacy_csv_migrations WHERE fingerprint = ?",
            (fingerprint,),
        ) is not None

    def record_legacy_csv_migration(
        self, fingerprint: str, source_path: str, row_count: int,
    ) -> None:
        fingerprint = self._required_text(fingerprint, "fingerprint")
        source_path = self._required_text(source_path, "source_path")
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            raise TradeStoreError("invalid legacy CSV row_count")
        self._transaction(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO legacy_csv_migrations "
            "(fingerprint, source_path, row_count, imported_at) VALUES (?, ?, ?, ?)",
            (fingerprint, source_path, row_count, self._now()),
        ))

    def get_last_closed_trade(self, account_id: str) -> dict[str, Any] | None:
        return self._read_one(
            "SELECT * FROM trades WHERE account_id = ? AND status = 'CLOSED' "
            "ORDER BY exit_time_utc DESC, updated_at DESC LIMIT 1", (self._required_text(account_id, "account_id"),)
        )

    def strategy_pnl(self, account_id: str, strategy_id: str, start_utc: datetime, end_utc: datetime) -> float:
        return self._pnl(account_id, start_utc, end_utc, strategy_id)

    def account_pnl(self, account_id: str, start_utc: datetime, end_utc: datetime) -> float:
        return self._pnl(account_id, start_utc, end_utc)

    def hub_pnl(self, hub_id: str, start_utc: datetime, end_utc: datetime) -> float:
        hub_id = self._required_text(hub_id, "hub_id")
        start, end = self._utc(start_utc), self._utc(end_utc)
        if start >= end:
            raise ValueError("invalid PnL time window")
        row = self._read_one(
            "SELECT COALESCE(SUM(profit), 0) AS pnl FROM trades "
            "WHERE hub_id = ? AND status = 'CLOSED' AND exit_time_utc >= ? AND exit_time_utc < ?",
            (hub_id, start, end),
        )
        return round(float(row["pnl"]), 2) if row is not None else 0.0

    @staticmethod
    def utc_day_window(now: datetime) -> tuple[datetime, datetime]:
        current = TradeStore._as_utc(now)
        start = datetime.combine(current.date(), datetime_time.min, tzinfo=timezone.utc)
        return start, start + timedelta(days=1)

    @staticmethod
    def utc_week_window(now: datetime) -> tuple[datetime, datetime]:
        current = TradeStore._as_utc(now)
        start = datetime.combine(current.date() - timedelta(days=current.weekday()), datetime_time.min, tzinfo=timezone.utc)
        return start, start + timedelta(days=7)

    def _pnl(self, account_id: str, start_utc: datetime, end_utc: datetime, strategy_id: str | None = None) -> float:
        account_id = self._required_text(account_id, "account_id")
        if strategy_id is not None:
            strategy_id = self._required_text(strategy_id, "strategy_id")
        start, end = self._utc(start_utc), self._utc(end_utc)
        if start >= end:
            raise ValueError("invalid PnL time window")
        query = "SELECT COALESCE(SUM(profit), 0) AS pnl FROM trades WHERE account_id = ? AND status = 'CLOSED' AND exit_time_utc >= ? AND exit_time_utc < ?"
        params: list[Any] = [account_id, start, end]
        if strategy_id is not None:
            query += " AND strategy_id = ?"
            params.append(strategy_id)
        row = self._read_one(query, tuple(params))
        return round(float(row["pnl"]), 2) if row is not None else 0.0

    def _aggregate_deals(self, conn: sqlite3.Connection, trade_id: str, close_reason: str | None = None) -> None:
        rows = conn.execute("SELECT * FROM trade_deals WHERE trade_id = ?", (trade_id,)).fetchall()
        entries = [row for row in rows if row["entry_type"] == "IN"]
        exits = [row for row in rows if row["entry_type"] == "OUT"]
        # Legacy/open-submit rows may predate a broker entry-deal ID.  Their
        # durable open row is still authoritative entry evidence, so an
        # exit-only reconciliation must not erase it and leave the trade OPEN.
        current = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        entry_volume = sum(row["volume"] for row in entries) or current["entry_volume"]
        closed_volume = sum(row["volume"] for row in exits)
        entry_price = self._weighted_price(entries) if entries else current["entry_price"]
        exit_price = self._weighted_price(exits)
        entry_time = min((row["occurred_at_utc"] for row in entries), default=current["entry_time_utc"])
        exit_time = max((row["occurred_at_utc"] for row in exits), default=None)
        commission = sum(row["commission"] for row in rows)
        swap = sum(row["swap"] for row in rows)
        profit = sum(row["profit"] for row in rows)
        points = None
        if entry_price is not None and exit_price is not None:
            points = exit_price - entry_price if current["side"] == "BUY" else entry_price - exit_price
        pnl_r = profit / current["risk_usd"] if current["risk_usd"] not in (None, 0) else None
        duration = None
        if entry_time and exit_time:
            duration = (datetime.fromisoformat(exit_time) - datetime.fromisoformat(entry_time)).total_seconds()
        status = "CLOSED" if entry_volume > 0 and closed_volume + 1e-9 >= entry_volume else "OPEN"
        conn.execute(
            """UPDATE trades SET entry_volume = ?, closed_volume = ?, entry_price = ?, exit_price = ?,
               entry_time_utc = COALESCE(?, entry_time_utc), exit_time_utc = ?, commission = ?, swap = ?,
               profit = ?, pnl_points = ?, pnl_r = ?, trade_duration_sec = ?,
               close_reason = COALESCE(?, close_reason), status = ?, updated_at = ? WHERE trade_id = ?""",
            (entry_volume, closed_volume, entry_price, exit_price, entry_time, exit_time, commission, swap, profit, points, pnl_r, duration, close_reason, status, self._now(), trade_id),
        )

    @staticmethod
    def _weighted_price(rows: list[sqlite3.Row]) -> float | None:
        volume = sum(row["volume"] for row in rows if row["price"] is not None)
        return None if volume == 0 else sum(row["price"] * row["volume"] for row in rows if row["price"] is not None) / volume

    def _upsert_deal(self, conn: sqlite3.Connection, trade_id: str, account_id: str, deal_id: str, entry_type: str, occurred_at_utc: str, volume: float, price: Any, commission: float, swap: float, profit: float, reason: str | None) -> None:
        if entry_type not in {"IN", "OUT"}:
            raise TradeStoreError("invalid deal entry type")
        conn.execute(
            """INSERT INTO trade_deals (account_id, deal_id, trade_id, entry_type, occurred_at_utc, volume, price, commission, swap, profit, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, deal_id) DO UPDATE SET trade_id = excluded.trade_id,
               entry_type = excluded.entry_type, occurred_at_utc = excluded.occurred_at_utc, volume = excluded.volume,
               price = excluded.price, commission = excluded.commission, swap = excluded.swap, profit = excluded.profit, reason = excluded.reason""",
            (account_id, deal_id, trade_id, entry_type, occurred_at_utc, volume, price, commission, swap, profit, reason),
        )

    def _normalise_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        for field in ("trade_id", "account_id", "strategy_id", "symbol", "position_id", "side"):
            record[field] = self._required_text(record.get(field), field)
        record["hub_id"] = self._required_text(record.get("hub_id") or record["account_id"], "hub_id")
        record["strategy_name"] = self._required_text(
            record.get("strategy_name") or record["strategy_id"], "strategy_name"
        )
        if record.get("broker_account_login") is not None:
            record["broker_account_login"] = self._optional_text(
                record["broker_account_login"], "broker_account_login"
            )
        record["entry_time_utc"] = self._utc(record.get("entry_time_utc"))
        record["entry_volume"] = self._number(record.get("entry_volume", 0), "entry_volume")
        record["status"] = "OPEN"
        record["closed_volume"] = 0
        for field in ("magic", "order_id", "deal_id", "code_version", "config_version", "data_version"):
            if record.get(field) is not None:
                record[field] = str(record[field])
        for field in ("entry_price", "expected_entry", "entry_spread_points", "initial_sl", "initial_tp", "stop_distance_points", "take_distance_points", "target_r", "risk_usd", "equity_at_entry"):
            if record.get(field) is not None:
                record[field] = self._number(record[field], field)
        return {key: record.get(key) for key in (
            "trade_id", "account_id", "hub_id", "broker_account_login", "strategy_id", "strategy_name",
            "symbol", "magic", "order_id", "position_id", "side",
            "entry_time_utc", "entry_volume", "closed_volume", "entry_price", "expected_entry", "entry_spread_points",
            "initial_sl", "initial_tp", "stop_distance_points", "take_distance_points", "target_r", "risk_usd",
            "equity_at_entry", "code_version", "config_version", "data_version", "status", "deal_id",
        ) if key != "deal_id"}

    def _normalise_close(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        record["exit_time_utc"] = self._utc(record.get("exit_time_utc"))
        for field in ("profit", "commission", "swap"):
            record[field] = self._number(record.get(field, 0), field)
        for field in ("exit_price", "pnl_points", "pnl_r", "trade_duration_sec", "volume"):
            if record.get(field) is not None:
                record[field] = self._number(record[field], field)
        if record.get("deal_id") is not None:
            record["deal_id"] = str(record["deal_id"])
        return record

    def _transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        last_error: BaseException | None = None
        for attempt in range(self.lock_retry_attempts):
            conn = None
            try:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                conn.execute("COMMIT")
                return result
            except (sqlite3.DatabaseError, OSError) as error:
                if conn is not None:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.DatabaseError:
                        pass
                last_error = error
                if isinstance(error, sqlite3.OperationalError) and any(token in str(error).lower() for token in ("locked", "busy")) and attempt + 1 < self.lock_retry_attempts:
                    time.sleep(self.lock_retry_base_sec * (attempt + 1))
                    continue
                break
            finally:
                if conn is not None:
                    conn.close()
        raise TradeStoreError(f"trade ledger operation failed: {last_error}") from last_error

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.executescript(self._SCHEMA)
            self._migrate_schema(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_hub_exit "
                "ON trades(hub_id, exit_time_utc, strategy_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_broker_login_exit "
                "ON trades(broker_account_login, exit_time_utc, strategy_id)"
            )
            return conn
        except BaseException:
            conn.close()
            raise

    def _read_one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._read_many(query, params)
        return rows[0] if rows else None

    def _read_many(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        def operation(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [self._row(row) for row in conn.execute(query, params).fetchall()]
        return self._transaction(operation)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TradeStoreError(f"{field} is required")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any, field: str) -> str:
        if isinstance(value, bool) or value is None:
            raise TradeStoreError(f"invalid {field}")
        text = str(value).strip()
        if not text:
            raise TradeStoreError(f"invalid {field}")
        return text

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Add identity metadata to pre-P0-T06A ledgers without data loss."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        additions = {
            "hub_id": "TEXT",
            "broker_account_login": "TEXT",
            "strategy_name": "TEXT",
        }
        for column, definition in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
        # Old rows predate this metadata.  account_id has always meant the
        # logical hub; an unknown broker login stays NULL instead of being
        # guessed from a current account configuration.
        conn.execute(
            "UPDATE trades SET hub_id = account_id WHERE hub_id IS NULL OR trim(hub_id) = ''"
        )
        conn.execute(
            "UPDATE trades SET strategy_name = strategy_id "
            "WHERE strategy_name IS NULL OR trim(strategy_name) = ''"
        )
        issue_info = list(conn.execute("PRAGMA table_info(reconciliation_issues)"))
        issue_columns = {row["name"] for row in issue_info}
        primary_key = [
            row["name"] for row in sorted(issue_info, key=lambda item: item["pk"])
            if row["pk"]
        ]
        if primary_key != ["account_id", "strategy_id", "position_id"]:
            # ALTER TABLE can add strategy_id but cannot replace the legacy
            # (account_id, position_id) primary key. Rebuild so two strategies
            # on one broker account can each own their __parity__ marker.
            conn.execute("DROP TABLE IF EXISTS reconciliation_issues_migrated")
            conn.execute(
                """CREATE TABLE reconciliation_issues_migrated (
                       account_id TEXT NOT NULL,
                       strategy_id TEXT NOT NULL,
                       position_id TEXT NOT NULL,
                       reason TEXT NOT NULL,
                       broker_deal_ids TEXT NOT NULL,
                       updated_at REAL NOT NULL,
                       PRIMARY KEY (account_id, strategy_id, position_id)
                   )"""
            )
            strategy_expression = (
                "NULLIF(trim(issue.strategy_id), '')" if "strategy_id" in issue_columns else
                "(SELECT trade.strategy_id FROM trades AS trade "
                " WHERE trade.account_id = issue.account_id "
                " AND trade.position_id = issue.position_id)"
            )
            conn.execute(
                f"""INSERT OR REPLACE INTO reconciliation_issues_migrated
                       (account_id, strategy_id, position_id, reason, broker_deal_ids, updated_at)
                    SELECT issue.account_id, {strategy_expression}, issue.position_id,
                           issue.reason, issue.broker_deal_ids, issue.updated_at
                    FROM reconciliation_issues AS issue
                    WHERE {strategy_expression} IS NOT NULL"""
            )
            conn.execute("DROP TABLE reconciliation_issues")
            conn.execute(
                "ALTER TABLE reconciliation_issues_migrated RENAME TO reconciliation_issues"
            )

    @staticmethod
    def _number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise TradeStoreError(f"invalid {field}")
        return float(value)

    @classmethod
    def _utc(cls, value: Any) -> str:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise TradeStoreError("invalid UTC timestamp") from exc
        if not isinstance(value, datetime):
            raise TradeStoreError("UTC timestamp is required")
        return cls._as_utc(value).isoformat()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
