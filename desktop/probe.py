"""Desktop server targets and bounded HTTP health/WebUI probing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from desktop import _http
from desktop.server_address import Scheme, ServerAddress, is_loopback_host, parse_server_address

DEFAULT_HOST = "127.0.0.1"
# Same as core.utils.config.DEFAULT_PORT; the Desktop imports nothing from the server side.
DEFAULT_PORT = 22332
PROBE_TIMEOUT_SECONDS = 2.0
# The kernel completes a loopback handshake even while the server's Event Loop is
# busy; a refused one is what takes long (Windows retries the SYN for about 2 s).
LOOPBACK_CONNECT_TIMEOUT_SECONDS = 0.2
PROBE_WEBUI_AVAILABLE = "webui_available"
PROBE_WEBUI_UNAVAILABLE = "webui_unavailable"
PROBE_SERVER_UNREACHABLE = "server_unreachable"
PROBE_NOT_VBOT_SERVER = "not_vbot_server"
PROBE_INVALID_TARGET = "invalid_target"
# The server refused this computer at its access guard (wrong network, or
# plain HTTP from another computer in the access mode ``authenticated``).
PROBE_ACCESS_DENIED = "access_denied"
AUTH_STATUS_PATH = "/api/auth/status"
ACCESS_MODE_AUTHENTICATED = "authenticated"
_ACCESS_MODES = frozenset({"local", "lan", ACCESS_MODE_AUTHENTICATED})
ACCESSOR_QUERY_PARAM = "accessor=desktop"


class HttpResponse(Protocol):
    """Subset of an HTTP response used by Desktop probing."""

    status_code: int

    def json(self) -> Any:
        """Return the decoded JSON body."""


class HttpGet(Protocol):
    """Synchronous HTTP GET callable used by Desktop probing."""

    def __call__(
        self, url: str, *, timeout: httpx.Timeout | float, trust_env: bool
    ) -> HttpResponse:
        """Fetch a URL with a bounded timeout."""


@dataclass(frozen=True)
class DesktopTarget:
    """Resolved Desktop server target.

    ``host`` is the plain host name (also for an HTTPS server); ``url`` its base
    URL. A target that could not be validated carries the attempted values and
    its ``configuration_error``.
    """

    host: str
    port: int
    url: str
    configuration_error: str | None = None
    scheme: Scheme = "http"

    @classmethod
    def for_address(cls, address: ServerAddress) -> DesktopTarget:
        """The target of a validated address."""
        return cls(address.host, address.port, address.url, scheme=address.scheme)

    @property
    def address(self) -> ServerAddress | None:
        """The validated address, or ``None`` for a target with a configuration error."""
        if self.configuration_error is not None:
            return None
        return ServerAddress(self.scheme, self.host, self.port)

    @property
    def display(self) -> str:
        """How the user sees the target: ``host:port`` or the HTTPS address."""
        address = self.address
        return address.display if address is not None else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class DesktopProbeResult:
    """Result of probing a target vBot server and its WebUI root.

    ``access_mode`` is the server's access mode (``local``, ``lan`` or
    ``authenticated``) when its WebUI is available and it reported one; a
    server from before access modes, or one whose status could not be read,
    leaves it ``None`` and needs no sign-in.
    """

    status: str
    target: DesktopTarget
    access_mode: str | None = None


def build_target_url(host: str, port: int) -> str:
    """Build the WebUI root URL of a target (``host`` may be an ``https://`` URL)."""

    return parse_server_address(host, port).url


def probe_target(
    target: DesktopTarget,
    *,
    get: HttpGet | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DesktopProbeResult:
    """Classify the target using one client and connection for both requests.

    A loopback target gets ``LOOPBACK_CONNECT_TIMEOUT_SECONDS`` to connect, so a
    server that is not listening yet costs a launch wait attempt 0.2 s, not 2 s.
    """

    if target.configuration_error is not None:
        return DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)

    budget: httpx.Timeout | float = timeout
    if is_loopback_host(target.host):
        budget = httpx.Timeout(timeout, connect=min(timeout, LOOPBACK_CONNECT_TIMEOUT_SECONDS))
    if get is not None:
        return _probe_target(target, lambda url: get(url, timeout=budget, trust_env=False))
    with httpx.Client(timeout=budget, trust_env=False, verify=_http.shared_ssl_context()) as client:
        return _probe_target(target, client.get)


def _probe_target(target: DesktopTarget, get: Callable[[str], HttpResponse]) -> DesktopProbeResult:
    base_url = target.url.rstrip("/")
    try:
        health_response = get(f"{base_url}/health")
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_SERVER_UNREACHABLE, target=target)

    if health_response.status_code == 403 and _error_code(health_response) == "access_denied":
        return DesktopProbeResult(status=PROBE_ACCESS_DENIED, target=target)
    if health_response.status_code != 200 or not _is_vbot_health_response(health_response):
        return DesktopProbeResult(status=PROBE_NOT_VBOT_SERVER, target=target)

    try:
        webui_response = get(target.url)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)

    if not 200 <= webui_response.status_code <= 399:
        return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)
    return DesktopProbeResult(
        status=PROBE_WEBUI_AVAILABLE,
        target=target,
        access_mode=_access_mode(get, f"{base_url}{AUTH_STATUS_PATH}"),
    )


def _access_mode(get: Callable[[str], HttpResponse], status_url: str) -> str | None:
    """Return the access mode the server reports, or ``None`` when it reports none.

    Asked without a credential: the mode is public. A server from before access
    modes answers 404; an unreadable answer is treated the same, so the WebUI's
    own sign-in gate stays the fallback.
    """
    try:
        response = get(status_url)
    except httpx.RequestError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    mode = payload.get("mode") if isinstance(payload, dict) else None
    return mode if mode in _ACCESS_MODES else None


def _error_code(response: HttpResponse) -> str | None:
    """Return the ``error.code`` of a JSON error envelope, if the response is one."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _is_vbot_health_response(response: HttpResponse) -> bool:
    """Return whether /health matches the vBot server identity contract."""

    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(payload == {"status": "ok"})
