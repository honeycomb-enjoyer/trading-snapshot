"""Regression checks for the global brokerless test harness."""

from __future__ import annotations

import socket

import pytest

from conftest import BlockedMT5, ExternalAccessBlocked


def test_default_mt5_adapter_refuses_terminal_initialization() -> None:
    with pytest.raises(ExternalAccessBlocked, match="MT5 access is forbidden"):
        BlockedMT5().initialize()


def test_network_connections_are_blocked() -> None:
    with pytest.raises(ExternalAccessBlocked, match="Network access is forbidden"):
        socket.create_connection(("example.com", 443))
