"""Ledger-backed analytics facade with one global compatibility CSV.

``TradeStore`` is authoritative.  ``runtime/analytics/trades.csv`` is normally
an atomic export across every hub and strategy.  A pre-ledger CSV is imported
once at startup as an explicit compatibility migration, then remains export-only.
"""

from __future__ import annotations

import csv
import hashlib
import os
import stat
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from analytics.trade_store import TradeStore, TradeStoreError


ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_DIR = ROOT / "runtime" / "analytics"
CSV_FILE = ANALYTICS_DIR / "trades.csv"  # export only; never a source of truth

NA = "N/A"
ENRICHED_NA_FIELDS = frozenset({"pnl_r", "pnl_points", "trade_duration_sec"})


class TradeExportError(RuntimeError):
    """The CSV projection failed after the authoritative ledger committed."""


class TradeLogger:
    def __init__(
        self,
        account_id: str,
        *,
        store: TradeStore | None = None,
        csv_path: Path | str | None = None,
        strategy_id: str | None = None,
        strategy_name: str | None = None,
        broker_account_login: str | int | None = None,
        export_replace_attempts: int = 6,
        export_replace_backoff_sec: float = 0.10,
    ):
        if export_replace_attempts < 1 or export_replace_backoff_sec < 0:
            raise ValueError("invalid CSV export retry configuration")
        # hub_id is the operator-facing slot.  account_id is the durable
        # broker-history scope: reusing one hub with another login must not
        # collide with repeated MT5 position/order/deal ticket numbers.
        self.hub_id = account_id
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        self.broker_account_login = (
            None if broker_account_login is None else str(broker_account_login)
        )
        self.account_id = (
            account_id
            if self.broker_account_login is None
            else f"{account_id}::{self.broker_account_login}"
        )
        self.export_replace_attempts = export_replace_attempts
        self.export_replace_backoff_sec = export_replace_backoff_sec
        self.last_export_error: str | None = None
        injected_store = store is not None
        migrate_default_paths = not injected_store and csv_path is None
        self.store = store or TradeStore()
        self.csv_headers = [
            "trade_id", "hub_id", "account_id", "broker_account_login", "strategy_id", "strategy_name",
            "symbol", "magic", "order_id", "position_id", "deal_ids",
            "ticket", "side", "entry_time", "exit_time", "expected_entry", "actual_entry", "entry_slippage_points",
            "entry_spread_points", "volume", "closed_volume", "initial_sl", "initial_tp", "stop_distance_points",
            "take_distance_points", "target_r", "risk_usd", "equity_at_entry", "exit_price", "close_reason",
            "commission", "swap", "pnl_usd", "pnl_points", "pnl_r", "trade_duration_sec", "code_version",
            "config_version", "data_version", "status", "connection_latency_ms", "tick_age_sec",
        ]
        # Normal runtime instances share CSV_FILE.  An injected store belongs
        # to an isolated runtime/test root, so its implicit export must stay
        # beside that store instead of ever touching the production CSV.
        if csv_path is not None:
            self.csv_file = Path(csv_path)
        elif injected_store:
            self.csv_file = self.store.db_path.parent / "analytics" / "trades.csv"
        else:
            self.csv_file = CSV_FILE
        if migrate_default_paths or csv_path is not None:
            self._migrate_legacy_csv()
        # Rows without a broker login came from formats that could not prove
        # the historical hub.  Keep their internal legacy scope, but never
        # present the importing/current hub as a historical fact.
        self.store.mark_unscoped_legacy_hubs_unknown()
        if self.broker_account_login is not None:
            self.store.reconcile_legacy_duplicates(self.account_id, self.strategy_id)

    def _migrate_legacy_csv(self) -> None:
        """Import pre-ledger CSV rows once before any authoritative export.

        The source file is never modified here.  A malformed row raises and
        therefore prevents startup/export from replacing the only legacy copy.
        Successfully imported rows are idempotent on later strategy startups.
        """
        if not self.csv_file.is_file() or self.csv_file.stat().st_size == 0:
            return
        try:
            fingerprint = hashlib.sha256(self.csv_file.read_bytes()).hexdigest()
            if self.store.legacy_csv_migration_applied(fingerprint):
                return
            with self.csv_file.open(newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            for index, row in enumerate(rows, start=2):
                if not any(value not in (None, "") for value in row.values()):
                    continue
                self._import_legacy_csv_row(row, line=index)
            self.store.record_legacy_csv_migration(
                fingerprint, str(self.csv_file.resolve()), len(rows)
            )
        except (OSError, csv.Error, KeyError, TypeError, ValueError, TradeStoreError) as exc:
            raise TradeStoreError(
                f"legacy trades.csv migration failed; source preserved: {exc}"
            ) from exc

    def _import_legacy_csv_row(self, row: dict[str, str], *, line: int) -> None:
        def text(*names, required=False):
            for name in names:
                value = row.get(name)
                if value is not None and str(value).strip():
                    return str(value).strip()
            if required:
                raise ValueError(f"line {line}: missing {'/'.join(names)}")
            return None

        def number(*names, default=None):
            value = text(*names)
            if value is None or value.upper() == NA:
                return default
            return float(value)

        raw_account_id = text("account_id")
        broker_login = text("broker_account_login") or (
            raw_account_id.split("::", 1)[1]
            if raw_account_id and "::" in raw_account_id else None
        )
        if broker_login:
            hub_id = text("hub_id") or (
                raw_account_id.split("::", 1)[0] if raw_account_id else None
            ) or self.hub_id
            account_scope = (
                raw_account_id
                if raw_account_id and "::" in raw_account_id
                else f"{hub_id}::{broker_login}"
            )
        else:
            # An unscoped CSV cannot prove which logical hub imported it in
            # the past.  `account_id` stays as a compatibility key, while the
            # operator-facing hub is explicitly unknown.
            hub_id = NA
            account_scope = raw_account_id or NA
        strategy_id = text("strategy_id", "strategy_name", required=True)
        strategy_name = text("strategy_name", "strategy_id", required=True)
        position_id = text("position_id", "ticket", required=True)
        entry_time = text("entry_time", "entry_time_utc", required=True)
        trade_id = text("trade_id") or (
            "legacy_csv_" + hashlib.sha256(
                repr(sorted(row.items())).encode("utf-8")
            ).hexdigest()
        )
        entry_volume = number("volume", "entry_volume")
        if entry_volume is None or entry_volume <= 0:
            # Releases before this fix exported a blank `volume` column but
            # retained the complete closed volume.  A zero-volume broker
            # position is impossible, so this is recovery evidence, not a
            # guessed trade size.
            entry_volume = number("closed_volume", default=0.0)
        trade = self.store.upsert_open({
            "trade_id": trade_id,
            "account_id": account_scope,
            "hub_id": hub_id,
            "broker_account_login": broker_login,
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "symbol": text("symbol", required=True),
            "magic": text("magic"),
            "order_id": text("order_id"),
            "position_id": position_id,
            "side": text("side", required=True),
            "entry_time_utc": entry_time,
            "entry_volume": entry_volume,
            "entry_price": number("actual_entry", "entry_price"),
            "expected_entry": number("expected_entry"),
            "entry_spread_points": number("entry_spread_points"),
            "initial_sl": number("initial_sl"),
            "initial_tp": number("initial_tp"),
            "stop_distance_points": number("stop_distance_points"),
            "take_distance_points": number("take_distance_points"),
            "target_r": number("target_r"),
            "risk_usd": number("risk_usd"),
            "equity_at_entry": number("equity_at_entry"),
            "code_version": text("code_version"),
            "config_version": text("config_version"),
            "data_version": text("data_version"),
        })
        if trade["trade_id"] != trade_id:
            raise ValueError(
                f"line {line}: broker position collides with trade {trade['trade_id']}"
            )
        exit_time = text("exit_time", "exit_time_utc")
        status = (text("status") or "").upper()
        if exit_time or status == "CLOSED":
            if not exit_time:
                raise ValueError(f"line {line}: closed trade has no exit_time")
            self.store.upsert_close(trade["trade_id"], trade["account_id"], {
                "exit_time_utc": exit_time,
                "exit_price": number("exit_price"),
                "profit": number("pnl_usd", "profit", default=0.0),
                "commission": number("commission", default=0.0),
                "swap": number("swap", default=0.0),
                "pnl_points": number("pnl_points"),
                "pnl_r": number("pnl_r"),
                "trade_duration_sec": number("trade_duration_sec"),
                "close_reason": text("close_reason"),
                "volume": number("closed_volume", "volume"),
            })

    def generate_trade_id(self, strategy_name: str) -> str:
        return f"{strategy_name}_{uuid.uuid4().hex}"

    def record_trade_open(
        self, ticket, magic, strategy_name, symbol, side, entry_time, expected_entry, actual_entry,
        entry_spread_points, volume, initial_sl, initial_tp, stop_distance_points, take_distance_points,
        target_r, risk_usd, equity_at_entry, connection_latency_ms=None, tick_age_sec=None,
        *, order_id=None, position_id=None, deal_id=None, code_version=None, config_version=None, data_version=None,
    ) -> str:
        trade_id = self.generate_trade_id(strategy_name)
        trade = self.store.upsert_open({
            "trade_id": trade_id,
            "account_id": self.account_id,
            "hub_id": self.hub_id,
            "broker_account_login": self.broker_account_login,
            "strategy_id": self.strategy_id or strategy_name,
            "strategy_name": self.strategy_name or strategy_name,
            "symbol": symbol,
            "magic": magic,
            "order_id": order_id,
            "position_id": str(position_id if position_id is not None else ticket),
            "side": side,
            "entry_time_utc": entry_time,
            "entry_volume": volume,
            "entry_price": actual_entry,
            "expected_entry": expected_entry,
            "entry_spread_points": entry_spread_points,
            "initial_sl": initial_sl,
            "initial_tp": initial_tp,
            "stop_distance_points": stop_distance_points,
            "take_distance_points": take_distance_points,
            "target_r": target_r,
            "risk_usd": risk_usd,
            "equity_at_entry": equity_at_entry,
            "code_version": code_version,
            "config_version": config_version,
            "data_version": data_version,
            "deal_id": deal_id,
        })
        return trade["trade_id"]

    def record_trade_close(
        self, trade_id, exit_time, exit_price, close_reason, pnl_usd, pnl_points, pnl_r,
        trade_duration_sec, *, deal_id=None, volume=None, commission=0.0, swap=0.0,
    ) -> bool:
        self.store.upsert_close(trade_id, self.account_id, {
            "exit_time_utc": exit_time, "exit_price": exit_price, "close_reason": close_reason,
            "profit": pnl_usd, "pnl_points": pnl_points, "pnl_r": pnl_r,
            "trade_duration_sec": trade_duration_sec, "deal_id": deal_id, "volume": volume,
            "commission": commission, "swap": swap,
        })
        self._export_after_durable_write()
        return True

    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        return self.store.get_trade(trade_id, self.account_id)

    def trade_exists_by_ticket(self, ticket) -> bool:
        return self.store.get_by_position(self.account_id, str(ticket)) is not None

    def get_trade_by_ticket(self, ticket) -> dict[str, Any] | None:
        return self._legacy_payload(self.store.get_by_position(self.account_id, str(ticket)))

    def get_last_closed_trade(self) -> dict[str, Any] | None:
        return self._legacy_payload(self.store.get_last_closed_trade(self.account_id))

    def open_position_ids(self) -> set[str]:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        return self.store.list_open_position_ids(self.account_id, self.strategy_id)

    def record_recovered_trade(self, payload: dict[str, Any], deals: list[dict[str, Any]] | None = None) -> str:
        """Import a broker-history aggregate idempotently into the ledger."""
        recovered = self.store.upsert_recovered_trade(self._store_payload(payload), deals or [])
        self._export_after_durable_write()
        return recovered["trade_id"]

    def record_recovered_close(self, ticket, deals: list[dict[str, Any]], close_reason: str | None) -> str:
        recovered = self.store.upsert_recovered_close_by_position(
            self.account_id, str(ticket), deals, close_reason,
        )
        self._export_after_durable_write()
        return recovered["trade_id"]

    def record_reconciliation_issue(self, ticket, reason: str, broker_deal_ids: list[str]) -> None:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        self.store.record_reconciliation_issue(
            self.account_id, self.strategy_id, str(ticket), reason, broker_deal_ids,
        )

    def clear_reconciliation_issue(self, ticket) -> None:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        self.store.clear_reconciliation_issue(self.account_id, self.strategy_id, str(ticket))

    def reconciliation_issues(self) -> list[dict[str, Any]]:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        return self.store.list_reconciliation_issues(self.account_id, self.strategy_id)

    def ledger_deal_ids(self, start: datetime, end: datetime, *, entry_type: str) -> set[str]:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        return self.store.list_deal_ids(
            self.account_id, start, end, entry_type=entry_type, strategy_id=self.strategy_id,
        )

    def missing_ledger_deal_ids(self, deal_ids, *, entry_type: str) -> set[str]:
        if not self.strategy_id:
            raise RuntimeError("STRATEGY_ID_UNAVAILABLE")
        return self.store.missing_deal_ids(
            self.account_id, self.strategy_id, entry_type=entry_type, deal_ids=deal_ids,
        )

    def get_daily_strategy_pnl(self, strategy_name: str, broker_now: datetime) -> float:
        start, end = self.store.utc_day_window(broker_now)
        return self.store.strategy_pnl(self.account_id, strategy_name, start, end)

    def get_weekly_strategy_pnl(self, strategy_name: str, broker_now: datetime) -> float:
        start, end = self.store.utc_week_window(broker_now)
        return self.store.strategy_pnl(self.account_id, strategy_name, start, end)

    def get_daily_account_pnl(self, account_id: str, broker_now: datetime) -> float:
        start, end = self.store.utc_day_window(broker_now)
        return self.store.account_pnl(self.account_id, start, end)

    def get_weekly_account_pnl(self, account_id: str, broker_now: datetime) -> float:
        start, end = self.store.utc_week_window(broker_now)
        return self.store.account_pnl(self.account_id, start, end)

    def get_daily_hub_pnl(self, broker_now: datetime) -> float:
        start, end = self.store.utc_day_window(broker_now)
        return self.store.hub_pnl(self.hub_id, start, end)

    def get_weekly_hub_pnl(self, broker_now: datetime) -> float:
        start, end = self.store.utc_week_window(broker_now)
        return self.store.hub_pnl(self.hub_id, start, end)

    def export_csv(self) -> Path:
        """Atomically export the complete ledger; never import this file."""
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        def write_snapshot(trades: list[dict[str, Any]]) -> None:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.csv_file.parent,
                prefix=f".{self.csv_file.name}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=self.csv_headers)
                    writer.writeheader()
                    for trade in trades:
                        payload = self._legacy_payload(trade)
                        writer.writerow({
                            key: self._csv_value(key, value)
                            for key, value in payload.items()
                            if key in self.csv_headers
                        })
                    stream.flush()
                    os.fsync(stream.fileno())
                self._replace_csv(temporary)
            except BaseException:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

        try:
            self.store.write_export_snapshot(write_snapshot)
        except TradeStoreError as exc:
            raise TradeExportError(f"trades.csv export failed: {exc}") from exc
        self.last_export_error = None
        return self.csv_file

    def _replace_csv(self, temporary: Path) -> None:
        """Replace the human export across common transient Windows locks."""
        last_error: PermissionError | None = None
        for attempt in range(self.export_replace_attempts):
            try:
                os.replace(temporary, self.csv_file)
                return
            except PermissionError as exc:
                last_error = exc
                if self.csv_file.exists():
                    try:
                        mode = self.csv_file.stat().st_mode
                        if not mode & stat.S_IWRITE:
                            self.csv_file.chmod(mode | stat.S_IWRITE)
                    except OSError:
                        pass
                if attempt + 1 < self.export_replace_attempts:
                    time.sleep(self.export_replace_backoff_sec * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _export_after_durable_write(self) -> bool:
        """Best-effort projection after SQLite has already committed.

        ``trades.csv`` is export-only.  A Windows reader holding the target
        file must not make a confirmed ledger close look unhandled or prevent
        execution-cache cleanup.  A later successful export always publishes
        the complete ledger snapshot, so no incremental CSV data is lost.
        """
        try:
            self.export_csv()
            return True
        except TradeExportError as exc:
            message = str(exc)
            if message != self.last_export_error:
                print(f"[TradeLogger {self.strategy_id or self.strategy_name}] CSV export deferred: {message}")
            self.last_export_error = message
            return False

    def export_status_snapshot(self) -> dict[str, Any]:
        return {
            "status": "WARNING" if self.last_export_error else "SYNCED",
            "error": self.last_export_error,
        }

    # Retained for focused compatibility tests. Runtime writes
    # use export_csv(), which gets its data solely from the durable store.
    def _append_csv(self, payload: dict[str, Any]) -> None:
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_file.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.csv_headers)
            writer.writerow({key: self._csv_value(key, payload.get(key)) for key in self.csv_headers})

    @staticmethod
    def _csv_value(key: str, value: Any) -> Any:
        return NA if value is None and key in ENRICHED_NA_FIELDS else value

    def _store_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_id": payload.get("trade_id") or self.generate_trade_id(payload["strategy_name"]),
            "account_id": self.account_id,
            "hub_id": self.hub_id,
            "broker_account_login": self.broker_account_login,
            "strategy_id": self.strategy_id or payload["strategy_name"],
            "strategy_name": self.strategy_name or payload["strategy_name"],
            "symbol": payload["symbol"],
            "magic": payload.get("magic"),
            "order_id": payload.get("order_id"),
            "position_id": str(payload.get("position_id") or payload["ticket"]),
            "side": payload["side"],
            "entry_time_utc": payload["entry_time"],
            "entry_volume": payload["volume"],
            "entry_price": payload.get("actual_entry"),
            "expected_entry": payload.get("expected_entry"),
            "entry_spread_points": payload.get("entry_spread_points"),
            "initial_sl": payload.get("initial_sl"),
            "initial_tp": payload.get("initial_tp"),
            "stop_distance_points": payload.get("stop_distance_points"),
            "take_distance_points": payload.get("take_distance_points"),
            "target_r": payload.get("target_r"),
            "risk_usd": payload.get("risk_usd"),
            "equity_at_entry": payload.get("equity_at_entry"),
            "code_version": payload.get("code_version"),
            "config_version": payload.get("config_version"),
            "data_version": payload.get("data_version"),
            "close_reason": payload.get("close_reason"),
        }

    @staticmethod
    def _legacy_payload(trade: dict[str, Any] | None) -> dict[str, Any] | None:
        if trade is None:
            return None
        payload = {
            **trade,
            "hub_id": trade.get("hub_id") or trade["account_id"],
            "strategy_name": trade.get("strategy_name") or trade["strategy_id"],
            "ticket": trade["position_id"],
            "entry_time": trade["entry_time_utc"],
            "exit_time": trade["exit_time_utc"],
            "actual_entry": trade["entry_price"],
            "volume": trade["entry_volume"] if trade["entry_volume"] > 0 else NA,
            "pnl_usd": trade["profit"],
        }
        for field in ENRICHED_NA_FIELDS:
            if payload.get(field) is None:
                payload[field] = NA
        return payload
