"""Connection outcomes and the state of pending native navigation attempts."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from desktop.access import PairingRequest
from desktop.connection_screen import PairingOffer, PairingView, connection_error_copy
from desktop.probe import DesktopProbeResult, DesktopTarget
from desktop.server_address import ServerAddress
from desktop.window import SessionLink

SIGN_IN_PAGE = "about:blank"
"""A page-started sign-in lands here before the native controller redeems its ticket."""


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
class PreparedConnection:
    """A connect outcome whose visible navigation has not happened yet.

    pywebview API calls return through a Promise callback stored in the current
    document. Replacing that document before the Python method returns destroys
    the callback and makes pywebview raise while delivering the result. This
    value lets bridge callers receive the outcome first and navigate from
    JavaScript afterwards, while native callers apply the same outcome
    themselves.

    ``failure`` names a sign-in outcome beside the probe ``result`` (a pairing
    offered or waiting for approval included). ``ticket_url`` signs the window
    in and never leaves the Desktop: a bridge caller gets :data:`SIGN_IN_PAGE`
    as ``navigation_url``.
    """

    result: DesktopProbeResult
    navigation_url: str | None = None
    error_title: str | None = None
    error_body: str | None = None
    failure: str | None = None
    offer: PairingOffer | None = None
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
        if self.offer is not None:
            payload["offer"] = self.offer.to_payload()
        if self.pairing is not None:
            payload["pairing"] = self.pairing.to_payload()
        return payload


@dataclass(frozen=True)
class _Attempt:
    """One connect attempt: the probe, then the sign-in the server needs.

    ``pair`` means the server needs this Desktop paired; the user decides.
    """

    result: DesktopProbeResult
    ticket_path: str | None = field(default=None, repr=False)
    pair: bool = False
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
class _Offer:
    """The server the connection screen offers to pair with, and where to go afterwards."""

    view: PairingOffer
    target: DesktopTarget
    label: str | None
    open_session: SessionLink | None
    location: str | None
    # Until when a page that started the connect may still reach SIGN_IN_PAGE,
    # where the window then shows the connection screen with the offer.
    screen_due: float | None = None


@dataclass(eq=False)
class _Landing:
    """A window sign-in until its ticket has landed the window on the WebUI.

    The ticket redirects to ``app_url``'s page; ``app_url`` is loaded only when
    the window landed anywhere else on the server.
    """

    address: ServerAddress
    ticket_url: str = field(repr=False)
    app_url: str
    expires_at: float
    # Set once the window was sent to the ticket.
    redeeming: bool = False


def _failed(result: DesktopProbeResult, status: str) -> PreparedConnection:
    """A failed connect with the inline error copy of ``status``."""

    error_title, error_body = connection_error_copy(status)
    return PreparedConnection(
        result=result,
        error_title=error_title,
        error_body=error_body,
        failure=None if status == result.status else status,
    )
