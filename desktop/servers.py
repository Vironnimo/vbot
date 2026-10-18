"""Remembered Desktop servers: identity, persisted entries and launch selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.server_address import Scheme, ServerAddress, parse_server_address
from desktop.settings import (
    HTTPS_SCHEME,
    SCHEME_KEY,
    clear_last_used,
    read_last_used,
    read_servers,
    write_last_used,
    write_servers,
)


@dataclass(frozen=True)
class ServerEntry:
    """A remembered Desktop server target.

    Identity is ``(scheme, host, port)`` — the optional ``label`` is display
    only. This mirrors the on-disk ``servers`` entry shape so the settings store
    and the controller speak the same vocabulary.
    """

    host: str
    port: int
    label: str | None = None
    scheme: Scheme = "http"

    @classmethod
    def for_address(cls, address: ServerAddress, label: str | None = None) -> ServerEntry:
        """The entry of a validated address."""

        return cls(address.host, address.port, label or None, address.scheme)

    @property
    def address(self) -> ServerAddress:
        """The server address of the entry."""

        return ServerAddress(self.scheme, self.host, self.port)

    @property
    def key(self) -> tuple[str, str, int]:
        """Return the identity used to match/deduplicate remembered servers."""

        return (self.scheme, self.host, self.port)

    def to_storage(self) -> dict[str, Any]:
        """Render the entry as a settings ``servers`` dict (scheme only for HTTPS)."""

        entry: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.scheme == HTTPS_SCHEME:
            entry[SCHEME_KEY] = HTTPS_SCHEME
        if self.label:
            entry["label"] = self.label
        return entry

    @classmethod
    def from_storage(cls, entry: dict[str, Any]) -> ServerEntry:
        """Build an entry from a normalized settings ``servers`` dict."""

        label = entry.get("label")
        return cls(
            host=entry["host"],
            port=entry["port"],
            label=label if isinstance(label, str) else None,
            scheme="https" if entry.get(SCHEME_KEY) == HTTPS_SCHEME else "http",
        )

    def to_bridge(self) -> dict[str, Any]:
        """Render the entry for bridge callers, which pass ``host`` and ``port`` back.

        ``host`` is the plain host of an HTTP server and the ``https://`` address
        of an HTTPS one.
        """

        payload: dict[str, Any] = {
            "host": self.address.bridge_host,
            "port": self.port,
            "scheme": self.scheme,
        }
        if self.label:
            payload["label"] = self.label
        return payload

    def display_name(self) -> str:
        """Return a human label for the UI — explicit label or the address."""

        display = self.address.display
        return f"{self.label} ({display})" if self.label else display


def list_servers(settings_file: Path | None = None) -> list[ServerEntry]:
    """Return the remembered servers in stored order."""

    return [ServerEntry.from_storage(entry) for entry in read_servers(settings_file)]


def add_server(
    host: str,
    port: Any,
    label: str | None = None,
    *,
    settings_file: Path | None = None,
) -> ServerEntry:
    """Add (or update) a remembered server, keyed by scheme, host and port.

    ``host`` may be an ``https://`` address (its port then wins over ``port``).
    An existing entry with the same identity is replaced in place (so a re-add
    can refresh its label) rather than duplicated; the list order is otherwise
    preserved. The address is validated through the shared Desktop rules before
    anything is persisted, so a malformed entry never reaches disk.
    """

    entry = ServerEntry.for_address(parse_server_address(host, port), label)

    servers = list_servers(settings_file)
    replaced = False
    updated: list[ServerEntry] = []
    for existing in servers:
        if existing.key == entry.key:
            updated.append(entry)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(entry)

    write_servers([item.to_storage() for item in updated], settings_file)
    return entry


def remove_server(
    host: str,
    port: Any,
    *,
    settings_file: Path | None = None,
) -> bool:
    """Remove a remembered server by its address (as :func:`add_server` takes it).

    Returns whether an entry was removed. Removing the last-used target also
    clears the last-used reference so launch does not point at a server the user
    just forgot.
    """

    target_key = _entry_key(host, port)
    servers = list_servers(settings_file)
    remaining = [entry for entry in servers if entry.key != target_key]
    if len(remaining) == len(servers):
        return False

    write_servers([entry.to_storage() for entry in remaining], settings_file)

    last_used = read_last_used(settings_file)
    if last_used is not None and ServerEntry.from_storage(last_used).key == target_key:
        clear_last_used(settings_file)
    return True


def select_server(
    host: str,
    port: Any,
    *,
    settings_file: Path | None = None,
) -> None:
    """Mark a server as last-used so the next launch auto-connects to it."""

    address = parse_server_address(host, port)
    write_last_used(address.host, address.port, settings_file, scheme=address.scheme)


def resolve_last_used(settings_file: Path | None = None) -> ServerEntry | None:
    """Resolve the target to auto-connect on launch.

    The last-used reference wins; when it names a remembered server the stored
    label is carried through. With no last-used reference, the first remembered
    server is used. With nothing remembered at all, ``None`` signals first-run —
    the caller opens the connection screen instead of grabbing a default.
    """

    servers = list_servers(settings_file)
    last_used = read_last_used(settings_file)
    if last_used is not None:
        wanted = ServerEntry.from_storage(last_used)
        for entry in servers:
            if entry.key == wanted.key:
                return entry
        return wanted
    if servers:
        return servers[0]
    return None


def _entry_key(host: Any, port: Any) -> tuple[str, Any, Any]:
    """The identity of an address; a value that does not parse keeps its raw form."""

    try:
        return ServerEntry.for_address(parse_server_address(host, port)).key
    except ValueError:
        return ("http", host, port)
