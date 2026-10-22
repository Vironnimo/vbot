"""Portals: private links through which the user opens a web app running on this computer.

A Portal forwards one port of the vBot server, an origin of its own, to one
``localhost`` port where an app the Agent started listens, such as a dev
server. The registry owns the Portal records and their lifetime; the server
attaches the network side (``PortalListeners``), which binds the listener and
forwards each request under the server's access rules.

Portals are process-local: they end when their Session is archived or its
Extension releases it, when the Agent or the user closes them, and when vBot
stops; they follow a renamed Identity Agent's Sessions. Closing a Portal never
stops the app. While Portals are open, a watch connects to each target now and
then, so the card can show whether the app answers
(``.vorch/domain-maps/tools/portal.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from core.sessions import SessionAddress
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.portal")

MAX_PORTALS_PER_SESSION = 5
MAX_PORTALS = 20
#: How often the watch connects to a target that does not answer, and to one that does.
WAITING_PROBE_SECONDS = 2.0
LISTENING_PROBE_SECONDS = 10.0
PROBE_TIMEOUT_SECONDS = 3.0
PORTAL_ID_PREFIX = "p"


@dataclass(frozen=True)
class Portal:
    """One open Portal. ``listening`` is whether the app answered the last connection."""

    id: str
    owner: SessionAddress
    title: str
    path: str
    target_port: int
    listen_port: int
    listening: bool


class PortalError(Exception):
    """A Portal operation that did not happen; ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PortalListeners(Protocol):
    """The network side of Portals, which the server provides.

    ``open`` binds a new listener for *portal_id* that forwards to
    ``localhost:<target_port>`` and returns its port; ``close`` stops it at once
    and ends its connections. ``own_ports`` are the ports vBot itself serves,
    which no Portal may target. ``address`` is the URL of a listen port without
    any credential.
    """

    own_ports: frozenset[int]

    async def open(self, portal_id: str, target_port: int) -> int: ...

    def close(self, portal_id: str) -> None: ...


