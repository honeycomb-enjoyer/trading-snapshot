"""Brokerless verification for P0-T02 account-scoped flattening.

Run with: ``python tests/test_account_flatten.py``
"""

import os
import sys
import types
from dataclasses import dataclass


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeMT5(types.ModuleType):
    TRADE_RETCODE_DONE = 10009
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_FILLING_RETURN = 3
    ORDER_TIME_GTC = 0

    def __init__(self):
        super().__init__("MetaTrader5")
        self.positions = []
        self.sent_requests = []
        self.responses_by_ticket = {}
        self.fail_position_queries = False

    def positions_get(self, ticket=None, **_kwargs):
        if self.fail_position_queries:
            return None
        if ticket is None:
            return tuple(self.positions)
        return tuple(p for p in self.positions if p.ticket == ticket)

    @staticmethod
    def last_error():
        return (500, "scripted positions_get failure")

    @staticmethod
    def symbol_info_tick(symbol):
        prices = {
            "XAUUSD": (2300.10, 2300.30),
            "EURUSD": (1.08001, 1.08003),
            "GER40": (18000.0, 18001.0),
        }
        bid, ask = prices[symbol]
        return types.SimpleNamespace(bid=bid, ask=ask)

    @staticmethod
    def symbol_info(symbol):
        digits = {"XAUUSD": 2, "EURUSD": 5, "GER40": 1}[symbol]
        return types.SimpleNamespace(digits=digits)

    def order_check(self, _request):
        return types.SimpleNamespace(retcode=self.TRADE_RETCODE_DONE)

    def order_send(self, request):
        self.sent_requests.append(dict(request))
        ticket = request["position"]
        responses = self.responses_by_ticket.setdefault(
            ticket, [self.TRADE_RETCODE_DONE]
        )
        retcode = responses.pop(0)
        if retcode == self.TRADE_RETCODE_DONE:
            self.positions = [p for p in self.positions if p.ticket != ticket]
        return types.SimpleNamespace(
            retcode=retcode,
            comment=f"retcode={retcode}",
            request_id=ticket,
        )


fake_mt5 = FakeMT5()
sys.modules["MetaTrader5"] = fake_mt5

from core.account_position_service import AccountPositionService
from core.broker import Broker, AlreadyClosedPosition
from guards.kill_switch import KillSwitch
import core.broker as broker_module


broker_module.ORDER_RETRY_DELAY_SEC = 0


@dataclass
class Position:
    ticket: int
    symbol: str
    type: int
    volume: float
    magic: int


class Alerts:
    def __init__(self):
        self.info = []
        self.critical = []

    def send_info(self, message):
        self.info.append(message)

    def send_critical(self, message):
        self.critical.append(message)

    def alert_system_issue(self, **_kwargs):
        pass


def make_service(positions):
    fake_mt5.positions = list(positions)
    fake_mt5.sent_requests = []
    fake_mt5.responses_by_ticket = {}
    fake_mt5.fail_position_queries = False
    broker = Broker(symbol="EURUSD")  # Must not leak into account closes.
    return broker, AccountPositionService(broker)


def test_multi_symbol_and_manual_flatten():
    positions = [
        Position(101, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 0.2, 43001),
        Position(102, "EURUSD", fake_mt5.POSITION_TYPE_SELL, 0.1, 0),
        Position(103, "GER40", fake_mt5.POSITION_TYPE_BUY, 1.0, 43001),
    ]
    _, service = make_service(positions)

    result = service.flatten_account("TEST_MULTI_SYMBOL")

    assert result.is_flat, result
    assert result.closed_tickets == [101, 102, 103]
    assert not fake_mt5.positions
    assert [r["symbol"] for r in fake_mt5.sent_requests] == [
        "XAUUSD", "EURUSD", "GER40"
    ]
    assert [r["position"] for r in fake_mt5.sent_requests] == [101, 102, 103]


def test_already_closed_ticket_is_idempotent():
    class AlreadyClosedBroker:
        trade_retcode_done = fake_mt5.TRADE_RETCODE_DONE

        @staticmethod
        def list_all_positions():
            return [Position(201, "XAUUSD", 0, 0.1, 0)]

        @staticmethod
        def close_position_by_ticket(ticket):
            assert ticket == 201
            return AlreadyClosedPosition(ticket)

    service = AccountPositionService(AlreadyClosedBroker())
    # After the close race, the verification query sees no position.
    calls = 0

    def list_positions():
        nonlocal calls
        calls += 1
        return [Position(201, "XAUUSD", 0, 0.1, 0)] if calls == 1 else []

    service.broker.list_all_positions = list_positions
    result = service.flatten_account("TEST_ALREADY_CLOSED")
    assert result.is_flat, result
    assert result.already_closed_tickets == [201]


def test_transient_and_permanent_retcode_are_explicit():
    positions = [
        Position(301, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 0.1, 1),
        Position(302, "GER40", fake_mt5.POSITION_TYPE_SELL, 1.0, 2),
    ]
    _, service = make_service(positions)
    fake_mt5.responses_by_ticket = {
        301: [10021, fake_mt5.TRADE_RETCODE_DONE],  # PRICE_CHANGED
        302: [10006],  # REJECT: must not be blindly retried
    }

    result = service.flatten_account("TEST_RETCODES")

    assert result.closed_tickets == [301]
    assert result.remaining_tickets == [302]
    assert 302 in result.failed_tickets
    assert "10006" in result.failed_tickets[302]
    assert [r["position"] for r in fake_mt5.sent_requests] == [301, 301, 302]


def test_query_failure_is_never_reported_as_flat():
    _, service = make_service([])
    fake_mt5.fail_position_queries = True

    result = service.flatten_account("TEST_QUERY_FAILURE")

    assert not result.is_flat
    assert result.verification_error


def test_kill_switch_stays_halted_on_partial_flatten_and_alerts_critical():
    positions = [Position(401, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 0.1, 0)]
    broker, _ = make_service(positions)
    fake_mt5.responses_by_ticket = {401: [10006]}
    alerts = Alerts()
    kill_switch = KillSwitch(alerts=alerts, strategy_name="test")
    kill_switch.trigger("ACCOUNT_TEST")

    result = kill_switch.flatten_all(broker=broker, reason="ACCOUNT_TEST")

    assert not result.is_flat
    assert kill_switch.triggered
    assert alerts.critical
    assert "remaining=[401]" in alerts.critical[-1]


if __name__ == "__main__":
    test_multi_symbol_and_manual_flatten()
    test_already_closed_ticket_is_idempotent()
    test_transient_and_permanent_retcode_are_explicit()
    test_query_failure_is_never_reported_as_flat()
    test_kill_switch_stays_halted_on_partial_flatten_and_alerts_critical()
    print("PASS - P0-T02 account flatten tests")
