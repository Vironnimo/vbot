"""Tests for the client presence registry."""

from __future__ import annotations

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


def test_register_adds_entry_to_list() -> None:
    registry = ClientRegistry()

    entry = registry.register(connection_id="tab-a", accessor="browser", user_agent=_CHROME_WINDOWS)

    roster = registry.list()
    assert len(roster) == 1
    assert roster[0] is entry
    assert entry.connection_id == "tab-a"
    assert entry.accessor == ACCESSOR_BROWSER
    assert entry.browser == "Chrome"
    assert entry.os == "Windows"


def test_register_mints_unique_registration_id_per_call() -> None:
    registry = ClientRegistry()

    first = registry.register(connection_id="tab-a", accessor="browser", user_agent="")
    second = registry.register(connection_id="tab-a", accessor="browser", user_agent="")

    # Two connections from the same client id (e.g. a reconnect overlap) are two
    # distinct registry entries keyed by the server-minted id.
    assert first.id != second.id
    assert len(registry.list()) == 2


def test_unregister_removes_only_the_named_entry() -> None:
    registry = ClientRegistry()
    first = registry.register(connection_id="tab-a", accessor="browser", user_agent="")
    second = registry.register(connection_id="tab-b", accessor="desktop", user_agent="")

    registry.unregister(first.id)

    roster = registry.list()
    assert [entry.id for entry in roster] == [second.id]


def test_unregister_unknown_id_is_a_noop() -> None:
    registry = ClientRegistry()
    registry.register(connection_id="tab-a", accessor="browser", user_agent="")

    registry.unregister("does-not-exist")

    assert len(registry.list()) == 1


def test_list_orders_by_connection_time() -> None:
    registry = ClientRegistry()
    older = registry.register(connection_id="tab-a", accessor="browser", user_agent="")
    # Force a later, deterministic timestamp on the second entry.
    newer = registry.register(connection_id="tab-b", accessor="browser", user_agent="")
    object.__setattr__(older, "connected_at", "2026-06-20T10:00:00+00:00")
    object.__setattr__(newer, "connected_at", "2026-06-20T11:00:00+00:00")

    roster = registry.list()

    assert [entry.connection_id for entry in roster] == ["tab-a", "tab-b"]


def test_to_dict_exposes_the_row_contract() -> None:
    registry = ClientRegistry()
    entry = registry.register(connection_id="tab-a", accessor="desktop", user_agent=_SAFARI_MAC)

    payload = entry.to_dict()

    assert payload == {
        "id": entry.id,
        "connection_id": "tab-a",
        "accessor": ACCESSOR_DESKTOP,
        "browser": "Safari",
        "os": "macOS",
        "connected_at": entry.connected_at,
        "status": CLIENT_STATUS_CONNECTED,
    }


def test_unknown_accessor_normalizes_to_unknown() -> None:
    registry = ClientRegistry()

    entry = registry.register(connection_id="tab-a", accessor="cli", user_agent="")

    assert entry.accessor == ACCESSOR_UNKNOWN


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