async def probe_localhost(port: int) -> bool:
    """Whether anything accepts a TCP connection on ``localhost:<port>``, IPv4 or IPv6."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("localhost", port, happy_eyeballs_delay=0.25),
            PROBE_TIMEOUT_SECONDS,
        )
    except OSError, TimeoutError:
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


class PortalRegistry:
    """Open, list and close Portals; tell subscribers which Session's Portals changed.

    Subscribers are called on the Event Loop with the owning Session's address.
    """

    def __init__(
        self,
        *,
        probe: Callable[[int], Awaitable[bool]] = probe_localhost,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._probe = probe
        self._sleep = sleep
        self._clock = clock
        self._listeners: PortalListeners | None = None
        self._portals: dict[str, Portal] = {}
        self._next_probe: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[Callable[[SessionAddress], None]] = []
        self._watch: asyncio.Task[None] | None = None

    # Wiring ------------------------------------------------------------------

    def attach(self, listeners: PortalListeners) -> None:
        """Use *listeners* for every Portal opened from now on."""
        self._listeners = listeners

    def detach(self) -> None:
        """Close every Portal and stop opening new ones; for server shutdown."""
        self.stop()
        self._listeners = None

    @property
    def available(self) -> bool:
        return self._listeners is not None

    def subscribe(self, callback: Callable[[SessionAddress], None]) -> Callable[[], None]:
        """Call *callback* whenever a Session's Portals change; returns the unsubscribe."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(callback)

        return unsubscribe

    # Operations --------------------------------------------------------------

    def list(self, owner: SessionAddress) -> list[Portal]:
        """The Session's open Portals in the order they were opened."""
        return [portal for portal in self._portals.values() if portal.owner == owner]

    def get(self, portal_id: str) -> Portal | None:
        return self._portals.get(portal_id)

    async def open(
        self, owner: SessionAddress, *, target_port: int, title: str, path: str
    ) -> tuple[Portal, bool]:
        """Open a Portal to *target_port*; returns it and whether it was opened now.

        A Portal of this Session to the same port and path is returned as it
        is. Raises ``PortalError`` with ``unavailable``, ``own_port`` or
        ``limit``.
        """
        async with self._lock:
            listeners = self._listeners
            if listeners is None:
                raise PortalError("unavailable", "Portals are not available")
            for portal in self.list(owner):
                if portal.target_port == target_port and portal.path == path:
                    return portal, False
            if target_port in listeners.own_ports or any(
                portal.listen_port == target_port for portal in self._portals.values()
            ):
                raise PortalError("own_port", f"Port {target_port} is served by vBot itself")
            if len(self.list(owner)) >= MAX_PORTALS_PER_SESSION:
                raise PortalError(
                    "limit", f"This Session already has {MAX_PORTALS_PER_SESSION} portals"
                )
            if len(self._portals) >= MAX_PORTALS:
                raise PortalError("limit", f"vBot already has {MAX_PORTALS} portals open")
            portal_id = self._new_id()
            listen_port = await listeners.open(portal_id, target_port)
            try:
                listening = await self._probe(target_port)
            except BaseException:
                # No record names the listener yet; a cancelled open must not leak it.
                listeners.close(portal_id)
                raise
            portal = Portal(
                portal_id, owner, title, path, target_port, listen_port, listening=listening
            )
            self._portals[portal_id] = portal
            self._schedule_probe(portal)
        _LOGGER.info(
            "Portal opened (id=%s target=%s listen=%s)", portal_id, target_port, listen_port
        )
        self._changed(owner)
        self._ensure_watch()
        return portal, True

    def close(self, portal_id: str) -> Portal | None:
        """Close one Portal; returns it, or ``None`` when no such Portal is open."""
        portal = self._portals.pop(portal_id, None)
        if portal is None:
            return None
        self._next_probe.pop(portal_id, None)
        if self._listeners is not None:
            try:
                self._listeners.close(portal_id)
            except Exception:
                _LOGGER.exception("Closing a portal listener failed (id=%s)", portal_id)
        _LOGGER.info("Portal closed (id=%s target=%s)", portal_id, portal.target_port)
        self._changed(portal.owner)
        return portal

    def close_sessions(self, addresses: Iterable[SessionAddress]) -> int:
        """Close the Portals of these Sessions, such as archived ones; returns how many."""
        owners = set(addresses)
        closed = [
            portal_id for portal_id, portal in self._portals.items() if portal.owner in owners
        ]
        for portal_id in closed:
            self.close(portal_id)
        return len(closed)

    def transfer_agent_scope(self, agent_id: str, new_agent_id: str) -> int:
        """Move the Portals of a renamed Identity Agent's Sessions to its new id.

        Each Portal keeps its id, listener and credential; only its owner changes,
        so the moved Session lists and closes it. Returns how many moved.
        """
        moved: list[tuple[SessionAddress, SessionAddress]] = []
        for portal_id, portal in list(self._portals.items()):
            owner = portal.owner
            if owner.project_id is None and owner.agent_id == agent_id:
                target = replace(owner, agent_id=new_agent_id)
                self._portals[portal_id] = replace(portal, owner=target)
                moved.append((owner, target))
        for owner in dict.fromkeys(address for pair in moved for address in pair):
            self._changed(owner)
        return len(moved)

    def report_listening(self, portal_id: str, listening: bool) -> None:
        """Record what forwarding a request learned about whether the app answers."""
        portal = self._portals.get(portal_id)
        if portal is None or portal.listening == listening:
            return
        self._portals[portal_id] = replace(portal, listening=listening)
        self._schedule_probe(self._portals[portal_id])
        self._changed(portal.owner)

    def stop(self) -> None:
        """Close every Portal and end the watch."""
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None
        for portal_id in list(self._portals):
            self.close(portal_id)

    async def aclose(self) -> None:
        watch = self._watch
        self.stop()
        if watch is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await watch

    # Watch -------------------------------------------------------------------

    def _ensure_watch(self) -> None:
        if self._watch is None or self._watch.done():
            self._watch = asyncio.get_running_loop().create_task(
                self._watch_targets(), name="portal-watch"
            )

    async def _watch_targets(self) -> None:
        while self._portals:
            await self._sleep(1.0)
            now = self._now()
            due = [
                portal
                for portal in self._portals.values()
                if self._next_probe.get(portal.id, 0.0) <= now
            ]
            for portal in due:
                listening = await self._probe(portal.target_port)
                current = self._portals.get(portal.id)
                if current is None:
                    continue
                if current.listening != listening:
                    self.report_listening(portal.id, listening)
                else:
                    self._schedule_probe(current)

    def _schedule_probe(self, portal: Portal) -> None:
        delay = LISTENING_PROBE_SECONDS if portal.listening else WAITING_PROBE_SECONDS
        self._next_probe[portal.id] = self._now() + delay

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    def _new_id(self) -> str:
        while True:
            portal_id = f"{PORTAL_ID_PREFIX}{secrets.token_hex(4)}"
            if portal_id not in self._portals:
                return portal_id

    def _changed(self, owner: SessionAddress) -> None:
        for callback in list(self._subscribers):
            try:
                callback(owner)
            except Exception:
                _LOGGER.exception("A portal change subscriber failed")


def describe_portals(portals: Sequence[Portal]) -> str:
    """Portal ids in a sentence, for errors that name the valid choices."""
    return ", ".join(portal.id for portal in portals) or "none"
