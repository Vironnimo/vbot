"""Tests for the client presence registry."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from server.clients import (
    ACCESSOR_BROWSER,
    ACCESSOR_DESKTOP,
    ACCESSOR_UNKNOWN,
    CLIENT_STATUS_CONNECTED,
    UNKNOWN_LABEL,
    ClientRegistry,
)

_CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
_SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15"
)
_EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


def test_register_lists_each_connection_with_the_row_contract_oldest_first() -> None:
    at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    clock = iter((at + timedelta(hours=1), at, at + timedelta(minutes=30)))
    registry = ClientRegistry(now_provider=lambda: next(clock))

    desktop = registry.register(connection_id="tab-b", accessor="desktop", user_agent=_SAFARI_MAC)
    unknown = registry.register(connection_id="tab-a", accessor="cli", user_agent="")
    # A reconnect overlap from the same client id is a second, distinct entry.
    overlap = registry.register(connection_id="tab-a", accessor="browser", user_agent="")

    assert registry.list() == [unknown, overlap, desktop]
    assert unknown.id != overlap.id
    assert (unknown.accessor, overlap.accessor) == (ACCESSOR_UNKNOWN, ACCESSOR_BROWSER)
    assert desktop.to_dict() == {
        "id": desktop.id,
        "connection_id": "tab-b",
        "accessor": ACCESSOR_DESKTOP,
        "browser": "Safari",
        "os": "macOS",
        "connected_at": "2026-06-20T11:00:00+00:00",
        "status": CLIENT_STATUS_CONNECTED,
    }


def test_unregister_removes_only_the_named_entry() -> None:
    registry = ClientRegistry()
    first = registry.register(connection_id="tab-a", accessor="browser", user_agent="")
    second = registry.register(connection_id="tab-b", accessor="desktop", user_agent="")

    registry.unregister("does-not-exist")
    assert len(registry.list()) == 2

    registry.unregister(first.id)
    assert registry.list() == [second]


def test_presence_logs_one_connect_and_disconnect_per_logical_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    at = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    clock = iter(
        (
            at,
            at + timedelta(minutes=18, seconds=4),
            at,
            at + timedelta(seconds=5),
            at + timedelta(minutes=2),
        )
    )
    registry = ClientRegistry(now_provider=lambda: next(clock))

    with caplog.at_level(logging.INFO, logger="vbot.server.clients"):
        window = registry.register(
            connection_id="12345678-abcd", accessor="browser", user_agent=_CHROME_WINDOWS
        )
        registry.unregister(window.id)
        # Overlapping reconnect sockets of one window form one presence cycle.
        original = registry.register(
            connection_id="same-tab-id", accessor="desktop", user_agent=_EDGE_WINDOWS
        )
        replacement = registry.register(
            connection_id="same-tab-id", accessor="desktop", user_agent=_EDGE_WINDOWS
        )
        registry.unregister(original.id)
        registry.unregister(replacement.id)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 4
    assert all("client_id=12345678" in message for message in messages[:2])
    assert "connected_for=18m 4s" in messages[1]
    assert all("client_id=same-tab" in message for message in messages[2:])
    assert "connected_for=2m)" in messages[3]


@pytest.mark.parametrize(
    ("user_agent", "expected_browser", "expected_os"),
    [
        (_CHROME_WINDOWS, "Chrome", "Windows"),
        (_FIREFOX_LINUX, "Firefox", "Linux"),
        (_SAFARI_MAC, "Safari", "macOS"),
        (_EDGE_WINDOWS, "Edge", "Windows"),
        ("", UNKNOWN_LABEL, UNKNOWN_LABEL),
        ("curl/8.4.0", UNKNOWN_LABEL, UNKNOWN_LABEL),
    ],
)
def test_browser_and_os_derivation(
    user_agent: str, expected_browser: str, expected_os: str
) -> None:
    registry = ClientRegistry()

    entry = registry.register(connection_id="tab-a", accessor="browser", user_agent=user_agent)

    assert entry.browser == expected_browser
    assert entry.os == expected_os
