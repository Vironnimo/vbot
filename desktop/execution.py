"""Computer capabilities supplied by the running Desktop to its selected server.

The native connection belongs to the window's lifetime, independently of the
WebUI document. A server scope owns its capability handlers across network
reconnects; changing server or ending the Desktop closes those handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.utils.ids import new_id
from desktop._http import shared_ssl_context
from desktop.credentials import DeviceTokenStore, StoredComputerIdentity
from desktop.extensions import (
    BeforeReload,
    DesktopExtensionCatalog,
    ExecutionHandler,
    HandlerFactory,
    NativeCapability,
)
from desktop.server_address import parse_server_address
from desktop.settings import read_section, update_section

logger = logging.getLogger("vbot.desktop.execution")
SETTINGS_KEY = "computer_sharing"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_PENDING_CALLS = 64
MAX_REMEMBERED_CALLS = 4096
RETIREMENT_TIMEOUT = 10.0
_wait_for_retirement = asyncio.wait
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


class SharingError(Exception):
    """A Desktop sharing setting could not be applied."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code


class Authorization(Protocol):
    def header(self, server_url: str) -> str | None: ...

    def rejected(self, server_url: str, header: str | None) -> bool: ...


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def connect_socket(url: str, header: str | None) -> AsyncIterator[Socket]:
    """Open one native socket with the same origin/TLS policy as Desktop HTTP."""
    from websockets.asyncio.client import connect

    target = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    options: dict[str, Any] = {
        "additional_headers": {"Authorization": header} if header else {},
        "max_size": MAX_MESSAGE_BYTES,
        "proxy": None,
        "open_timeout": 10,
        "close_timeout": 2,
        "ping_interval": 15,
        "ping_timeout": 15,
    }
    if target.startswith("wss://"):
        options["ssl"] = shared_ssl_context()
    async with connect(f"{target.rstrip('/')}/ws/computers", **options) as socket:
        yield socket


@dataclass
class _Scope:
    origin: str
    identity: StoredComputerIdentity
    instance_id: str = field(default_factory=lambda: new_id("pci"))
    server_instance: str | None = None
    capability_updates: bool = False
    catalog_revision: int = 0
    handlers: dict[str, ExecutionHandler] = field(default_factory=dict)
    ready: set[str] = field(default_factory=set)
    declarations: dict[str, NativeCapability] = field(default_factory=dict)
    retiring: dict[str, tuple[ExecutionHandler, NativeCapability]] = field(default_factory=dict)
    retirement_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    calls: set[asyncio.Task[None]] = field(default_factory=set)
    capability_calls: dict[asyncio.Task[None], str] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    seen_order: deque[str] = field(default_factory=deque)
    connection: int = 0


