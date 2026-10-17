"""How the CLI, the TUI and the tray reach a vBot server.

One owner for what every connection to a server target needs: its origin, TLS
verification for that origin, and the credential on every HTTP request and
WebSocket. Callers build their clients from :func:`http_options` and
:func:`websocket_target`; nothing else attaches credentials.

Which credential a request carries:

- A loopback target gets the local access token of the server on that port in
  the target's data directory (``core.access.local_token``). It is read at the
  first request and again after a 401, because every server start writes a new
  one. Without a token file, the stored Device sign-in of the origin is used
  (a tunnel to another computer's server).
- Any other target gets the stored Device sign-in of its exact origin
  (``cli/sign_ins.py``), which ``vbot devices pair`` creates.
- A request to another origin never carries either; clients built here do not
  follow redirects.

In ``local`` and ``lan`` a server ignores the credential, so sending it is
harmless. Tokens are never logged or printed.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Literal, override
from urllib.parse import urlsplit

import httpx

from cli._server_target import ServerInstance, is_loopback_target, server_origin
from cli.sign_ins import SignInStore, SignInStoreError
from core.access.local_token import local_token_path, read_local_access_token
from core.utils.tls import ssl_context_for

#: The access refusals a server sends before routing (``access.md``).
AUTH_REQUIRED = "auth_required"
ACCESS_DENIED = "access_denied"
#: WebSocket close codes of the same refusals.
WEBSOCKET_REFUSALS = {4401: AUTH_REQUIRED, 4403: ACCESS_DENIED}


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


def credential_for(instance: ServerInstance) -> Credential | None:
    """The credential requests to ``instance`` carry now (see the module docstring)."""

    if is_loopback_target(instance):
        token = read_local_access_token(instance.data_dir, instance.port)
        if token is not None:
            return Credential("local", token)
    origin = server_origin(instance.url)
    if origin is None:
        return None
    try:
        sign_in = SignInStore().sign_in(origin)
    except SignInStoreError:
        # An unreadable file signs in nothing; pairing reports the file itself.
        return None
    return None if sign_in is None else Credential("device", sign_in.token)


def authorization_headers(instance: ServerInstance) -> dict[str, str]:
    """The ``Authorization`` header for one connection to ``instance``, read fresh."""

    credential = credential_for(instance)
    return {} if credential is None else {"Authorization": credential.header}


class ServerAuth(httpx.Auth):
    """Attach the target's credential; after a 401, re-read the local token once.

    The credential is looked up at the first request and kept for later ones on
    the same client. A 401 from a loopback target re-reads the local access token
    and repeats the request once when the token changed (the server restarted).
    Requests to any other origin than the target's go out without a credential.
    """

    def __init__(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._origin = server_origin(instance.url)
        self._credential: Credential | None = None
        self._looked_up = False

    @override
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        if self._origin is None or server_origin(str(request.url)) != self._origin:
            yield request
            return
        if not self._looked_up:
            self._credential, self._looked_up = credential_for(self._instance), True
        sent = self._credential
        if sent is not None:
            request.headers["Authorization"] = sent.header
        response = yield request
        if response.status_code != httpx.codes.UNAUTHORIZED:
            return
        fresh = self._fresh_local_token(sent)
        if fresh is None:
            return
        self._credential = fresh
        request.headers["Authorization"] = fresh.header
        yield request

    def _fresh_local_token(self, sent: Credential | None) -> Credential | None:
        """The local token of a loopback target when it differs from the one sent."""

        if not is_loopback_target(self._instance):
            return None
        token = read_local_access_token(self._instance.data_dir, self._instance.port)
        if token is None or (sent is not None and sent.token == token):
            return None
        return Credential("local", token)


def http_options(instance: ServerInstance, *, sign_in: bool = True) -> dict[str, Any]:
    """Keyword arguments for an ``httpx`` client or request to ``instance``.

    Ambient proxy settings and ``.netrc`` are ignored: RPC bodies carry secrets.
    A plain ``http://`` target gets a TLS context without CA parsing
    (``core.utils.tls``). ``sign_in=False`` sends no credential, for the pairing
    endpoints, where a stale token would count as a failed sign-in.
    """

    options: dict[str, Any] = {"trust_env": False, "verify": ssl_context_for(instance.url)}
    if sign_in:
        options["auth"] = ServerAuth(instance)
    return options


def websocket_target(instance: ServerInstance, path: str) -> tuple[str, dict[str, Any]]:
    """The ``ws(s)://`` URL of a server path and the ``websockets.connect`` options it needs.

    The options carry the credential, read fresh for this connection, TLS
    verification for ``wss://`` and no proxy.
    """

    scheme, _, rest = instance.url.partition("://")
    secure = scheme == "https"
    url = f"{'wss' if secure else 'ws'}://{rest}{path}"
    options: dict[str, Any] = {"proxy": None, "additional_headers": authorization_headers(instance)}
    if secure:
        options["ssl"] = ssl_context_for(instance.url)
    return url, options


def access_refusal(instance: ServerInstance, code: str, *, command: bool = True) -> str | None:
    """What an ``auth_required`` or ``access_denied`` refusal means for this target, and the fix.

    ``command=False`` leaves out the sign-in command, for output that prints it
    as ``Next:``. ``None`` for any other code.
    """

    origin = server_origin(instance.url) or instance.url
    pair = f" Sign in with: vbot devices pair {origin}" if command else ""
    if code == AUTH_REQUIRED:
        if is_loopback_target(instance):
            path = local_token_path(instance.data_dir, instance.port)
            if read_local_access_token(instance.data_dir, instance.port) is None:
                return (
                    f"The server on port {instance.port} admits only signed-in devices, and "
                    f"{path} does not exist: that server keeps its data in another directory. "
                    "Repeat the command with that server's --data-dir."
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
