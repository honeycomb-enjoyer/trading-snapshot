"""Global isolation guard for research unit tests."""

from __future__ import annotations

import socket
import sys
import types

import pytest


class ExternalAccessBlocked(RuntimeError):
    """Raised when a unit test attempts to use a real external service."""


class BlockedMT5(types.ModuleType):
    """Import-safe MT5 adapter that refuses terminal access."""

    def __init__(self) -> None:
        super().__init__("MetaTrader5")

    @staticmethod
    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise ExternalAccessBlocked("MT5 access is forbidden in brokerless tests")

    initialize = _blocked
    shutdown = _blocked
    copy_rates_range = _blocked


sys.modules.setdefault("MetaTrader5", BlockedMT5())


def _block_network(*_args: object, **_kwargs: object) -> None:
    raise ExternalAccessBlocked("Network access is forbidden in brokerless tests")


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(socket.socket, "connect", _block_network)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" not in item.keywords:
            item.add_marker(pytest.mark.unit)
        if "slow" not in item.keywords:
            item.add_marker(pytest.mark.brokerless)