class ComputerSharing:
    """Thread-safe Desktop settings/lifecycle facade around one native async loop."""

    def __init__(
        self,
        *,
        settings_path: Path,
        identities: DeviceTokenStore,
        authorization: Authorization,
        factories: Mapping[str, HandlerFactory],
        capabilities: Mapping[str, NativeCapability] | None = None,
        extension_records: list[dict[str, Any]] | None = None,
        extension_catalog: DesktopExtensionCatalog | None = None,
        publish: Callable[[Mapping[str, Any]], None] = lambda _status: None,
        connector: Callable[..., Any] = connect_socket,
        available: Mapping[str, bool] | None = None,
        reload_extensions: Callable[
            [DesktopExtensionCatalog, BeforeReload], DesktopExtensionCatalog
        ]
        | None = None,
    ) -> None:
        self._settings_path = settings_path
        self._settings = read_section(SETTINGS_KEY, settings_path)
        self._identities = identities
        self._authorization = authorization
        if extension_catalog is not None:
            if capabilities is not None or extension_records is not None:
                raise ValueError("An Extension catalog is the complete native declaration snapshot")
            capabilities = extension_catalog.capabilities
            extension_records = extension_catalog.records
        self._capabilities = (
            dict(capabilities)
            if capabilities is not None
            else {
                name: NativeCapability(name, "builtin", name, "", factory)
                for name, factory in factories.items()
            }
        )
        if set(self._capabilities) != set(factories):
            raise ValueError("Native capability declarations must match their factories")
        self._extension_records = list(extension_records or ())
        self._extension_catalog = extension_catalog or DesktopExtensionCatalog(
            dict(self._capabilities), list(self._extension_records)
        )
        self._reload_extensions = reload_extensions
        self._catalog_revision = 0
        self._reloading = False
        self._publish = publish
        self._connector = connector
        self._available = {
            name: declaration.available for name, declaration in self._capabilities.items()
        }
        if available is not None:
            self._available.update(available)
        self._lock = threading.RLock()
        self._update_lock = threading.Lock()
        self._origin = ""
        self._server_url = ""
        self._closed = False
        self._quiescing = False
        self._switching_server = False
        self._disabling_terminal = False
        self._starting_terminals = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed: asyncio.Event | None = None
        self._scope: _Scope | None = None
        self._socket: Socket | None = None
        self._connected = False
        self._serving_header: str | None = None
        self._welcomes = 0
        self._error_code: str | None = None
        self._last_status: dict[str, Any] | None = None
        self._capability_errors: dict[str, str] = {}

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._thread_main, name="vbot-desktop-computer", daemon=True
            )
            self._thread.start()

    def set_server_url(self, server_url: str) -> None:
        address = parse_server_address(server_url.rstrip("/"), None) if server_url else None
        with self._lock:
            origin = address.origin if address else ""
            if origin == self._origin:
                # A completed pairing can replace this origin's credential; every
                # other reconnect of the window keeps the healthy native link.
                if (
                    not self._connected
                    or self._authorization.header(self._server_url) != self._serving_header
                ):
                    self._wake()
                return
            self._origin = origin
            self._server_url = address.url if address else ""
            self._connected = False
            self._error_code = None
            self._wake()
        self._report()

    def status(self) -> dict[str, Any]:
        with self._lock:
            settings = self._settings
            identity = self._identities.computer_identity(self._origin) if self._origin else None
            return {
                "computer_id": identity.computer_id if identity else None,
                "name": _computer_name(settings.get("name")),
                "server_url": self._server_url,
                "connected": self._connected,
                "capabilities": _grants(settings, self._origin, self._capabilities),
                "capability_catalog": [item.describe() for item in self._capabilities.values()],
                "extensions": [dict(record) for record in self._extension_records]
                + [
                    {"name": name, "status": "failed", "error": reason, "capability_errors": []}
                    for name, reason in self._capability_errors.items()
                    if name not in self._capabilities
                ],
                "reload_supported": self._reload_extensions is not None
                and (
                    self._socket is None
                    or self._scope is not None
                    and self._scope.capability_updates
                ),
                "reloading": self._reloading,
                "catalog_revision": self._catalog_revision,
                "capability_errors": dict(self._capability_errors),
                "available": {
                    name: allowed and name not in self._capability_errors
                    for name, allowed in self._available.items()
                },
                "active_terminals": self.active_terminals,
                "error_code": self._error_code,
            }

    @property
    def active_terminals(self) -> int:
        with self._lock:
            handler = self._scope.handlers.get("terminal") if self._scope else None
            if handler is None and self._scope is not None and "terminal" in self._scope.retiring:
                handler = self._scope.retiring["terminal"][0]
            count = getattr(handler, "active_count", 0)
            return max(
                count if isinstance(count, int) and count >= 0 else 0, self._starting_terminals
            )

    def allow_close(self, confirm: Callable[[str, str], bool]) -> bool:
        """Fence new work before checking whether quitting needs the user's choice."""
        with self._lock:
            self._quiescing = True
            count = self.active_terminals
        if not count:
            return True
        try:
            allowed = confirm(
                "Close vBot Desktop?",
                f"{count} local terminal(s) are still running. Closing vBot Desktop stops "
                "their programs and disconnects this computer. Close it?",
            )
        except Exception:
            logger.warning("The Desktop could not confirm stopping local terminals", exc_info=True)
            allowed = False
        if not allowed:
            self.resume()
        return bool(allowed)

    def prepare_restart(self) -> bool:
        """Fence new work for an update only while no local terminal needs preserving."""
        with self._lock:
            if self.active_terminals or self._switching_server or self._disabling_terminal:
                return False
            self._quiescing = True
            return True

    @contextmanager
    def changing_server(
        self, server_url: str, confirm: Callable[[str, str], bool]
    ) -> Iterator[bool]:
        """Fence admission while a ready target replaces the selected server."""
        target = parse_server_address(server_url.rstrip("/"), None)
        with self._lock:
            origin = self._origin
            changing = bool(origin and origin != target.origin)
            busy = (
                self._closed
                or self._quiescing
                or self._switching_server
                or self._disabling_terminal
            )
            if changing and not busy:
                self._switching_server = True
            count = self.active_terminals
        if busy or not changing:
            yield not busy
            return
        try:
            allowed = True
            if count:
                try:
                    allowed = confirm(
                        "Switch vBot server?",
                        f"{count} local terminal(s) are still running. Switching to "
                        f"{target.origin} stops their programs and disconnects this computer "
                        "from its current server. Switch server?",
                    )
                except Exception:
                    logger.warning("The Desktop could not confirm switching servers", exc_info=True)
                    allowed = False
            with self._lock:
                allowed = bool(
                    allowed and self._origin == origin and not self._closed and not self._quiescing
                )
            yield allowed
        finally:
            with self._lock:
                self._switching_server = False

    def resume(self) -> None:
        """Release a closing/restart fence when the window remains open."""
        with self._lock:
            self._quiescing = False

    def update(
        self,
        changes: Any,
        *,
        expected_server: str,
        confirm: Callable[[str, str], bool] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Apply the native-approved settings only to the unchanged selected server.

        Turning Terminal off stops its running programs, so like quitting it
        asks first through ``confirm``; without one it refuses (``declined``).
        """
        # A confirmation may run on a different bridge thread. Serialize settings
        # mutations so another update cannot change the grant during the dialog.
        with self._update_lock:
            if expected_revision is not None and expected_revision != self._catalog_revision:
                raise SharingError(
                    "computer_catalog_changed",
                    "The local Extensions changed; review their sharing choices again",
                )
            return self._update(changes, expected_server=expected_server, confirm=confirm)

    def _update(
        self,
        changes: Any,
        *,
        expected_server: str,
        confirm: Callable[[str, str], bool] | None,
    ) -> dict[str, Any]:
        if not isinstance(changes, dict) or set(changes) - {"name", *self._capabilities}:
            raise SharingError("computer_sharing_invalid", "Invalid computer sharing settings")
        normalized: dict[str, Any] = {}
        for name in self._capabilities:
            if name in changes:
                if not isinstance(changes[name], bool):
                    raise SharingError(
                        "computer_sharing_invalid", "A sharing choice must be boolean"
                    )
                if changes[name] and not self._available.get(name, False):
                    raise SharingError(
                        "computer_capability_unavailable", "This capability is unavailable"
                    )
                normalized[name] = changes[name]
        if "name" in changes:
            name = changes["name"]
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name.strip()) > 80
                or not name.isprintable()
            ):
                raise SharingError(
                    "computer_name_invalid", "Use a computer name of 1 to 80 printable characters"
                )
            normalized["name"] = name.strip()
        with self._lock:
            if not self._origin or expected_server != self._server_url or self._closed:
                raise SharingError(
                    "computer_server_changed",
                    "The selected server changed; open the settings again",
                )
            origin = self._origin
            stopping = normalized.get("terminal") is False and _grants(
                self._settings, origin, self._capabilities
            ).get("terminal", False)
            if stopping:
                self._disabling_terminal = True
        try:
            count = self.active_terminals if stopping else 0
            if count:
                try:
                    allowed = confirm is not None and confirm(
                        "Stop sharing Terminal?",
                        f"{count} local terminal(s) are still running. Turning off Terminal "
                        "sharing stops their programs. Turn it off?",
                    )
                except Exception:
                    logger.warning(
                        "The Desktop could not confirm stopping local terminals", exc_info=True
                    )
                    allowed = False
                if not allowed:
                    raise SharingError("declined", "Terminal sharing stays on")
            with self._lock:
                if not self._origin or expected_server != self._server_url or self._closed:
                    raise SharingError(
                        "computer_server_changed",
                        "The selected server changed; open the settings again",
                    )
                origin = self._origin

                def apply(settings: dict[str, Any]) -> dict[str, Any]:
                    if "name" in normalized:
                        settings["name"] = normalized["name"]
                    servers = settings.setdefault("servers", {})
                    if not isinstance(servers, dict):
                        raise SharingError(
                            "computer_sharing_invalid",
                            "Stored computer sharing settings are invalid",
                        )
                    grants = servers.setdefault(origin, {})
                    if not isinstance(grants, dict):
                        raise SharingError(
                            "computer_sharing_invalid",
                            "Stored computer sharing settings are invalid",
                        )
                    grants.update(
                        {
                            name: normalized[name]
                            for name in self._capabilities
                            if name in normalized
                        }
                    )
                    sources = settings.setdefault("capability_sources", {})
                    if not isinstance(sources, dict):
                        raise SharingError(
                            "computer_sharing_invalid", "Stored capability sources are invalid"
                        )
                    server_sources = sources.setdefault(origin, {})
                    if not isinstance(server_sources, dict):
                        raise SharingError(
                            "computer_sharing_invalid", "Stored capability sources are invalid"
                        )
                    server_sources.update(
                        {
                            name: self._capabilities[name].source
                            for name in self._capabilities
                            if normalized.get(name) is True
                        }
                    )
                    return settings

                updated = update_section(SETTINGS_KEY, apply, self._settings_path)
                self._settings = updated
                for capability in self._capabilities:
                    if normalized.get(capability) is False:
                        self._capability_errors.pop(capability, None)
                if any(_grants(updated, origin, self._capabilities).values()):
                    self._identities.computer_identity(origin, create=True)
                self._wake()
        finally:
            if stopping:
                with self._lock:
                    self._disabling_terminal = False
        logger.info("Computer sharing settings changed (fields=%s)", ",".join(sorted(normalized)))
        self._report()
        return self.status()

    def reload_extensions(
        self, *, expected_server: str, confirm: Callable[[str, str], bool] | None = None
    ) -> dict[str, Any]:
        """Rescan only local packages after the bridge's native page consent.

        The existing owner fences changed handlers immediately, then retires
        their resources on its native loop. Unchanged handlers keep their scope.
        """
        with self._update_lock:
            with self._lock:
                if self._reload_extensions is None:
                    raise SharingError(
                        "computer_reload_unavailable", "Local Extension reload is unavailable"
                    )
                if self._socket is not None and (
                    self._scope is None or not self._scope.capability_updates
                ):
                    raise SharingError(
                        "computer_reload_unavailable",
                        "This server does not support live native Extension reload",
                    )
                if expected_server != self._server_url or self._closed:
                    raise SharingError(
                        "computer_server_changed",
                        "The selected server changed; open the settings again",
                    )
                if self._reloading or self._quiescing or self._switching_server:
                    raise SharingError(
                        "computer_reload_busy",
                        "The Desktop is already changing its native capabilities",
                    )

            def before_load(affected: frozenset[str]) -> None:
                with self._lock:
                    if expected_server != self._server_url or self._closed:
                        raise SharingError(
                            "computer_server_changed",
                            "The selected server changed; open the settings again",
                        )
                    if self._socket is not None and (
                        self._scope is None or not self._scope.capability_updates
                    ):
                        raise SharingError(
                            "computer_reload_unavailable",
                            "This server does not support live native Extension reload",
                        )
                    self._disabling_terminal = "terminal" in affected
                count = self.active_terminals if "terminal" in affected else 0
                if count:
                    try:
                        allowed = confirm is not None and confirm(
                            "Reload Desktop Extensions?",
                            f"{count} local terminal(s) are still running. The Terminal Extension "
                            "changed. Reloading stops their programs. Reload it?",
                        )
                    except Exception:
                        logger.warning(
                            "The Desktop could not confirm reloading local terminals", exc_info=True
                        )
                        allowed = False
                    if not allowed:
                        raise SharingError("declined", "The local Extensions were not reloaded")
                with self._lock:
                    if (
                        expected_server != self._server_url
                        or self._closed
                        or self._quiescing
                        or self._switching_server
                    ):
                        raise SharingError(
                            "computer_server_changed",
                            "The selected server changed; open the settings again",
                        )
                    if self._socket is not None and (
                        self._scope is None or not self._scope.capability_updates
                    ):
                        raise SharingError(
                            "computer_reload_unavailable",
                            "This server does not support live native Extension reload",
                        )

            try:
                catalog = self._reload_extensions(self._extension_catalog, before_load)
                with self._lock:
                    if (
                        expected_server != self._server_url
                        or self._closed
                        or self._quiescing
                        or self._switching_server
                    ):
                        raise SharingError(
                            "computer_server_changed",
                            "The selected server changed; open the settings again",
                        )
                    if self._socket is not None and (
                        self._scope is None or not self._scope.capability_updates
                    ):
                        raise SharingError(
                            "computer_reload_unavailable",
                            "This server does not support live native Extension reload",
                        )
                    previous_grants = _grants(self._settings, self._origin, self._capabilities)
                    next_grants = _grants(self._settings, self._origin, catalog.capabilities)
                    previous_active = {
                        name: item.identity
                        for name, item in self._capabilities.items()
                        if previous_grants[name] and item.available
                    }
                    next_active = {
                        name: item.identity
                        for name, item in catalog.capabilities.items()
                        if next_grants[name] and item.available
                    }
                    needs_reconfigure = previous_active != next_active or bool(
                        self._capability_errors
                    )
                    self._capabilities = dict(catalog.capabilities)
                    # The fingerprint baseline advances atomically with the
                    # accepted declarations, never merely because imports ran.
                    self._extension_catalog = catalog
                    self._available = {
                        name: item.available for name, item in self._capabilities.items()
                    }
                    self._extension_records = list(catalog.records)
                    self._capability_errors = {}
                    self._catalog_revision += 1
                    self._reloading = self._loop is not None and needs_reconfigure
                    if needs_reconfigure:
                        self._wake()
            finally:
                with self._lock:
                    self._disabling_terminal = False
        self._report()
        return self.status()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wake()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=30)
            if thread.is_alive():
                logger.error("Desktop computer execution did not close within 30 seconds")

    def _wake(self) -> None:
        if self._loop is not None and self._changed is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._changed.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self.run())
        except Exception:
            logger.exception("Desktop computer execution stopped unexpectedly")
            self._set_connection(False, "computer_execution_failed")

    async def run(self) -> None:
        """Serve until close; also usable on an owned Event Loop in protocol tests."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._changed = asyncio.Event()
        retry_seconds = 1.0
        try:
            while not self._closed:
                self._changed.clear()
                with self._lock:
                    origin, server_url = self._origin, self._server_url
                    settings = self._settings
                    grants = _grants(settings, origin, self._capabilities)
                    enabled = {
                        name
                        for name, allowed in grants.items()
                        if allowed and self._available[name]
                    }
                if self._scope is not None and self._scope.origin != origin:
                    await self._close_scope()
                if not origin or not enabled:
                    await self._close_scope()
                    with self._lock:
                        self._reloading = False
                    self._set_connection(False, None)
                    self._report()
                    await self._changed.wait()
                    continue
                welcomes = self._welcomes
                try:
                    identity = self._identities.computer_identity(origin, create=True)
                    assert identity is not None
                    if self._scope is not None and self._scope.identity != identity:
                        await self._close_scope()
                    if self._scope is None:
                        self._scope = _Scope(origin, identity)
                    scope = self._scope
                    await self._configure(scope)
                    await self._run_connection(
                        scope, server_url, _computer_name(settings.get("name"))
                    )
                    retry_seconds = 1.0
                except _ServerRestartedError:
                    await self._close_scope()
                    continue
                except Exception as error:
                    self._set_connection(False, _connection_error(error))
                    if self._welcomes != welcomes:
                        retry_seconds = 1.0  # a working link lost later starts a fresh backoff
                if self._closed or self._changed.is_set():
                    continue
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=retry_seconds)
                except TimeoutError:
                    retry_seconds = min(30.0, retry_seconds * 2)
        finally:
            await self._close_scope()
            self._set_connection(False, None)
            with self._lock:
                self._loop = None
                self._changed = None

    async def _run_connection(self, scope: _Scope, server_url: str, name: str) -> None:
        """Refresh native handlers without breaking another handler's admitted calls."""
        assert self._changed is not None
        serving = asyncio.create_task(self._serve(scope, server_url, name))
        configuring: asyncio.Task[None] | None = None
        changed: asyncio.Task[bool] | None = None
        refresh_again = False
        try:
            while True:
                changed = asyncio.create_task(self._changed.wait())
                waiting = [serving, changed]
                if configuring is not None:
                    waiting.append(configuring)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if serving in done:
                    await serving
                    return
                if changed in done:
                    self._changed.clear()
                    with self._lock:
                        refresh = (
                            (self._reloading or configuring is not None)
                            and not self._closed
                            and self._origin == scope.origin
                            and self._socket is not None
                            and scope.capability_updates
                        )
                    if not refresh:
                        self._changed.set()
                        return
                    if configuring is None:
                        configuring = asyncio.create_task(self._configure(scope, live=True))
                    else:
                        refresh_again = True
                else:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                if configuring is not None and configuring in done:
                    await configuring
                    if refresh_again:
                        refresh_again = False
                        configuring = asyncio.create_task(self._configure(scope, live=True))
                        continue
                    configuring = None
                    socket = self._socket
                    if socket is not None:
                        await self._send(
                            socket, {"type": "capabilities", "capabilities": sorted(scope.ready)}
                        )
                    with self._lock:
                        self._reloading = False
                    self._report()
        finally:
            owned = [task for task in (serving, configuring, changed) if task is not None]
            for task in owned:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)

    async def _configure(self, scope: _Scope, *, live: bool = False) -> None:
        errors: dict[str, str] = {}
        with self._lock:
            declarations = dict(self._capabilities)
            revision = self._catalog_revision
            enabled = {
                name
                for name, allowed in _grants(self._settings, self._origin, declarations).items()
                if allowed and self._available[name]
            }
        retired = {
            name
            for name in scope.handlers
            if name not in enabled
            or scope.declarations[name].identity != declarations[name].identity
        }
        for capability in retired:
            scope.ready.discard(capability)
            scope.retiring[capability] = (
                scope.handlers.pop(capability),
                scope.declarations.pop(capability),
            )
        for capability, (removed, _declaration) in tuple(scope.retiring.items()):
            if capability not in scope.retirement_tasks:
                scope.retirement_tasks[capability] = asyncio.create_task(
                    self._retire_handler(scope, capability, removed)
                )
        if scope.retirement_tasks:
            # One budget covers the entire batch. A timed-out close or admitted
            # call remains owned and quarantined; cancelling its await could hide
            # a still-running worker and incorrectly admit a second handler.
            await _wait_for_retirement(scope.retirement_tasks.values(), timeout=RETIREMENT_TIMEOUT)
        for capability, task in tuple(scope.retirement_tasks.items()):
            if not task.done():
                errors[capability] = "retirement: timeout; cleanup still pending"
                continue
            scope.retirement_tasks.pop(capability)
            if task.cancelled():
                errors[capability] = "close: CancelledError"
                continue
            try:
                task.result()
                scope.retiring.pop(capability)
            except Exception as error:
                errors[capability] = f"close: {type(error).__name__}"
        for capability in sorted(enabled):
            if capability in scope.retiring:
                continue
            try:
                handler = scope.handlers.get(capability)
                if handler is None:
                    declaration = declarations[capability]

                    def emit(
                        event: str,
                        data: dict[str, Any],
                        cap: str = capability,
                        declared: NativeCapability = declaration,
                    ) -> None:
                        self._emit(scope, cap, declared, event, data)

                    handler = declaration.factory(emit)
                    scope.handlers[capability] = handler
                    scope.declarations[capability] = declaration
                    if not all(
                        callable(getattr(handler, method, None))
                        for method in ("execute", "disconnected", "close")
                    ):
                        raise TypeError("A capability factory must return an execution handler")
                ready = getattr(handler, "ready", None)
                reason = await ready() if ready is not None else None
                if reason is None:
                    scope.ready.add(capability)
                    continue
                errors[capability] = str(reason)
            except Exception as error:
                errors[capability] = type(error).__name__
            # Failed initialization must not break another capability's native
            # lifetime or advertise the incomplete handler on the next link.
            failed = scope.handlers.pop(capability, None)
            scope.ready.discard(capability)
            scope.declarations.pop(capability, None)
            if failed is not None:
                try:
                    await failed.close()
                except Exception as error:
                    errors[capability] += f"; close: {type(error).__name__}"
        with self._lock:
            self._capability_errors = errors
            scope.catalog_revision = revision
            if self._catalog_revision == revision and not live:
                self._reloading = False
        self._report()
        if not scope.handlers and not live:
            raise SharingError(
                "computer_capability_unavailable", "The shared capabilities are unavailable"
            )

    async def _retire_handler(
        self, scope: _Scope, capability: str, handler: ExecutionHandler
    ) -> None:
        """Close exactly once while pending; retain every admitted call to settlement."""
        await handler.close()
        pending = [task for task, name in scope.capability_calls.items() if name == capability]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _serve(self, scope: _Scope, server_url: str, name: str) -> None:
        header = self._authorization.header(server_url)
        self._serving_header = header
        try:
            async with self._connector(server_url, header) as socket:
                offered_revision = scope.catalog_revision
                await socket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "computer_id": scope.identity.computer_id,
                            "key": scope.identity.key,
                            "instance_id": scope.instance_id,
                            "name": name,
                            "platform": sys.platform,
                            "capabilities": sorted(scope.handlers),
                        }
                    )
                )
                welcome = _message(await asyncio.wait_for(socket.recv(), timeout=10))
                if welcome.get("type") == "error":
                    raise _refusal(welcome)
                if (
                    welcome.get("type") != "welcome"
                    or welcome.get("protocol") != 1
                    or welcome.get("computer_id") != scope.identity.computer_id
                    or welcome.get("instance_id") != scope.instance_id
                    or not isinstance(welcome.get("server_instance"), str)
                ):
                    raise SharingError(
                        "computer_protocol_error", "The server returned an invalid welcome"
                    )
                with self._lock:
                    server_instance = welcome["server_instance"]
                    if (
                        scope.server_instance is not None
                        and scope.server_instance != server_instance
                    ):
                        raise _ServerRestartedError()
                    scope.server_instance = server_instance
                    scope.capability_updates = welcome.get("capability_updates") == 1
                    if not scope.capability_updates and offered_revision != self._catalog_revision:
                        # Reload completed while an old server was handshaking.
                        # It has admitted no calls; reconnect with the new offer
                        # before publishing this link as usable.
                        self._wake()
                        return
                    scope.connection += 1
                    self._welcomes += 1
                    self._socket = socket
                self._set_connection(True, None)
                while True:
                    message = _message(await socket.recv())
                    if message.get("type") == "error":
                        raise _refusal(message)
                    if message.get("type") != "call":
                        raise SharingError(
                            "computer_protocol_error", "The server sent an invalid request"
                        )
                    call_id = message.get("id")
                    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                        raise SharingError(
                            "computer_protocol_error", "The request has no valid identity"
                        )
                    if call_id in scope.seen:
                        await self._failure(
                            socket,
                            call_id,
                            "computer_duplicate_call",
                            "This request was already received; no action was repeated",
                        )
                        continue
                    if len(scope.calls) >= MAX_PENDING_CALLS:
                        await self._failure(
                            socket,
                            call_id,
                            "computer_busy",
                            "The computer has too many pending operations; nothing was started",
                        )
                        continue
                    scope.seen.add(call_id)
                    task = asyncio.create_task(self._call(scope, socket, scope.connection, message))
                    scope.calls.add(task)
                    task.add_done_callback(scope.calls.discard)
                    capability = message.get("capability")
                    if isinstance(capability, str):
                        scope.capability_calls[task] = capability
                        task.add_done_callback(
                            lambda completed: scope.capability_calls.pop(completed, None)
                        )
        except Exception as error:
            if _unauthorized(error):
                self._authorization.rejected(server_url, header)
            raise
        finally:
            self._socket = None
            scope.connection += 1
            self._set_connection(False, self._error_code)
            await asyncio.gather(*(handler.disconnected() for handler in scope.handlers.values()))

    async def _call(
        self, scope: _Scope, socket: Socket, connection: int, request: dict[str, Any]
    ) -> None:
        call_id = request["id"]
        starting_terminal = False
        try:
            if scope.connection != connection or scope is not self._scope:
                return
            capability, operation, params = (
                request.get("capability"),
                request.get("operation"),
                request.get("params"),
            )
            handler = scope.handlers.get(capability) if isinstance(capability, str) else None
            read_only = isinstance(operation, str) and operation in getattr(
                handler, "read_only_operations", ()
            )
            with self._lock:
                grants = _grants(self._settings, self._origin, self._capabilities)
                if (
                    self._origin != scope.origin
                    or not isinstance(capability, str)
                    or not grants.get(capability, False)
                    or capability not in scope.declarations
                    or capability not in scope.ready
                    or capability not in self._capabilities
                    or scope.declarations[capability].identity
                    != self._capabilities[capability].identity
                ):
                    raise SharingError(
                        "computer_capability_not_shared",
                        "This capability is not shared; nothing was done",
                    )
                if (self._quiescing or self._switching_server) and not read_only:
                    raise SharingError(
                        "computer_closing",
                        "The Desktop is closing, restarting or switching servers; nothing was done",
                    )
                if self._disabling_terminal and capability == "terminal" and operation == "start":
                    raise SharingError(
                        "computer_closing", "Terminal sharing is being turned off; nothing was done"
                    )
                if capability == "terminal" and operation == "start":
                    self._starting_terminals += 1
                    starting_terminal = True
            if handler is None:
                raise SharingError(
                    "computer_capability_not_shared",
                    "This capability is not shared; nothing was done",
                )
            if not isinstance(operation, str) or not operation or not isinstance(params, dict):
                raise SharingError(
                    "invalid_arguments",
                    "The operation and its parameters are required; nothing was done",
                )
            result = await handler.execute(operation, params)
            if not isinstance(result, dict):
                raise TypeError("A computer capability returned an invalid result")
            await self._send(
                socket, {"type": "result", "id": call_id, "ok": True, "result": result}
            )
        except Exception as error:
            code = getattr(error, "code", getattr(error, "error_code", None))
            if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
                code = "computer_execution_error"
                message = (
                    "The computer operation failed; its effects are unknown. "
                    "Check the current state before repeating it."
                )
                logger.error("Computer operation failed (error_type=%s)", type(error).__name__)
            else:
                message = str(error)
            await self._failure(socket, call_id, code, message)
        finally:
            # Pending requests remain deduplicated even while newer requests
            # exhaust the bounded history of completed operations.
            scope.seen_order.append(call_id)
            if len(scope.seen_order) > MAX_REMEMBERED_CALLS:
                scope.seen.discard(scope.seen_order.popleft())
            if starting_terminal:
                with self._lock:
                    self._starting_terminals -= 1
            self._report()

    async def _send(self, socket: Socket, message: dict[str, Any]) -> None:
        if self._socket is not socket:
            return
        encoded = json.dumps(message)
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Computer response exceeds transport limit")
        # The receive loop reports a lost connection; a reply is never replayed.
        with contextlib.suppress(Exception):
            await socket.send(encoded)

    async def _failure(self, socket: Socket, call_id: str, code: str, message: str) -> None:
        await self._send(
            socket,
            {
                "type": "result",
                "id": call_id,
                "ok": False,
                "error": {"code": code, "message": message[:4000]},
            },
        )

    def _emit(
        self,
        scope: _Scope,
        capability: str,
        declaration: NativeCapability,
        event: str,
        data: dict[str, Any],
    ) -> None:
        def deliver() -> None:
            socket = self._socket
            if (
                socket is None
                or self._scope is not scope
                or scope.declarations.get(capability) is not declaration
                or capability not in scope.ready
                or self._capabilities.get(capability) is None
                or self._capabilities[capability].identity != declaration.identity
            ):
                return
            task = asyncio.create_task(
                self._send(
                    socket,
                    {"type": "event", "capability": capability, "event": event, "data": data},
                )
            )
            scope.calls.add(task)
            task.add_done_callback(scope.calls.discard)

        with self._lock:
            if self._loop is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(deliver)

    async def _close_scope(self) -> None:
        scope = self._scope
        if scope is None:
            return
        self._socket = None
        scope.connection += 1
        disconnects = await asyncio.gather(
            *(handler.disconnected() for handler in scope.handlers.values()), return_exceptions=True
        )
        closed = await asyncio.gather(
            *(handler.close() for handler in scope.handlers.values()),
            *(
                scope.retirement_tasks[capability]
                if capability in scope.retirement_tasks
                else handler.close()
                for capability, (handler, _declaration) in scope.retiring.items()
            ),
            return_exceptions=True,
        )
        if scope.calls:
            await asyncio.gather(*tuple(scope.calls), return_exceptions=True)
        self._scope = None
        for result in (*disconnects, *closed):
            if isinstance(result, BaseException):
                raise result

    def _set_connection(self, connected: bool, error_code: str | None) -> None:
        with self._lock:
            previous = self._connected, self._error_code
            self._connected, self._error_code = connected, error_code
        if previous == (connected, error_code):
            return
        if connected:
            logger.info("Desktop computer connected")
        elif error_code and previous[1] != error_code:
            logger.warning("Desktop computer unavailable (code=%s)", error_code)
        self._report()

    def _report(self) -> None:
        status = self.status()
        with self._lock:
            if status == self._last_status:
                return
            self._last_status = status
        self._publish(status)


