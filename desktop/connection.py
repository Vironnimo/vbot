"""In-window server selection and sign-in for the vBot pywebview Desktop accessor.

The Desktop owns a small native connection screen instead of a dead-end error
page: it lists the remembered servers, lets the user add/select one, and
auto-connects to the last-used target on launch. Every failure (unreachable
server, non-vBot server, missing WebUI, invalid target, refused sign-in) lands
the user back on the *same* interactive screen with the failed target
prefilled, so there is never a page the user cannot recover from.

Two pieces live here:

- Remembered-servers operations over :mod:`desktop.settings`
  (:func:`list_servers` / :func:`add_server` / :func:`select_server` /
  :func:`remove_server` / :func:`resolve_last_used`). A server is a host and
  port (HTTP) or an ``https://`` address (:mod:`desktop.server_address`).
- :class:`ConnectionController`, which holds the live pywebview ``Window`` and
  drives the launch connect and server switches: a successful probe navigates
  the window to the server's WebUI via ``Window.load_url``; any failure renders
  the connection screen (:mod:`desktop.connection_screen`) inline via
  ``Window.load_html``. The launch connect waits for a server that does not
  answer yet (a server still starting after an update or at login), showing a
  waiting state meanwhile.

A server in the access mode ``authenticated`` needs a sign-in
(:mod:`desktop.access`): the window opens a login ticket, whose redemption gives
the WebView its cookie and redirects to ``/``, and then loads the WebUI with the
Desktop's parameters. Without a credential the connection screen shows a
pairing code until the pairing is approved, denied or expires, or the user
cancels it. A ticket never reaches page JavaScript: a connect the page starts
returns :data:`SIGN_IN_PAGE`, and the controller opens the ticket once that
page has loaded (:meth:`ConnectionController.page_loaded`).

pywebview is imported lazily (like the rest of ``desktop/``) so the backend test
gate never requires the optional GUI package; tests inject a fake window.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from desktop._windows import url_origin
from desktop.access import TICKET_PATH_PREFIX, AccessFailure, PairingRequest, ServerAccess
from desktop.connection_screen import (
    STATUS_PAIRING_DENIED,
    STATUS_PAIRING_EXPIRED,
    STATUS_PAIRING_REQUIRED,
    STATUS_SIGN_IN_FAILED,
    PairingView,
    build_connection_html,
    connection_error_copy,
)
from desktop.main import (
    ACCESS_MODE_AUTHENTICATED,
    ACCESSOR_QUERY_PARAM,
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
    SessionLink,
    probe_target,
)
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

logger = logging.getLogger("vbot.desktop.connection")

DESKTOP_SESSION_QUERY_PARAM = "desktop_session"
# The WebUI opens this Session once it has loaded and removes both parameters.
OPEN_AGENT_QUERY_PARAM = "open_agent"
OPEN_SESSION_QUERY_PARAM = "open_session"
SIGN_IN_PAGE = "about:blank"
"""Where a page goes when its connect needs a sign-in; the controller opens the ticket next."""
LAUNCH_WAIT_SECONDS = 60.0
"""How long the launch connect keeps trying a server that is unreachable."""
LAUNCH_RETRY_SECONDS = 0.25
"""How often a waiting launch connect starts another attempt.

A loopback attempt fails within 0.2 s (``LOOPBACK_CONNECT_TIMEOUT_SECONDS``), so a
short interval lets the window connect soon after a starting server answers.
"""
SIGN_IN_LANDING_SECONDS = 60.0
"""How long a window sign-in may take to land: a login ticket's validity."""
# close() waits this long for a background connect step to stop. It never
# touches the window once ended, so a longer request in flight may finish on its own.
_CLOSE_JOIN_SECONDS = 1.0
_PAIRING_FAILURES = {"denied": STATUS_PAIRING_DENIED, "expired": STATUS_PAIRING_EXPIRED}


