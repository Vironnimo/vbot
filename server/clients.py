"""In-memory presence registry of connected app-window clients.

Only ``/ws`` window connections register here — browser tabs and the Desktop
shell. The CLI holds no persistent ``/ws`` window (it is request/response RPC)
and channels (Telegram/Discord) are not windows, so neither appears: the roster
answers "which app windows are open", not "every way in". The registry is a
pure in-memory momentary snapshot; nothing is persisted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

JsonObject = dict[str, Any]

ACCESSOR_BROWSER = "browser"
ACCESSOR_DESKTOP = "desktop"
ACCESSOR_UNKNOWN = "unknown"
_ALLOWED_ACCESSORS = frozenset({ACCESSOR_BROWSER, ACCESSOR_DESKTOP})

UNKNOWN_LABEL = "Unknown"

# A connected window always holds an open socket, so its presence status is a
# constant today. The field exists so the row contract already carries it and a
# future "idle/away" notion stays one value, not a new column.
CLIENT_STATUS_CONNECTED = "connected"


@dataclass(frozen=True)
class ClientEntry:
    """One open app-window connection in the presence roster."""

    id: str
    connection_id: str
    accessor: str
    browser: str
    os: str
    connected_at: str

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "accessor": self.accessor,
            "browser": self.browser,
            "os": self.os,
            "connected_at": self.connected_at,
            "status": CLIENT_STATUS_CONNECTED,
        }


class ClientRegistry:
    """In-memory roster of open ``/ws`` app-window connections."""

    def __init__(self) -> None:
        self._entries: dict[str, ClientEntry] = {}

    def register(self, *, connection_id: str, accessor: str, user_agent: str) -> ClientEntry:
        """Add one window connection and return its entry.

        The registry mints its own ``id`` (the unregister key), independent of
        the client-supplied ``connection_id`` (which the client uses only to
        mark its own row). A server-minted key keeps a tab's reconnect — a new
        socket registering before the old one's ``finally`` removes it — from
        deleting the live entry through a shared client id.
        """
        browser, operating_system = _browser_and_os(user_agent)
        entry = ClientEntry(
            id=uuid.uuid4().hex,
            connection_id=connection_id,
            accessor=_normalize_accessor(accessor),
            browser=browser,
            os=operating_system,
            connected_at=datetime.now(UTC).isoformat(),
        )
        self._entries[entry.id] = entry
        return entry

    def unregister(self, registration_id: str) -> None:
        """Remove the connection with this registration id (no-op if absent)."""
        self._entries.pop(registration_id, None)

    def list(self) -> list[ClientEntry]:
        """Return the roster, oldest connection first."""
        return sorted(self._entries.values(), key=lambda entry: entry.connected_at)


def _normalize_accessor(accessor: str) -> str:
    return accessor if accessor in _ALLOWED_ACCESSORS else ACCESSOR_UNKNOWN


def _browser_and_os(user_agent: str) -> tuple[str, str]:
    """Derive a coarse ``(browser, os)`` label pair from a User-Agent string.

    Deliberately a small heuristic, not a full UA parser: presence rows only
    need a human hint ("Chrome on Windows"), and an unknown token is fine.
    """
    return (_browser_from_user_agent(user_agent), _os_from_user_agent(user_agent))


def _browser_from_user_agent(user_agent: str) -> str:
    agent = user_agent or ""
    # Order matters: Edge/Opera UAs also carry "Chrome", Chrome also carries
    # "Safari", so the more specific token has to win first.
    if "Edg" in agent:
        return "Edge"
    if "OPR" in agent or "Opera" in agent:
        return "Opera"
    if "Firefox" in agent:
        return "Firefox"
    if "Chrome" in agent or "Chromium" in agent:
        return "Chrome"
    if "Safari" in agent:
        return "Safari"
    return UNKNOWN_LABEL


def _os_from_user_agent(user_agent: str) -> str:
    agent = user_agent or ""
    # Order matters: Android UAs also contain "Linux"; iOS UAs contain "like Mac".
    if "Windows" in agent:
        return "Windows"
    if "Android" in agent:
        return "Android"
    if "iPhone" in agent or "iPad" in agent or "iOS" in agent:
        return "iOS"
    if "Mac OS" in agent or "Macintosh" in agent:
        return "macOS"
    if "Linux" in agent:
        return "Linux"
    return UNKNOWN_LABEL
