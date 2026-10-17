"""How the CLI, the TUI and the tray reach a vBot server.

One owner for what every connection to a server target needs: its origin, TLS
verification for that origin, and the credential on every HTTP request and
WebSocket. Callers build their clients from :func:`http_options` and
:func:`websocket_target`; nothing else attaches credentials.

Which credential a request carries:

- A plain ``http://`` target on a loopback address the server on that port
  listens on, named by its IP literal (``http://127.0.0.1:22332``, never
  ``localhost``), gets that server's local access token from the target's data
  directory (``core.access.local_token``). It is read at the first request and
  again after a 401, because every server start writes a new one.
- An ``https://`` target gets the stored Device sign-in of its exact origin
  (``cli/sign_ins.py``), which ``vbot devices pair`` creates. A caller can name
  another place that keeps Device sign-ins (``device_sign_in``): the tray of a
  Desktop Client signs in with the Desktop's own Device.
- Any other target carries no credential: over plain ``http://`` a token could
  reach whatever listens at that address, on this computer another user's
  program included. A tunnel to another computer's server needs that server's
  ``https://`` address.
- A request to another origin never carries either; clients built here do not
  follow redirects.

In ``local`` and ``lan`` a server ignores the credential, so sending it is
harmless. A server whose device store is unavailable answers 503
``devices_unavailable`` (WebSocket 4503): the credential stays as it is.
Tokens are never logged or printed.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Literal, override
from urllib.parse import urlsplit

import httpx

from cli._server_target import ServerInstance, is_loopback_target, server_origin
from cli.sign_ins import SignInStore, SignInStoreError
from core.access.local_token import local_token_path, read_local_access
from core.utils.tls import ssl_context_for

#: The access refusals a server sends before routing (``access.md``).
AUTH_REQUIRED = "auth_required"
ACCESS_DENIED = "access_denied"
DEVICES_UNAVAILABLE = "devices_unavailable"
#: WebSocket close codes of the same refusals.
WEBSOCKET_REFUSALS = {4401: AUTH_REQUIRED, 4403: ACCESS_DENIED, 4503: DEVICES_UNAVAILABLE}

#: Looks up the Device token kept for a server origin; ``None`` without one.
DeviceSignIn = Callable[[str], str | None]


@dataclass(frozen=True)
class Credential:
    """The token one request carries: the local access token or a Device token."""

    kind: Literal["local", "device"]
    token: str

    @override
    def __repr__(self) -> str:
        return f"Credential(kind={self.kind!r})"

    @property
    def header(self) -> str:
        return f"Bearer {self.token}"


def credential_for(
    instance: ServerInstance, *, device_sign_in: DeviceSignIn | None = None
) -> Credential | None:
    """The credential requests to ``instance`` carry now (see the module docstring).

    ``device_sign_in`` replaces the CLI's own sign-ins as the source of the
    Device token; the local access token of a loopback target still comes first.
    """

    if is_loopback_target(instance):
        local = read_local_access(instance.data_dir, instance.port)
        if local is not None and local.admits(instance.url):
            return Credential("local", local.token)
    origin = server_origin(instance.url)
    if origin is None or urlsplit(origin).scheme != "https":
        return None
    token = (device_sign_in or cli_sign_in)(origin)
    return None if token is None else Credential("device", token)


def cli_sign_in(origin: str) -> str | None:
    """The Device token ``vbot devices pair`` stored for ``origin``."""

    try:
        sign_in = SignInStore().sign_in(origin)
    except SignInStoreError:
        # An unreadable file signs in nothing; pairing reports the file itself.
        return None
    return None if sign_in is None else sign_in.token


def authorization_headers(
    instance: ServerInstance, *, device_sign_in: DeviceSignIn | None = None
) -> dict[str, str]:
    """The ``Authorization`` header for one connection to ``instance``, read fresh."""

    credential = credential_for(instance, device_sign_in=device_sign_in)
    return {} if credential is None else {"Authorization": credential.header}


class ServerAuth(httpx.Auth):
    """Attach the target's credential; after a 401, look it up again once.

    The credential is looked up at the first request and kept for later ones on
    the same client. A 401 looks it up again and repeats the request once when
    it changed: a restarted server wrote a new local access token, or the
    accessor signed in again meanwhile. Requests to any other origin than the
    target's go out without a credential.
    """

    def __init__(
        self, instance: ServerInstance, *, device_sign_in: DeviceSignIn | None = None
    ) -> None:
        self._instance = instance
        self._device_sign_in = device_sign_in
        self._origin = server_origin(instance.url)
        self._credential: Credential | None = None
        self._looked_up = False

    @override
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        if self._origin is None or server_origin(str(request.url)) != self._origin:
            yield request
            return
        if not self._looked_up:
            self._credential, self._looked_up = self._lookup(), True
        sent = self._credential
        if sent is not None:
            request.headers["Authorization"] = sent.header
        response = yield request
        if response.status_code != httpx.codes.UNAUTHORIZED:
            return
        fresh = self._lookup()
        if fresh is None or fresh == sent:
            return
        self._credential = fresh
        request.headers["Authorization"] = fresh.header
        yield request

    def _lookup(self) -> Credential | None:
        return credential_for(self._instance, device_sign_in=self._device_sign_in)


def http_options(
    instance: ServerInstance,
    *,
    sign_in: bool = True,
    device_sign_in: DeviceSignIn | None = None,
) -> dict[str, Any]:
    """Keyword arguments for an ``httpx`` client or request to ``instance``.

    Ambient proxy settings and ``.netrc`` are ignored: RPC bodies carry secrets.
    A plain ``http://`` target gets a TLS context without CA parsing
    (``core.utils.tls``). ``sign_in=False`` sends no credential, for the pairing
    endpoints, where a stale token would count as a failed sign-in.
    """

    options: dict[str, Any] = {"trust_env": False, "verify": ssl_context_for(instance.url)}
    if sign_in:
        options["auth"] = ServerAuth(instance, device_sign_in=device_sign_in)
    return options


def websocket_target(
    instance: ServerInstance, path: str, *, device_sign_in: DeviceSignIn | None = None
) -> tuple[str, dict[str, Any]]:
    """The ``ws(s)://`` URL of a server path and the ``websockets.connect`` options it needs.

    The options carry the credential, read fresh for this connection, TLS
    verification for ``wss://`` and no proxy.
    """

    scheme, _, rest = instance.url.partition("://")
    secure = scheme == "https"
    url = f"{'wss' if secure else 'ws'}://{rest}{path}"
    options: dict[str, Any] = {
        "proxy": None,
        "additional_headers": authorization_headers(instance, device_sign_in=device_sign_in),
    }
    if secure:
        options["ssl"] = ssl_context_for(instance.url)
    return url, options


def access_refusal(instance: ServerInstance, code: str, *, command: bool = True) -> str | None:
    """What an access refusal means for this target, and the fix.

    Codes: ``auth_required``, ``access_denied`` and ``devices_unavailable``.
    ``command=False`` leaves out the sign-in command, for output that prints it
    as ``Next:``. ``None`` for any other code.
    """

    origin = server_origin(instance.url) or instance.url
    if code == DEVICES_UNAVAILABLE:
        return (
            f"The server at {origin} cannot check signed-in devices right now: its device "
            "store is unavailable. This computer's sign-in stays; the server log on the "
            "server's computer names the cause."
        )
    pair = f" Sign in with: vbot devices pair {origin}" if command else ""
    if code == AUTH_REQUIRED:
        if is_loopback_target(instance):
            path = local_token_path(instance.data_dir, instance.port)
            local = read_local_access(instance.data_dir, instance.port)
            if local is None:
                return (
                    f"The server on port {instance.port} admits only signed-in devices, and "
                    f"{path} does not exist: that server keeps its data in another directory. "
                    "Repeat the command with that server's --data-dir."
                )
            if not local.admits(instance.url):
                address = next(iter(local.loopback), "127.0.0.1")
                url = f"http://{f'[{address}]' if ':' in address else address}:{instance.port}"
                return (
                    f"The server on port {instance.port} admits only signed-in devices. This "
                    "computer signs in with the server's local access token only at an address "
                    f"the server listens on, never by a host name: repeat the command with "
                    f"--url {url}."
                )
            return (
                f"The server on port {instance.port} refused the local access token in {path}: "
                "another server wrote it. Repeat the command with the --data-dir of the server "
                f"that listens on port {instance.port}."
            )
        try:
            signed_in = SignInStore().sign_in(origin) is not None
        except SignInStoreError as exc:
            return f"The server at {origin} admits only signed-in devices. {exc}."
        if signed_in:
            return (
                f"The server at {origin} refused this computer's sign-in: its Device was revoked "
                f"or expired and has to pair again.{pair}"
            )
        return (
            f"The server at {origin} admits only signed-in devices, and this computer has no "
            f"sign-in for it.{pair}"
        )
    if code == ACCESS_DENIED:
        text = (
            f"The server at {origin} refuses this connection in its access mode; signing in "
            "does not change that."
        )
        if urlsplit(origin).scheme == "http" and not is_loopback_target(instance):
            text += (
                " A server that requires sign-in accepts plain http:// only from its own "
                "computer; connect to its https:// address."
            )
        return text + " On the server's computer, 'vbot application access show' shows its mode."
    return None