class WindowProtocol(Protocol):
    """Subset of a pywebview ``Window`` the controller drives.

    pywebview keeps the same ``Window`` across navigation, so the controller can
    swap between the remote WebUI and the local connection screen on one window.
    """

    def load_url(self, url: str) -> Any:
        """Navigate the live window to a URL."""

    def load_html(self, content: str) -> Any:
        """Replace the live window content with inline HTML."""

    def get_current_url(self) -> str | None:
        """Return the URL of the loaded page (``None`` for inline HTML)."""


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


@dataclass(frozen=True)
class PreparedConnection:
    """A connect outcome whose visible navigation has not happened yet.

    pywebview API calls return through a Promise callback stored in the current
    document. Replacing that document before the Python method returns destroys
    the callback and makes pywebview raise while delivering the result. This
    value lets bridge callers receive the outcome first and navigate from
    JavaScript afterwards, while native callers apply the same outcome
    themselves.

    ``failure`` names a sign-in outcome beside the probe ``result`` (a pairing
    waiting for approval included). ``ticket_url`` signs the window in and
    never leaves the Desktop: a bridge caller gets :data:`SIGN_IN_PAGE` as
    ``navigation_url``.
    """

    result: DesktopProbeResult
    navigation_url: str | None = None
    error_title: str | None = None
    error_body: str | None = None
    failure: str | None = None
    pairing: PairingView | None = None
    ticket_url: str | None = field(default=None, repr=False)

    @property
    def status(self) -> str:
        """The outcome: a sign-in status, else the probe status."""

        return self.failure or self.result.status

    def to_bridge_payload(self) -> dict[str, Any]:
        """Return the plain payload consumed by Desktop JavaScript callers."""

        payload: dict[str, Any] = {"status": self.status}
        if self.navigation_url is not None:
            payload["url"] = self.navigation_url
        if self.error_title is not None:
            payload["error_title"] = self.error_title
        if self.error_body is not None:
            payload["error_body"] = self.error_body
        if self.pairing is not None:
            payload["pairing"] = self.pairing.to_payload()
        return payload


@dataclass(frozen=True)
class _Attempt:
    """One connect attempt: the probe, then the sign-in the server needs."""

    result: DesktopProbeResult
    ticket_path: str | None = field(default=None, repr=False)
    pairing: PairingRequest | None = None
    failure: str | None = None


@dataclass(eq=False)
class _LaunchConnect:
    """The launch connect while its outcome may still replace the window's content."""

    target: DesktopTarget
    label: str | None
    open_session: SessionLink | None
    location: str | None
    started: float
    # Set once the connection screen shows the waiting state.
    waiting: bool = False
    stop: threading.Event = field(default_factory=threading.Event)


@dataclass(eq=False)
class _PairingWait:
    """A pairing waiting for approval; its outcome may still replace the window's content."""

    request: PairingRequest
    target: DesktopTarget
    label: str | None
    open_session: SessionLink | None
    location: str | None
    # The connection screen shows the code; failures are shown there too.
    on_screen: bool
    stop: threading.Event = field(default_factory=threading.Event)


@dataclass(eq=False)
class _Landing:
    """A window sign-in until the WebUI is loaded with the Desktop's parameters."""

    address: ServerAddress
    ticket_url: str = field(repr=False)
    app_url: str
    expires_at: float
    # Set once the window was sent to the ticket.
    redeeming: bool = False


# -- Remembered-servers operations -------------------------------------------


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


# -- Controller --------------------------------------------------------------


