"""Recover broker history into the durable trade/deal ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

import MetaTrader5 as mt5


class ReconciliationWatermarkStore:
    """Small durable cursor, advanced only after a successful ledger import."""

    def __init__(
        self,
        path: Path | str,
        *,
        replace_attempts: int = 6,
        replace_backoff_sec: float = 0.10,
    ):
        if replace_attempts < 1 or replace_backoff_sec < 0:
            raise ValueError("invalid watermark replace retry configuration")
        self.path = Path(path)
        self.replace_attempts = replace_attempts
        self.replace_backoff_sec = replace_backoff_sec
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> datetime | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            value = payload["watermark_utc"]
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid reconciliation watermark {self.path}: {exc}") from exc

    def save(self, value: datetime) -> None:
        payload = json.dumps({"watermark_utc": value.astimezone(timezone.utc).isoformat()}, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(temporary)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _replace(self, temporary: str) -> None:
        """Atomically replace a watermark across common Windows file states.

        A runtime directory copied from backup may leave the existing JSON
        with the Windows read-only attribute. Antivirus/indexing can also hold
        a short sharing lock. Both surface as PermissionError from os.replace
        even though creating the sibling temp file succeeded.
        """
        last_error: PermissionError | None = None
        for attempt in range(self.replace_attempts):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError as exc:
                last_error = exc
                if self.path.exists():
                    try:
                        mode = self.path.stat().st_mode
                        if not mode & stat.S_IWRITE:
                            self.path.chmod(mode | stat.S_IWRITE)
                    except OSError:
                        # A real ACL denial remains fail-closed after bounded
                        # retries; never delete the old watermark.
                        pass
                if attempt + 1 < self.replace_attempts:
                    time.sleep(self.replace_backoff_sec * (attempt + 1))
        assert last_error is not None
        raise last_error


class TradeReconciliation:
    def __init__(self, broker, trade_logger, state_manager, alerts, strategy_config,
                 *, bootstrap_days=30, overlap_sec=300, watermark_path=None):
        self.broker = broker
        self.trade_logger = trade_logger
        self.state_manager = state_manager
        self.alerts = alerts
        self.strategy_config = strategy_config
        if bootstrap_days < 1 or overlap_sec < 0:
            raise ValueError("invalid reconciliation catch-up configuration")
        self.bootstrap_days = bootstrap_days
        self.overlap_sec = overlap_sec
        root = Path(__file__).resolve().parents[1] / "runtime"
        safe_account = self._safe_filename_component(trade_logger.account_id)
        safe_strategy = self._safe_filename_component(
            getattr(trade_logger, "strategy_id", strategy_config.STRATEGY_NAME)
        )
        self.watermark_store = ReconciliationWatermarkStore(
            watermark_path or root / f"trade_reconciliation_{safe_account}_{safe_strategy}.json"
        )
        self._health = "UNKNOWN"
        self._last_alert_at = 0.0

    @staticmethod
    def _safe_filename_component(value):
        """Return a portable filename component for Windows and POSIX."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
        if not safe:
            raise ValueError("empty reconciliation identity")
        return safe

    def reconcile(self):
        execution_cache = self.state_manager.state.get("execution_cache", {})
        end = self._utc(self.broker.broker_now())
        watermark = self.watermark_store.load()
        start = (
            watermark - timedelta(seconds=self.overlap_sec)
            if watermark is not None
            else end - timedelta(days=self.bootstrap_days)
        )
        query_bounds = getattr(self.broker, "history_query_bounds", None)
        query_start, query_end = (
            query_bounds(start, end) if callable(query_bounds) else (start, end)
        )
        deals = mt5.history_deals_get(query_start, query_end)
        if deals is None:
            return 0
        deals = list(deals)

        owned_position_ids = {str(ticket) for ticket in execution_cache}
        open_position_ids = getattr(self.trade_logger, "open_position_ids", None)
        if callable(open_position_ids):
            owned_position_ids.update(open_position_ids())

        # Manual close deals may have magic=0 and may already be behind the
        # rolling watermark. Query exact broker position histories for every
        # durable OPEN row/cache entry, then deduplicate the combined result.
        seen_deal_ids = {str(getattr(deal, "ticket", "")) for deal in deals}
        for position_id in sorted(owned_position_ids):
            try:
                position_deals = mt5.history_deals_get(position=int(position_id))
            except Exception:
                position_deals = None
            for deal in position_deals or ():
                deal_id = str(getattr(deal, "ticket", ""))
                if deal_id and deal_id not in seen_deal_ids:
                    deals.append(deal)
                    seen_deal_ids.add(deal_id)

        grouped = {}
        close_entries = {
            getattr(mt5, "DEAL_ENTRY_OUT", object()),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", object()),
        }
        reversal_entry = getattr(mt5, "DEAL_ENTRY_INOUT", object())
        # A strategy-magic IN deal is ownership evidence for a manual OUT deal
        # included in the same broker response.
        owned_position_ids.update(
            str(deal.position_id)
            for deal in deals
            if getattr(deal, "symbol", None) == self.strategy_config.SYMBOL
            and getattr(deal, "magic", None) == self.strategy_config.MAGIC
            and getattr(deal, "entry", None) == getattr(mt5, "DEAL_ENTRY_IN", None)
        )
        broker_close_ids = set()
        for deal in deals:
            if deal.symbol != self.strategy_config.SYMBOL:
                continue
            position_id = str(deal.position_id)
            if deal.entry == mt5.DEAL_ENTRY_IN:
                if deal.magic != self.strategy_config.MAGIC:
                    continue
                group = grouped.setdefault(position_id, {"open": [], "close": [], "reversal": False})
                group["open"].append(deal)
            elif deal.entry in close_entries:
                if deal.magic != self.strategy_config.MAGIC and position_id not in owned_position_ids:
                    continue
                group = grouped.setdefault(position_id, {"open": [], "close": [], "reversal": False})
                group["close"].append(deal)
                broker_close_ids.add(str(deal.ticket))
            elif deal.entry == reversal_entry:
                if deal.magic != self.strategy_config.MAGIC and position_id not in owned_position_ids:
                    continue
                group = grouped.setdefault(position_id, {"open": [], "close": [], "reversal": False})
                # A reversal may contain both a close and a new entry. Do not
                # pretend it is a plain OUT deal and corrupt the new leg.
                group["close"].append(deal)
                group["reversal"] = True
                broker_close_ids.add(str(deal.ticket))

        recovered = 0
        complete = True
        for position_id, group in grouped.items():
            if not group["close"]:
                continue
            if group["reversal"]:
                complete = False
                self.trade_logger.record_reconciliation_issue(
                    position_id, "REVERSAL_DEAL_REQUIRES_SPLIT", sorted(str(deal.ticket) for deal in group["close"]),
                )
                continue
            cache = execution_cache.get(position_id)
            existing = self.trade_logger.get_trade_by_ticket(position_id)
            already_recorded = existing is not None
            was_open = existing is not None and existing.get("status") == "OPEN"
            if not group["open"] and existing is not None:
                close_deals = sorted(group["close"], key=lambda item: item.time)
                close_reason = self._close_reason(close_deals)
                trade_id = self.trade_logger.record_recovered_close(
                    position_id, [self._deal_row(deal, "OUT") for deal in close_deals], close_reason,
                )
                if cache is not None:
                    self.state_manager.clear_execution_cache(position_id)
                self.trade_logger.clear_reconciliation_issue(position_id)
                if cache is not None or was_open:
                    self._alert_recovered_close(trade_id)
                continue

            if not group["open"]:
                group["open"] = self._lookup_position_entries(position_id, end)
            if not group["open"]:
                # Do not manufacture entry-derived fields.  The issue is
                # durable and the watermark remains behind this close so a
                # later bounded history query can repair it.
                complete = False
                self.trade_logger.record_reconciliation_issue(
                    position_id, "ENTRY_METADATA_UNAVAILABLE", sorted(str(deal.ticket) for deal in group["close"]),
                )
                continue

            open_deal = min(group["open"], key=lambda item: item.time)
            close_deals = sorted(group["close"], key=lambda item: item.time)
            close_reason = self._close_reason(close_deals)
            risk_usd = cache.get("risk_usd") if cache else None
            payload = {
                "trade_id": cache.get("trade_id") if cache else None,
                "ticket": position_id,
                "position_id": position_id,
                "order_id": getattr(open_deal, "order", None),
                "magic": self.strategy_config.MAGIC,
                "strategy_name": self.strategy_config.STRATEGY_NAME,
                "symbol": self.strategy_config.SYMBOL,
                "side": "BUY" if open_deal.type == mt5.ORDER_TYPE_BUY else "SELL",
                "entry_time": self._utc(open_deal.time),
                "expected_entry": cache.get("expected_entry_price") if cache else None,
                "actual_entry": cache.get("actual_entry_price") if cache else open_deal.price,
                "entry_spread_points": cache.get("entry_spread") if cache else None,
                "volume": sum(deal.volume for deal in group["open"]),
                "initial_sl": None,
                "initial_tp": None,
                "stop_distance_points": None,
                "take_distance_points": None,
                "target_r": None,
                "risk_usd": risk_usd,
                "equity_at_entry": None,
                "close_reason": close_reason,
            }
            deal_rows = [self._deal_row(deal, "IN") for deal in group["open"]]
            deal_rows.extend(self._deal_row(deal, "OUT") for deal in close_deals)
            trade_id = self.trade_logger.record_recovered_trade(payload, deal_rows)

            # A broker-history rerun yields the same broker-deal primary keys,
            # so it updates this trade instead of creating a second row.
            if cache is not None:
                self.state_manager.clear_execution_cache(position_id)
            self.trade_logger.clear_reconciliation_issue(position_id)
            if cache is not None or not already_recorded or was_open:
                # Keep alerts limited to actual recovery; normal reruns are silent.
                self._alert_recovered_close(trade_id)
            recovered += int(not already_recorded)
        if broker_close_ids:
            # MT5 history endpoints may include a deal stamped exactly at the
            # requested end instant; ledger windows are deliberately [start,
            # end), so widen only this parity read by one microsecond.
            ledger_close_ids = self.trade_logger.ledger_deal_ids(
                start, end + timedelta(microseconds=1), entry_type="OUT",
            )
            missing = broker_close_ids - ledger_close_ids
            if missing:
                complete = False
                self.trade_logger.record_reconciliation_issue(
                    "__parity__", "BROKER_LEDGER_PARITY_MISMATCH", sorted(missing),
                )
            else:
                self.trade_logger.clear_reconciliation_issue("__parity__")

        # A previous pass may have committed the missing OUT deals before an
        # export/worker interruption. The next MT5 window can then contain no
        # close at all, so revalidate the durable marker by its stored IDs
        # instead of leaving Stats WARNING forever.
        self._clear_resolved_reconciliation_issues()
        # Every incomplete branch above persists a durable issue. If the
        # direct deal-ID check resolved all of them in this same pass, recompute
        # completeness instead of retaining the stale local False value and
        # replaying this window forever.
        complete = not self.trade_logger.reconciliation_issues()

        # Advancing only after all ledger upserts and parity checks makes a
        # crash/restart replay an overlapping idempotent slice.  An unresolved
        # close intentionally pins the cursor instead of becoming a silent
        # history gap.
        if complete:
            self.watermark_store.save(end)
            self._health = "SYNCED"
        else:
            self._health = "DEGRADED"
            self._alert_degraded()
        return recovered

    def _alert_recovered_close(self, trade_id):
        """Alert only after the durable ledger contains the recovered close."""
        if not self.alerts:
            return
        trade = self.trade_logger.get_trade(trade_id)
        if trade is None or trade.get("status") != "CLOSED":
            return
        self.alerts.alert_position_closed(
            strategy_name=self.strategy_config.STRATEGY_NAME,
            pnl_usd=trade["profit"],
            r_multiple=trade["pnl_r"] if trade["pnl_r"] is not None else 0.0,
            reason=trade["close_reason"],
        )

    def _clear_resolved_reconciliation_issues(self):
        for issue in self.trade_logger.reconciliation_issues():
            reason = issue.get("reason")
            if reason not in {
                "BROKER_LEDGER_PARITY_MISMATCH",
                "ENTRY_METADATA_UNAVAILABLE",
            }:
                continue
            deal_ids = {
                value for value in str(issue.get("broker_deal_ids") or "").split(";") if value
            }
            if deal_ids and not self.trade_logger.missing_ledger_deal_ids(
                deal_ids, entry_type="OUT",
            ):
                # For an entry-metadata issue an OUT deal can only be attached
                # through a durable trade row, which proves the ledger now has
                # enough identity to own this close. Reversal issues remain
                # manual/fail-closed because deal presence alone cannot prove
                # that both legs were split correctly.
                self.trade_logger.clear_reconciliation_issue(issue["position_id"])

    def health_snapshot(self):
        issues = self.trade_logger.reconciliation_issues()
        return {
            "status": "DEGRADED" if issues or self._health == "DEGRADED" else self._health,
            "issues": issues,
        }

    def _lookup_position_entries(self, position_id, end):
        """Bounded, position-specific fallback for an exit-only window."""
        try:
            start = end - timedelta(days=self.bootstrap_days)
            query_bounds = getattr(self.broker, "history_query_bounds", None)
            query_start, query_end = (
                query_bounds(start, end) if callable(query_bounds) else (start, end)
            )
            candidates = mt5.history_deals_get(query_start, query_end)
        except Exception:
            return []
        if candidates is None:
            return []
        return [
            deal for deal in candidates
            if str(getattr(deal, "position_id", "")) == position_id
            and getattr(deal, "entry", None) == getattr(mt5, "DEAL_ENTRY_IN", None)
            and getattr(deal, "symbol", None) == self.strategy_config.SYMBOL
            and getattr(deal, "magic", None) == self.strategy_config.MAGIC
        ]

    def _close_reason(self, close_deals):
        latest = close_deals[-1]
        return self.broker.decode_close_reason({
            "reason": getattr(latest, "reason", None),
            "comment": getattr(latest, "comment", None),
        })

    def _alert_degraded(self):
        if not self.alerts:
            return
        try:
            issues = self.trade_logger.reconciliation_issues()
            visible = [
                f"{issue.get('reason', 'UNKNOWN')} [{issue.get('position_id', '?')}]"
                for issue in issues[:3]
            ]
            suffix = f"; +{len(issues) - len(visible)} more" if len(issues) > len(visible) else ""
            details = "; ".join(visible) + suffix
            message = (
                f"Trade reconciliation DEGRADED: {details}"
                if details else
                "Trade reconciliation DEGRADED: broker close parity or entry metadata is incomplete"
            )
            if hasattr(self.alerts, "send_throttled_warning"):
                self.alerts.send_throttled_warning(key="trade_reconciliation_degraded", message=message)
            else:
                self.alerts.send_warning(message)
        except Exception:
            pass

    @staticmethod
    def _utc(timestamp):
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    def _deal_row(self, deal, entry_type):
        normalize_epoch = getattr(self.broker, "utc_from_broker_epoch", None)
        occurred_at = (
            normalize_epoch(deal.time)
            if callable(normalize_epoch)
            else self._utc(deal.time)
        )
        return {
            "deal_id": str(deal.ticket),
            "entry_type": entry_type,
            "occurred_at_utc": occurred_at,
            "volume": deal.volume,
            "price": deal.price,
            "commission": getattr(deal, "commission", 0.0),
            "swap": getattr(deal, "swap", 0.0),
            "profit": getattr(deal, "profit", 0.0),
            "reason": self.broker.decode_close_reason({"reason": deal.reason, "comment": deal.comment}) if entry_type == "OUT" else None,
        }