class _ServerRestartedError(Exception):
    pass


def _message(raw: str | bytes) -> dict[str, Any]:
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise SharingError("computer_protocol_error", "The server sent an invalid message")
    return message


def _refusal(message: dict[str, Any]) -> SharingError:
    """The server's own reason, such as an identity conflict, instead of a protocol error."""
    error = message.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    wording = error.get("message") if isinstance(error, dict) else None
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        code = "computer_protocol_error"
    return SharingError(
        code,
        wording if isinstance(wording, str) else "The server refused this computer",
    )


def _grants(
    settings: dict[str, Any], origin: str, capabilities: Mapping[str, NativeCapability]
) -> dict[str, bool]:
    servers = settings.get("servers")
    grants = servers.get(origin) if isinstance(servers, dict) else None
    sources = settings.get("capability_sources")
    server_sources = sources.get(origin) if isinstance(sources, dict) else None
    return {
        name: isinstance(grants, dict)
        and grants.get(name) is True
        and (
            (isinstance(server_sources, dict) and server_sources.get(name) == item.source)
            or (not item.source or item.source.startswith("builtin:"))
            and (not isinstance(server_sources, dict) or name not in server_sources)
        )
        for name, item in capabilities.items()
    }


def _computer_name(value: Any) -> str:
    return value if isinstance(value, str) and value else platform.node() or "This computer"


def _unauthorized(error: Exception) -> bool:
    response = getattr(error, "response", None)
    received = getattr(error, "rcvd", None)
    return getattr(response, "status_code", None) == 401 or getattr(received, "code", None) == 4401


def _connection_error(error: Exception) -> str:
    if _unauthorized(error):
        return "auth_required"
    code = getattr(error, "error_code", None)
    return (
        code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else "computer_disconnected"
    )