class ConnectionController:
    """Drive the launch connect, server switches and sign-in on one live pywebview window.

    The controller owns no window at construction time — the entrypoint creates
    the ``Window`` and hands it over via :meth:`attach_window` (the bridge is
    wired before the GUI loop exists, so the window arrives later). Every
    connect probes the target with the shared :func:`probe_target` and signs in
    through ``access`` when the server is in the access mode ``authenticated``:
    the launch connect (:meth:`launch_connect`, :meth:`auto_connect`) navigates
    the window itself, a connect the user starts (:meth:`prepare_connect`)
    returns the outcome for its calling document to apply. The entrypoint wires
    :meth:`page_loaded` to the window's ``loaded`` event.
    """

    def __init__(
        self,
        *,
        settings_file: Path | None = None,
        window: WindowProtocol | None = None,
        probe: Callable[[DesktopTarget], DesktopProbeResult] = probe_target,
        access: ServerAccess | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_file = settings_file
        self._window = window
        self._probe = probe
        self._access = access
        self._clock = clock
        # WebView2 reuses this persistent profile across Desktop restarts. A
        # launch-specific document URL prevents a previously cached SPA entry
        # from selecting an obsolete content-hashed asset bundle after an update.
        self._desktop_session_id = uuid4().hex
        # Notified with the base WebUI URL on every successful connect, so the
        # voice worker follows the window's active server (wired to the bridge in
        # the launcher — the controller stays decoupled from the voice stack).
        self._active_server_listener: Callable[[str], None] | None = None
        # What the window was last sent to. Read from the GUI thread (WebView2
        # permission requests) and the ``loaded`` event, so it has its own short
        # lock, never held while the window is driven.
        self._window_lock = threading.Lock()
        self._active_address: ServerAddress | None = None
        self._landing: _Landing | None = None
        # Set once a lost sign-in of the active server was handled; reset by the
        # next connect, so repeated refusals never loop the window.
        self._lost_handled = False
        # The launch connect or pairing until its outcome is applied. A connect
        # the user starts and close() end it under this lock, so a late outcome
        # never replaces what the user chose. Never taken on the GUI thread.
        self._lock = threading.Lock()
        self._pending: _LaunchConnect | _PairingWait | None = None
        self._worker: threading.Thread | None = None
        self._closed = False
        if access is not None:
            access.set_sign_in_lost_listener(self._sign_in_lost)

    def attach_window(self, window: WindowProtocol) -> None:
        """Bind the live pywebview window the controller navigates."""

        self._window = window

    def set_active_server_listener(self, listener: Callable[[str], None] | None) -> None:
        """Register a callback invoked with the base URL on each successful connect."""

        self._active_server_listener = listener

    def active_server_url(self) -> str | None:
        """Return the base URL of the last successfully prepared connection."""

        with self._window_lock:
            return self._active_address.url if self._active_address is not None else None

    def active_target(self) -> tuple[str, int] | None:
        """Return the ``(host, port)`` of the last successfully prepared connection.

        ``host`` is the ``https://`` address for an HTTPS server, as every
        ``(host, port)`` surface takes it.
        """

        with self._window_lock:
            address = self._active_address
        return (address.bridge_host, address.port) if address is not None else None

    # -- Remembered-servers surface (delegates to the module operations) -----

    def list_servers(self) -> list[ServerEntry]:
        """Return the remembered servers in stored order."""

        return list_servers(self._settings_file)

    def describe_servers(self) -> list[dict[str, Any]]:
        """Return the remembered servers for bridge callers.

        Each entry is :meth:`ServerEntry.to_bridge` plus ``active`` (the window's
        server) and ``signed_in`` (a device token is stored for it).
        """

        with self._window_lock:
            active = self._active_address.origin if self._active_address is not None else None
        return [
            {
                **entry.to_bridge(),
                "active": entry.address.origin == active,
                "signed_in": self._signed_in(entry.address),
            }
            for entry in self.list_servers()
        ]

    def add_server(self, host: str, port: Any, label: str | None = None) -> ServerEntry:
        """Remember a server, keyed by scheme, host and port."""

        return add_server(host, port, label, settings_file=self._settings_file)

    def remove_server(self, host: str, port: Any) -> bool:
        """Forget a remembered server and its stored sign-in."""

        removed = remove_server(host, port, settings_file=self._settings_file)
        try:
            address = parse_server_address(host, port)
        except ValueError:
            return removed
        self.forget_sign_in(address.bridge_host, address.port)
        return removed

    def forget_sign_in(self, host: str, port: Any) -> bool:
        """Forget the stored sign-in of a server; reports whether one was stored.

        The window does not pair again by itself for a server whose sign-in the
        user forgot; the next connect to it does.
        """

        address = parse_server_address(host, port)
        if self._access is None:
            return False
        with self._window_lock:
            if self._active_address is not None and self._active_address.origin == address.origin:
                self._lost_handled = True
        return self._access.forget(address)

    def resolve_last_used(self) -> ServerEntry | None:
        """Resolve the launch target (last-used, else first remembered, else None)."""

        return resolve_last_used(self._settings_file)

    # -- Navigation ----------------------------------------------------------

    def launch_connect(
        self,
        host: str,
        port: Any,
        label: str | None = None,
        *,
        open_session: SessionLink | None = None,
        location: str | None = None,
    ) -> DesktopProbeResult:
        """Connect the window at launch, waiting for a server that is unreachable.

        On a successful probe the server is remembered (carrying ``label``) and
        marked last-used, and the window loads the WebUI with the
        ``accessor=desktop`` marker (signed in first when the server needs it);
        ``open_session`` adds the ``open_agent`` / ``open_session`` parameters,
        so the WebUI opens that Session once loaded, and ``location`` (a URL
        fragment such as ``#settings``, restored after a Desktop restart) opens
        that place of the WebUI.

        While the server is unreachable (still starting, for example), the
        connection screen shows a waiting state for the target and a background
        thread starts another attempt about every :data:`LAUNCH_RETRY_SECONDS`
        for up to :data:`LAUNCH_WAIT_SECONDS`; a success then connects exactly as
        a first one would. A server that needs pairing shows its code on the
        connection screen and connects once the pairing is approved. Any other
        failure, and an unreachable server at the deadline, shows the connection
        screen with the failed target prefilled and an inline error, so the user
        corrects the target in place. A connect the user starts
        (:meth:`prepare_connect`) or :meth:`close` ends the launch connect: its
        outcome then never replaces the window's content.

        Returns the first attempt's probe result; ``server_unreachable`` means
        the launch connect goes on waiting.
        """

        launch = _LaunchConnect(
            target=self._build_target(host, port),
            label=label,
            open_session=open_session,
            location=location,
            started=self._clock(),
        )
        with self._lock:
            self._end_pending_locked()
            if not self._closed:
                self._pending = launch
        attempt = self._attempt(launch.target)
        with self._lock:
            if self._pending is not launch:
                return attempt.result
            if attempt.result.status != PROBE_SERVER_UNREACHABLE:
                self._pending = None
                self._finish_launch(launch, attempt)
                return attempt.result
            launch.waiting = True
            logger.info(
                "Desktop waiting for %s to answer (timeout=%ds)",
                launch.target.display,
                LAUNCH_WAIT_SECONDS,
            )
            self._show_connection_screen(probe_result=None, waiting_for=launch.target)
            self._start_worker(self._keep_trying, launch, "vbot-desktop-launch-wait")
        return attempt.result

    def auto_connect(self, *, open_session: SessionLink | None = None) -> DesktopProbeResult | None:
        """Launch entry point without a target: :meth:`launch_connect` to the last-used one.

        First run (nothing remembered) opens the connection screen with no
        inline error and returns ``None`` (a given ``open_session`` is dropped
        with it).
        """

        entry = self.resolve_last_used()
        if entry is None:
            if open_session is not None:
                logger.info(
                    "No server is remembered; Session %s of %s is not opened",
                    open_session.session,
                    open_session.agent,
                )
            self.show_connection_screen()
            return None
        return self.launch_connect(
            entry.address.bridge_host, entry.port, entry.label, open_session=open_session
        )

    def prepare_connect(
        self,
        host: str,
        port: Any,
        label: str | None = None,
        *,
        on_screen: bool = False,
    ) -> PreparedConnection:
        """Probe, sign in and persist a target the user chose, without replacing the document.

        One attempt, never waiting: successful attempts are remembered, marked
        last-used, and announced to the active-server listener exactly like
        :meth:`launch_connect`, but navigation is returned as data. Failed
        attempts carry the inline error copy. This is the bridge-safe seam:
        pywebview can deliver the payload to JavaScript before that document
        applies the outcome. A server that needs pairing returns its code
        (``pairing``, also as error copy for callers that know no pairing); the
        window connects once the pairing is approved. ``on_screen`` means the
        connection screen shows the code, so a pairing that fails is shown
        there. It ends the launch connect and a pending pairing first, so the
        user's choice wins.
        """

        self._end_pending("user_connect")
        target = self._build_target(host, port)
        attempt = self._attempt(target)
        prepared = self._settle(target, attempt, label)
        if attempt.pairing is not None and prepared.pairing is not None:
            with self._lock:
                self._wait_for_pairing_locked(
                    _PairingWait(attempt.pairing, target, label, None, None, on_screen)
                )
        return prepared

    def cancel_pairing(self) -> bool:
        """End a pairing waiting for approval; reports whether one was waiting.

        The window keeps what it shows; the connection screen hides the code itself.
        """

        with self._lock:
            if not isinstance(self._pending, _PairingWait):
                return False
            self._end_pending_locked()
        logger.info("Desktop pairing cancelled")
        return True

    def page_loaded(self) -> None:
        """Continue a window sign-in once the window loaded a page (pywebview ``loaded``).

        A connect the page started is signed in when the page it was sent to
        (:data:`SIGN_IN_PAGE`) has loaded: the window opens the ticket. Once the
        ticket redirected to the server's WebUI, the window loads it with the
        Desktop's parameters, and this Desktop's window Device is recorded. A
        ticket the server refused shows the connection screen. Any other page
        is left alone.
        """

        window = self._window
        if window is None:
            return
        try:
            url = window.get_current_url()
        except Exception:
            logger.debug("The Desktop window has no current URL", exc_info=True)
            return
        with self._window_lock:
            landing = self._landing
            if landing is None:
                return
            if self._clock() > landing.expires_at:
                self._landing = None
                return
            if not landing.redeeming:
                if url not in (None, SIGN_IN_PAGE):
                    return
                landing.redeeming = True
                step = "ticket"
            else:
                if url is None or url_origin(url) != landing.address.origin:
                    return
                self._landing = None
                step = "refused" if urlsplit(url).path.startswith(TICKET_PATH_PREFIX) else "app"
        if step == "ticket":
            window.load_url(landing.ticket_url)
        elif step == "refused":
            logger.warning("%s did not accept the window's sign-in", landing.address.display)
            target = DesktopTarget.for_address(landing.address)
            self._show_connection_screen(DesktopProbeResult(STATUS_SIGN_IN_FAILED, target))
        else:
            window.load_url(landing.app_url)
            access = self._access
            if access is not None:
                threading.Thread(
                    target=access.record_window_device,
                    args=(landing.address,),
                    name="vbot-desktop-window-device",
                    daemon=True,
                ).start()

    def close(self) -> None:
        """End the launch connect and a pending pairing for good; the window is closing.

        A background step that is still running stops without touching the window.
        """

        self._end_pending("closed")
        thread = self._worker
        if thread is not None and thread is not threading.current_thread():
            thread.join(_CLOSE_JOIN_SECONDS)

    def is_active_server(self, host: str, port: Any) -> bool:
        """Whether ``host:port`` (or an ``https://`` address) is the window's server."""

        try:
            origin = parse_server_address(host, port).origin
        except ValueError:
            return False
        with self._window_lock:
            return self._active_address is not None and self._active_address.origin == origin

    def show_connection_screen(self) -> None:
        """Render the connection screen with no inline error."""

        self._show_connection_screen(probe_result=None)

    # -- Internal ------------------------------------------------------------

    def _build_target(self, host: Any, port: Any) -> DesktopTarget:
        """Validate host/port into a :class:`DesktopTarget`, carrying any error.

        Invalid values are folded into ``configuration_error`` so the probe
        classifies them as an invalid target and the UI can keep the attempted
        values visible without a bridge exception.
        """

        try:
            address = parse_server_address(host, port)
        except ValueError as exc:
            return DesktopTarget(
                host=str(host or ""),
                port=_display_port(port),
                url="",
                configuration_error=str(exc),
            )
        return DesktopTarget.for_address(address)

    def _attempt(self, target: DesktopTarget, *, pair: bool = True) -> _Attempt:
        """Probe the target; sign in when its server is in the access mode ``authenticated``.

        Without a credential a pairing is started, unless ``pair`` is false (a
        sign-in right after an approved pairing must not pair again).
        """

        result = self._probe(target)
        address = target.address
        if (
            self._access is None
            or address is None
            or result.status != PROBE_WEBUI_AVAILABLE
            or result.access_mode != ACCESS_MODE_AUTHENTICATED
        ):
            return _Attempt(result)
        sign_in = self._access.sign_in(address)
        if sign_in.ticket_path is not None:
            return _Attempt(result, ticket_path=sign_in.ticket_path)
        if sign_in.error_code is not None:
            return _Attempt(result, failure=sign_in.error_code)
        if not sign_in.pairing_required:
            return _Attempt(result)
        if not pair:
            return _Attempt(result, failure=STATUS_SIGN_IN_FAILED)
        try:
            return _Attempt(result, pairing=self._access.start_pairing(address))
        except AccessFailure as exc:
            logger.warning("Desktop pairing with %s failed to start (%s)", address.display, exc)
            return _Attempt(result, failure=exc.error_code)

    def _keep_trying(self, launch: _LaunchConnect) -> None:
        """Try the launch target again until it answers, the deadline passes or it ends."""

        deadline = launch.started + LAUNCH_WAIT_SECONDS
        attempt_started = launch.started
        try:
            # An attempt can take the whole probe timeout; the next one still
            # starts about a retry interval after the previous one started.
            while not launch.stop.wait(
                max(0.0, attempt_started + LAUNCH_RETRY_SECONDS - self._clock())
            ):
                attempt_started = self._clock()
                attempt = self._attempt(launch.target)
                with self._lock:
                    if self._pending is not launch:
                        return
                    if (
                        attempt.result.status == PROBE_SERVER_UNREACHABLE
                        and self._clock() < deadline
                    ):
                        logger.debug(
                            "Desktop launch target %s is still unreachable",
                            launch.target.display,
                        )
                        continue
                    self._pending = None
                    self._finish_launch(launch, attempt)
                    return
        except Exception:
            logger.exception(
                "Desktop launch connect to %s stopped unexpectedly", launch.target.display
            )

    def _finish_launch(self, launch: _LaunchConnect, attempt: _Attempt) -> None:
        """Apply the launch connect's outcome to the window (under the lock)."""

        waited = self._clock() - launch.started if launch.waiting else None
        prepared = self._settle(
            launch.target,
            attempt,
            launch.label,
            open_session=launch.open_session,
            location=launch.location,
            waited=waited,
        )
        if attempt.pairing is not None and prepared.pairing is not None:
            self._wait_for_pairing_locked(
                _PairingWait(
                    attempt.pairing,
                    launch.target,
                    launch.label,
                    launch.open_session,
                    launch.location,
                    on_screen=True,
                )
            )
            self._show_connection_screen(probe_result=None, pairing=prepared.pairing)
            return
        self._apply(prepared)

    def _wait_for_pairing_locked(self, wait: _PairingWait) -> None:
        """Make ``wait`` the pending step and poll for its outcome in the background."""

        self._end_pending_locked()
        if self._closed:
            return
        self._pending = wait
        self._start_worker(self._await_pairing, wait, "vbot-desktop-pairing")

    def _await_pairing(self, wait: _PairingWait) -> None:
        """Wait for the pairing's outcome; connect once approved, else report it."""

        access = self._access
        if access is None:
            return
        try:
            outcome = access.await_pairing(wait.request, wait.stop)
            attempt = (
                self._attempt(wait.target, pair=False) if outcome.status == "approved" else None
            )
            with self._lock:
                if self._pending is not wait:
                    return
                self._pending = None
                if attempt is not None:
                    prepared = self._settle(
                        wait.target,
                        attempt,
                        wait.label,
                        open_session=wait.open_session,
                        location=wait.location,
                    )
                    if prepared.navigation_url is not None or wait.on_screen:
                        self._apply(prepared)
                    return
                failure = _PAIRING_FAILURES.get(outcome.status, outcome.error_code)
                if failure is not None and wait.on_screen:
                    self._show_connection_screen(DesktopProbeResult(failure, wait.target))
        except Exception:
            logger.exception("Desktop pairing with %s stopped unexpectedly", wait.target.display)

    def _apply(self, prepared: PreparedConnection) -> None:
        """Navigate the window to a prepared outcome, or show its failure."""

        if prepared.ticket_url is not None:
            with self._window_lock:
                if self._landing is not None:
                    self._landing.redeeming = True
            self._navigate_url(prepared.ticket_url)
        elif prepared.navigation_url is not None:
            self._navigate_url(prepared.navigation_url)
        else:
            self._show_connection_screen(replace(prepared.result, status=prepared.status))

    def _settle(
        self,
        target: DesktopTarget,
        attempt: _Attempt,
        label: str | None,
        *,
        open_session: SessionLink | None = None,
        location: str | None = None,
        waited: float | None = None,
    ) -> PreparedConnection:
        """Remember and announce a successful connect, or describe the failure inline."""

        result = attempt.result
        waited_note = "" if waited is None else f" (waited={waited:.0f}s)"
        address = target.address
        if result.status != PROBE_WEBUI_AVAILABLE or address is None:
            logger.warning(
                "Desktop connection to %s failed (%s)%s",
                target.display,
                result.status,
                waited_note,
            )
            return _failed(result, result.status)

        self._remember(address, label)
        if attempt.failure is not None:
            logger.warning(
                "Desktop sign-in to %s failed (%s)%s", address.display, attempt.failure, waited_note
            )
            return _failed(result, attempt.failure)
        if attempt.pairing is not None:
            view = PairingView(code=attempt.pairing.code, server=address.display, target=target)
            logger.info("Desktop waiting for %s to approve its pairing", address.display)
            return PreparedConnection(
                result=result,
                failure=STATUS_PAIRING_REQUIRED,
                error_title=view.title,
                error_body=view.body,
                pairing=view,
            )

        app_url = _with_accessor_param(address.url, self._desktop_session_id)
        if open_session is not None:
            app_url = _with_session_link(app_url, open_session)
        if location is not None:
            app_url = f"{app_url}{location}"
        ticket_url = (
            None
            if attempt.ticket_path is None
            else f"{address.url.rstrip('/')}{attempt.ticket_path}"
        )
        logger.info(
            "Desktop connecting to %s%s%s",
            address.display,
            " (signing in)" if ticket_url is not None else "",
            waited_note,
        )
        landing = (
            None
            if ticket_url is None
            else _Landing(address, ticket_url, app_url, self._clock() + SIGN_IN_LANDING_SECONDS)
        )
        self._activate(address, landing)
        if ticket_url is None:
            return PreparedConnection(result=result, navigation_url=app_url)
        return PreparedConnection(result=result, navigation_url=SIGN_IN_PAGE, ticket_url=ticket_url)

    def _remember(self, address: ServerAddress, label: str | None) -> None:
        """Remember a server that answered; a connect without a name keeps the saved one."""

        if label is None:
            key = ServerEntry.for_address(address).key
            label = next((entry.label for entry in self.list_servers() if entry.key == key), None)
        self.add_server(address.bridge_host, address.port, label)

    def _activate(self, address: ServerAddress, landing: _Landing | None) -> None:
        """Make ``address`` the window's server: last-used, active, and announced."""

        select_server(address.bridge_host, address.port, settings_file=self._settings_file)
        with self._window_lock:
            self._active_address = address
            self._landing = landing
            self._lost_handled = False
        self._notify_active_server(address.url)

    def _sign_in_lost(self, address: ServerAddress) -> None:
        """A server refused this Desktop's stored sign-in: sign the window in again, once.

        Only for the window's server, and only once until the next connect, so
        a server that keeps refusing never loops the window.
        """

        with self._window_lock:
            active = self._active_address
            if self._lost_handled or active is None or active.origin != address.origin:
                return
            self._lost_handled = True
        with self._lock:
            if self._closed:
                return
        logger.info(
            "%s refused this Desktop's sign-in; connecting the window again", address.display
        )
        threading.Thread(
            target=self._reconnect,
            args=(address,),
            name="vbot-desktop-sign-in",
            daemon=True,
        ).start()

    def _reconnect(self, address: ServerAddress) -> None:
        try:
            self.launch_connect(address.bridge_host, address.port)
        except Exception:
            logger.exception("Desktop could not connect the window to %s again", address.display)

    def _end_pending(self, reason: str) -> None:
        with self._lock:
            if reason == "closed":
                self._closed = True
            pending = self._end_pending_locked()
        if isinstance(pending, _LaunchConnect) and pending.waiting:
            logger.info(
                "Desktop stopped waiting for %s (reason=%s)", pending.target.display, reason
            )
        elif isinstance(pending, _PairingWait):
            logger.info(
                "Desktop stopped waiting for the pairing with %s (reason=%s)",
                pending.target.display,
                reason,
            )

    def _end_pending_locked(self) -> _LaunchConnect | _PairingWait | None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.stop.set()
        return pending

    def _start_worker(self, target: Callable[[Any], None], step: Any, name: str) -> None:
        self._worker = threading.Thread(target=target, args=(step,), name=name, daemon=True)
        self._worker.start()

    def _navigate_url(self, url: str) -> None:
        window = self._require_window()
        window.load_url(url)

    def _notify_active_server(self, url: str) -> None:
        """Tell the active-server listener (the voice worker) about a new server.

        The listener rebuilds a background worker, so a failure there must never
        break the window navigation that already succeeded — it is logged and
        swallowed. Passed the plain base URL, not the ``accessor=desktop`` window
        URL, since the worker composes its own ``/api/...`` paths from it.
        """

        if self._active_server_listener is None:
            return
        try:
            self._active_server_listener(url)
        except Exception:
            logger.warning("Active-server listener failed for %s", url, exc_info=True)

    def _show_connection_screen(
        self,
        probe_result: DesktopProbeResult | None,
        *,
        waiting_for: DesktopTarget | None = None,
        pairing: PairingView | None = None,
    ) -> None:
        window = self._require_window()
        with self._window_lock:
            # The screen replaces the page a sign-in was waiting for.
            self._landing = None
        servers = self.list_servers()
        window.load_html(
            build_connection_html(
                servers=servers,
                probe_result=probe_result,
                waiting_for=waiting_for,
                pairing=pairing,
                signed_in=[
                    entry.address.origin for entry in servers if self._signed_in(entry.address)
                ],
            )
        )

    def _signed_in(self, address: ServerAddress) -> bool:
        return self._access is not None and self._access.signed_in(address)

    def _require_window(self) -> WindowProtocol:
        if self._window is None:
            raise RuntimeError("ConnectionController has no window attached")
        return self._window


def _failed(result: DesktopProbeResult, status: str) -> PreparedConnection:
    """A failed connect with the inline error copy of ``status``."""

    error_title, error_body = connection_error_copy(status)
    return PreparedConnection(
        result=result,
        error_title=error_title,
        error_body=error_body,
        failure=None if status == result.status else status,
    )


def _with_accessor_param(url: str, desktop_session_id: str) -> str:
    """Mark a Desktop WebUI URL and make its document cache key launch-specific."""

    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}{ACCESSOR_QUERY_PARAM}&{DESKTOP_SESSION_QUERY_PARAM}={desktop_session_id}"
    )


def _with_session_link(url: str, link: SessionLink) -> str:
    """Ask the WebUI behind ``url`` to open one Session once it has loaded."""

    separator = "&" if "?" in url else "?"
    query = urlencode(
        {OPEN_AGENT_QUERY_PARAM: link.agent, OPEN_SESSION_QUERY_PARAM: link.session},
        quote_via=quote,
    )
    return f"{url}{separator}{query}"


def _display_port(value: Any) -> int:
    """Return a safe form-prefill port for an invalid target."""

    try:
        return int(value)
    except TypeError, ValueError:
        return 0
