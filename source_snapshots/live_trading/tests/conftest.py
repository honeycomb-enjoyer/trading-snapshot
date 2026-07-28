"""Global isolation guard for the brokerless unit-test suite."""

from __future__ import annotations

import socket
import sys
import types

import pytest


class ExternalAccessBlocked(RuntimeError):
    """Raised when a unit test attempts to use a real external service."""


class BlockedMT5(types.ModuleType):
    """Import-safe MT5 adapter that fails before any terminal interaction."""

    def __init__(self) -> None:
        super().__init__("MetaTrader5")
        for index, name in enumerate(
            ("M1", "M2", "M3", "M4", "M5", "M10", "M15", "M30", "H1", "H2", "H4", "D1"),
            start=1,
        ):
            setattr(self, f"TIMEFRAME_{name}", index)

    @staticmethod
    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise ExternalAccessBlocked("MT5 access is forbidden in brokerless tests")

    initialize = _blocked
    login = _blocked
    shutdown = _blocked
    account_info = _blocked
    terminal_info = _blocked
    positions_get = _blocked
    order_send = _blocked
    order_check = _blocked
    copy_rates_range = _blocked
    history_orders_get = _blocked
    history_deals_get = _blocked


sys.modules.setdefault("MetaTrader5", BlockedMT5())


def _block_network(*_args: object, **_kwargs: object) -> None:
    raise ExternalAccessBlocked("Network access is forbidden in brokerless tests")


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent unit tests from opening network connections by accident."""
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(socket.socket, "connect", _block_network)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The current suite is fully isolated unless a future test opts out."""
    for item in items:
        if "integration" not in item.keywords:
            item.add_marker(pytest.mark.unit)
        if "slow" not in item.keywords:
            item.add_marker(pytest.mark.brokerless)
