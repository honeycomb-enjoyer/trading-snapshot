import csv
from datetime import date, datetime, timezone

import pytest

from analytics.trade_report import (
    TradeReportError,
    TradeResult,
    build_report,
    calculate_metrics,
    load_trade_results,
    main,
)


UTC = timezone.utc
HEADERS = [
    "strategy_id", "status", "exit_time", "pnl_r", "pnl_usd", "risk_usd"
]


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _trade(strategy_id, day, pnl_r):
    return TradeResult(strategy_id, datetime(2026, 7, day, 12, tzinfo=UTC), pnl_r)


def test_calculates_requested_r_metrics_in_exit_order():
    metrics = calculate_metrics(
        [
            _trade("alpha", 4, 2.0),
            _trade("alpha", 1, -1.0),
            _trade("alpha", 2, 1.0),
            _trade("alpha", 3, 0.0),
            _trade("alpha", 5, -0.5),
        ]
    )

    assert (metrics.trades, metrics.wins, metrics.losses, metrics.breakeven) == (5, 2, 2, 1)
    assert metrics.raw_winrate == pytest.approx(0.4)
    assert metrics.winrate_without_be == pytest.approx(0.5)
    assert metrics.net_r == pytest.approx(1.5)
    assert metrics.max_drawdown_r == pytest.approx(1.0)
    assert metrics.profit_factor == pytest.approx(2.0)
    assert metrics.expectancy == pytest.approx(0.3)
    assert metrics.average_win == pytest.approx(1.5)
    assert metrics.average_loss == pytest.approx(-0.75)
    assert metrics.best_trade == pytest.approx(2.0)
    assert metrics.worst_trade == pytest.approx(-1.0)


def test_loads_closed_window_and_merges_strategy_id_case(tmp_path):
    path = tmp_path / "trades.csv"
    _write_csv(
        path,
        [
            {"strategy_id": "ALPHA", "status": "CLOSED", "exit_time": "2026-07-01T23:00:00+00:00", "pnl_r": "1"},
            {"strategy_id": "alpha", "status": "CLOSED", "exit_time": "2026-07-02T23:59:59Z", "pnl_r": "-1"},
            {"strategy_id": "alpha", "status": "OPEN", "exit_time": "", "pnl_r": ""},
            {"strategy_id": "beta", "status": "CLOSED", "exit_time": "2026-07-03T00:00:00+00:00", "pnl_r": "2"},
        ],
    )

    trades, skipped = load_trade_results(
        path, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2)
    )

    assert skipped == 0
    assert [(trade.strategy_id, trade.pnl_r) for trade in trades] == [
        ("alpha", 1.0),
        ("alpha", -1.0),
    ]
    report = build_report(trades)
    assert report.count("| alpha |") == 2
    assert "ALPHA" not in report


def test_falls_back_to_profit_over_risk_and_counts_unusable_rows(tmp_path):
    path = tmp_path / "trades.csv"
    _write_csv(
        path,
        [
            {"strategy_id": "alpha", "status": "CLOSED", "exit_time": "2026-07-01T10:00:00+00:00", "pnl_r": "N/A", "pnl_usd": "50", "risk_usd": "100"},
            {"strategy_id": "beta", "status": "CLOSED", "exit_time": "2026-07-01T11:00:00+00:00", "pnl_r": "", "pnl_usd": "20", "risk_usd": ""},
        ],
    )

    trades, skipped = load_trade_results(path)

    assert [trade.pnl_r for trade in trades] == [0.5]
    assert skipped == 1


def test_cli_accepts_one_date_as_exact_utc_day(tmp_path, capsys):
    path = tmp_path / "trades.csv"
    _write_csv(
        path,
        [
            {"strategy_id": "alpha", "status": "CLOSED", "exit_time": "2026-07-01T23:00:00+00:00", "pnl_r": "1"},
            {"strategy_id": "beta", "status": "CLOSED", "exit_time": "2026-07-02T00:00:00+00:00", "pnl_r": "2"},
        ],
    )

    assert main(["2026-07-01", "--csv", str(path)]) == 0
    output = capsys.readouterr().out
    assert "alpha" in output
    assert "beta" not in output


def test_rejects_reverse_window_and_naive_exit_time(tmp_path):
    with pytest.raises(TradeReportError, match="start date"):
        load_trade_results(
            tmp_path / "missing.csv",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 1),
        )

    path = tmp_path / "trades.csv"
    _write_csv(
        path,
        [{"strategy_id": "alpha", "status": "CLOSED", "exit_time": "2026-07-01T12:00:00", "pnl_r": "1"}],
    )
    with pytest.raises(TradeReportError, match="no timezone"):
        load_trade_results(path)
