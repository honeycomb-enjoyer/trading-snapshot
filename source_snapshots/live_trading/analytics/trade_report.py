"""Markdown R-multiple statistics from the atomic ``trades.csv`` export.

This module is intentionally read-only.  It does not query the broker, mutate
the durable ledger, or feed any value back into risk/execution decisions.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_CSV_PATH = Path(__file__).resolve().parents[1] / "runtime" / "analytics" / "trades.csv"
BREAKEVEN_TOLERANCE = 1e-12
REQUIRED_COLUMNS = frozenset({"strategy_id", "status", "exit_time", "pnl_r"})
MISSING_VALUES = frozenset({"", "N/A", "NA", "NONE", "NULL"})


class TradeReportError(ValueError):
    """Raised when a report cannot be built safely from the CSV export."""


@dataclass(frozen=True, slots=True)
class TradeResult:
    strategy_id: str
    exit_time_utc: datetime
    pnl_r: float


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    breakeven: int
    raw_winrate: float | None
    winrate_without_be: float | None
    net_r: float
    max_drawdown_r: float
    profit_factor: float | None
    expectancy: float | None
    average_win: float | None
    average_loss: float | None
    best_trade: float | None
    worst_trade: float | None


def load_trade_results(
    csv_path: Path | str = DEFAULT_CSV_PATH,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[list[TradeResult], int]:
    """Load closed trades whose UTC exit date is inside an inclusive window.

    Returns the usable R-results and the number of otherwise relevant closed
    rows that could not provide an R multiple.
    """
    if start_date is not None and end_date is not None and start_date > end_date:
        raise TradeReportError("start date must not be later than end date")

    path = Path(csv_path)
    try:
        stream = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise TradeReportError(f"cannot read trades CSV {path}: {exc}") from exc

    results: list[TradeResult] = []
    skipped_without_r = 0
    try:
        with stream:
            reader = csv.DictReader(stream)
            missing = REQUIRED_COLUMNS.difference(reader.fieldnames or ())
            if missing:
                raise TradeReportError(
                    "trades CSV is missing required columns: " + ", ".join(sorted(missing))
                )
            for line_number, row in enumerate(reader, start=2):
                if (row.get("status") or "").strip().upper() != "CLOSED":
                    continue
                exit_time = _parse_exit_time(row.get("exit_time"), line_number)
                exit_date = exit_time.date()
                if start_date is not None and exit_date < start_date:
                    continue
                if end_date is not None and exit_date > end_date:
                    continue

                strategy_id = (row.get("strategy_id") or "").strip().casefold()
                if not strategy_id:
                    raise TradeReportError(f"line {line_number}: strategy_id is empty")
                pnl_r = _read_pnl_r(row, line_number)
                if pnl_r is None:
                    skipped_without_r += 1
                    continue
                results.append(TradeResult(strategy_id, exit_time, pnl_r))
    except csv.Error as exc:
        raise TradeReportError(f"cannot parse trades CSV {path}: {exc}") from exc

    results.sort(key=lambda trade: trade.exit_time_utc)
    return results, skipped_without_r


def calculate_metrics(trades: Iterable[TradeResult]) -> Metrics:
    ordered = sorted(trades, key=lambda trade: trade.exit_time_utc)
    values = [trade.pnl_r for trade in ordered]
    wins = [value for value in values if value > BREAKEVEN_TOLERANCE]
    losses = [value for value in values if value < -BREAKEVEN_TOLERANCE]
    breakeven = len(values) - len(wins) - len(losses)
    decisive = len(wins) + len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return Metrics(
        trades=len(values),
        wins=len(wins),
        losses=len(losses),
        breakeven=breakeven,
        raw_winrate=_ratio(len(wins), len(values)),
        winrate_without_be=_ratio(len(wins), decisive),
        net_r=sum(values),
        max_drawdown_r=_maximum_drawdown(values),
        profit_factor=(gross_profit / gross_loss if gross_loss else None),
        expectancy=(sum(values) / len(values) if values else None),
        average_win=(gross_profit / len(wins) if wins else None),
        average_loss=(sum(losses) / len(losses) if losses else None),
        best_trade=(max(values) if values else None),
        worst_trade=(min(values) if values else None),
    )


def build_report(trades: Sequence[TradeResult]) -> str:
    """Render total and per-strategy statistics as a compact Markdown report."""
    groups: list[tuple[str, list[TradeResult]]] = [("ALL", list(trades))]
    strategy_ids = sorted({trade.strategy_id for trade in trades})
    groups.extend(
        (strategy_id, [trade for trade in trades if trade.strategy_id == strategy_id])
        for strategy_id in strategy_ids
    )
    summaries = [(name, calculate_metrics(group)) for name, group in groups]

    overview_rows = [
        [
            name,
            str(metrics.trades),
            str(metrics.wins),
            str(metrics.losses),
            str(metrics.breakeven),
            _format_percent(metrics.raw_winrate),
            _format_percent(metrics.winrate_without_be),
        ]
        for name, metrics in summaries
    ]
    r_rows = [
        [
            name,
            _format_number(metrics.net_r),
            _format_number(metrics.max_drawdown_r),
            _format_number(metrics.profit_factor),
            _format_number(metrics.expectancy, decimals=3),
            _format_number(metrics.average_win),
            _format_number(metrics.average_loss),
            _format_number(metrics.best_trade),
            _format_number(metrics.worst_trade),
        ]
        for name, metrics in summaries
    ]
    if trades:
        first_exit = min(trade.exit_time_utc for trade in trades).isoformat()
        last_exit = max(trade.exit_time_utc for trade in trades).isoformat()
    else:
        first_exit = last_exit = "N/A"

    lines = [
        "# Live Trade Report",
        "",
        "## Scope",
        "",
        f"- Closed trades: `{len(trades)}`",
        f"- First exit UTC: `{first_exit}`",
        f"- Last exit UTC: `{last_exit}`",
        "",
        "## Trade Outcomes",
        "",
        _render_markdown_table(
            ["Scope", "Trades", "Wins", "Losses", "BE", "Winrate", "Winrate ex BE"],
            overview_rows,
        ),
        "",
        "## R-Multiple Metrics",
        "",
        _render_markdown_table(
            ["Scope", "Net R", "Max DD", "Profit Factor", "Expectancy", "Avg Win", "Avg Loss", "Best", "Worst"],
            r_rows,
        ),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show total and per-strategy R statistics from trades.csv."
    )
    parser.add_argument(
        "dates",
        metavar="YYYY-MM-DD",
        nargs="*",
        help="one date for a UTC day, or start and end dates for an inclusive UTC window",
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH, help="path to trades.csv")
    parser.add_argument("--output", type=Path, help="optional path for a Markdown report")
    args = parser.parse_args(argv)

    if len(args.dates) > 2:
        parser.error("provide no more than two dates")
    try:
        dates = [_parse_date(value) for value in args.dates]
        start_date = dates[0] if dates else None
        end_date = dates[-1] if dates else None
        trades, skipped = load_trade_results(
            args.csv, start_date=start_date, end_date=end_date
        )
        report = build_report(trades)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n", encoding="utf-8")
        print(report)
        if skipped:
            print(
                f"\nWarning: excluded {skipped} closed trade(s) without pnl_r "
                "or usable pnl_usd/risk_usd.",
                file=sys.stderr,
            )
        return 0
    except TradeReportError as exc:
        print(f"Trade report error: {exc}", file=sys.stderr)
        return 2


def _read_pnl_r(row: dict[str, str], line_number: int) -> float | None:
    raw_pnl_r = (row.get("pnl_r") or "").strip()
    if raw_pnl_r.upper() not in MISSING_VALUES:
        return _finite_float(raw_pnl_r, "pnl_r", line_number)

    raw_pnl_usd = (row.get("pnl_usd") or "").strip()
    raw_risk_usd = (row.get("risk_usd") or "").strip()
    if raw_pnl_usd.upper() in MISSING_VALUES or raw_risk_usd.upper() in MISSING_VALUES:
        return None
    pnl_usd = _finite_float(raw_pnl_usd, "pnl_usd", line_number)
    risk_usd = _finite_float(raw_risk_usd, "risk_usd", line_number)
    return pnl_usd / risk_usd if risk_usd > 0 else None


def _parse_exit_time(value: str | None, line_number: int) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise TradeReportError(f"line {line_number}: closed trade has no exit_time")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradeReportError(f"line {line_number}: invalid exit_time {raw!r}") from exc
    if parsed.tzinfo is None:
        raise TradeReportError(f"line {line_number}: exit_time has no timezone")
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TradeReportError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def _finite_float(value: str, field: str, line_number: int) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise TradeReportError(f"line {line_number}: invalid {field} {value!r}") from exc
    if not math.isfinite(parsed):
        raise TradeReportError(f"line {line_number}: {field} must be finite")
    return parsed


def _maximum_drawdown(values: Iterable[float]) -> float:
    equity = peak = maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _format_number(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def _render_markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join((header, separator, *body))


if __name__ == "__main__":
    raise SystemExit(main())
