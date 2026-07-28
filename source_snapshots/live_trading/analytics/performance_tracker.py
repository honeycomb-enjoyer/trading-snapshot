"""Deprecated compatibility facade backed by the account-scoped ledger.

New code should query :class:`analytics.trade_store.TradeStore` directly.
This class intentionally no longer reads CSV exports, which are not
authoritative and have no reliable trade-status field.
"""

from __future__ import annotations

import warnings

from analytics.trade_store import TradeStore


class PerformanceTracker:
    def __init__(self, account_id: str = "hub_demo", store: TradeStore | None = None, trades_file=None):
        warnings.warn(
            "PerformanceTracker is deprecated; use TradeStore account-scoped queries.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.account_id = account_id
        self.store = store or TradeStore()
        self.trades_file = trades_file  # accepted only for source compatibility

    def load_trades(self):
        return self.store.list_trades(self.account_id)

    def closed_trades(self):
        return self.store.list_trades(self.account_id, closed_only=True)

    @staticmethod
    def _to_float(value):
        try:
            return 0.0 if value in (None, "", "None") else float(value)
        except (TypeError, ValueError):
            return 0.0

    def get_summary(self):
        trades = self.closed_trades()
        pnls = [self._to_float(trade.get("profit")) for trade in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        total = len(pnls)
        gross_profit, gross_loss = sum(wins), abs(sum(losses))
        return {
            "total_trades": total, "wins": len(wins), "losses": len(losses),
            "breakeven": total - len(wins) - len(losses),
            "winrate": round(len(wins) / total * 100, 2) if total else 0.0,
            "gross_profit": round(gross_profit, 2), "gross_loss": round(gross_loss, 2),
            "net_profit": round(sum(pnls), 2),
            "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
            "avg_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else 0.0,
            "expectancy": round(sum(pnls) / total, 2) if total else 0.0,
            "max_drawdown": round(self.calculate_max_drawdown(pnls), 2),
        }

    @staticmethod
    def calculate_max_drawdown(pnls):
        equity = peak = maximum = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            maximum = max(maximum, peak - equity)
        return maximum

    def strategy_breakdown(self):
        grouped = {}
        for trade in self.closed_trades():
            grouped.setdefault(trade["strategy_id"], []).append(self._to_float(trade["profit"]))
        return {
            strategy: {
                "trades": len(pnls),
                "winrate": round(sum(pnl > 0 for pnl in pnls) / len(pnls) * 100, 2),
                "profit_factor": round(sum(pnl for pnl in pnls if pnl > 0) / abs(sum(pnl for pnl in pnls if pnl < 0)), 2) if any(pnl < 0 for pnl in pnls) else 0.0,
            }
            for strategy, pnls in grouped.items()
        }
