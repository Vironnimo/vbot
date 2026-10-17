"""Signing the Desktop in to vBot servers in the access mode ``authenticated``.

:class:`ServerAccess` owns the Desktop's credentials and the HTTP calls of its
sign-in; the connection controller decides what the window shows. Per server
origin it resolves one credential, in this order:

1. the local access token of the installation's own server, when the Desktop
   was started with ``--server-data-dir``: read from
   ``<data-dir>/runtime/local-access-<port>.token`` at each use and sent only
   to ``http://`` and a loopback IP literal the server records there as one it
   listens on (never ``localhost``: a name may resolve to an address another
   program holds);
2. for an ``https://`` server, the device token stored when this Desktop was
   paired (:mod:`desktop.credentials`);
3. none: an ``https://`` server needs pairing (:meth:`ServerAccess.start_pairing`,
   :meth:`ServerAccess.await_pairing`); any other server refuses this Desktop
   (``https_required``). Over plain ``http://`` a device token or a pairing
   could reach whatever listens at that address, so neither is ever sent there.

The window signs in through a login ticket (:meth:`ServerAccess.sign_in`) that
lands it on the WebUI page the Desktop asks for. The ticket's redemption gives
the WebView a browser Device and its cookie; a WebView that is signed in already
keeps its Device. The server revokes such a ticket-created Device together with
the Device that issued the ticket, so forgetting this Desktop's sign-in signs its
window out too. Native requests (speech, Voice) send the credential as
``Authorization: Bearer`` (:meth:`ServerAccess.header`); a refused one
(:meth:`ServerAccess.rejected`, a 401) is re-read (local access token) or
dropped (device token), and a dropped sign-in is reported to the listener so the
window can pair again. No other answer, ``devices_unavailable`` (503) included,
drops a credential.

Tokens, tickets, poll secrets and pairing codes never appear in logs.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import platform
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from desktop._http import shared_ssl_context
from desktop.credentials import (
    DEVICE_TOKEN_PATTERN,
    CredentialStoreError,
    DeviceTokenStore,
    StoredSignIn,
)
from desktop.server_address import ServerAddress, parse_server_address

logger = logging.getLogger("vbot.desktop.access")

ERROR_ACCESS_DENIED = "access_denied"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_DEVICES_UNAVAILABLE = "devices_unavailable"
ERROR_SIGN_IN_FAILED = "sign_in_failed"
ERROR_SERVER_UNREACHABLE = "server_unreachable"
#: A server reached over plain ``http://`` that this Desktop cannot sign in to.
ERROR_HTTPS_REQUIRED = "https_required"
_PASSED_ON_ERRORS = frozenset({ERROR_ACCESS_DENIED, ERROR_RATE_LIMITED, ERROR_DEVICES_UNAVAILABLE})

TICKET_PATH_PREFIX = "/auth/ticket/"
PAIR_START_PATH = "/api/auth/pair/start"
PAIR_POLL_PATH = "/api/auth/pair/poll"
RPC_PATH = "/api/rpc"
LOCAL_TOKEN_DIRECTORY = "runtime"
DEVICE_KIND = "desktop"
DEVICE_NAME_MAX_CHARS = 80
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_POLL_WAIT_SECONDS = 60.0
_LOCAL_TOKEN_PATTERN = re.compile(r"vbl_[A-Za-z0-9_-]{43}")
_LOCAL_TOKEN_MAX_BYTES = 1024
_TICKET_PATH_PATTERN = re.compile(r"/auth/ticket/[A-Za-z0-9_-]{16,256}")
_PAIRING_CODE_PATTERN = re.compile(r"[0-9A-Z]{4}-[0-9A-Z]{4}")
_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
#: Characters a device name must not carry (the server refuses them).
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_HOST_ACTOR = "host"

PairingStatus = Literal["approved", "denied", "expired", "cancelled", "failed"]


class AccessFailure(Exception):  # noqa: N818 - names the outcome
    """A sign-in step the server refused or could not answer, with a stable ``error_code``."""

    def __init__(self, error_code: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class SignIn:
    """How the window gets into a server in the access mode ``authenticated``.

    ``ticket_path`` opens the WebUI signed in; ``pairing_required`` means the
    Desktop has no valid credential; ``error_code`` names a failure. All empty:
    the server no longer needs a sign-in.
    """

    ticket_path: str | None = field(default=None, repr=False)
    pairing_required: bool = False
    error_code: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class PairingRequest:
    """A started pairing: the code the user approves, and how to ask for the result."""

    address: ServerAddress
    code: str
    request_id: str = field(repr=False)
    poll_secret: str = field(repr=False)
    poll_interval: float
    expires_at: float


@dataclass(frozen=True)
class PairingOutcome:
    """How a pairing ended; ``failed`` carries the ``error_code``."""

    status: PairingStatus
    error_code: str | None = None


@dataclass(frozen=True)
class _Credential:
    token: str = field(repr=False)
    # Who the server records as the principal: the host or this Desktop's device.
    actor: str

    @property
    def local(self) -> bool:
        return self.actor == _HOST_ACTOR


class _Refused:
    """The credential was refused (401): try the next one."""


_REFUSED = _Refused()


def _wait(event: threading.Event, seconds: float) -> bool:
    return event.wait(seconds)


class ServerAccess:
    """Credentials and sign-in for every server the Desktop connects to; thread-safe.

    ``server_data_dir`` is the data directory of the installation's own server
    (``--server-data-dir``). ``transport``, ``clock`` and ``wait`` replace the
    network, the monotonic clock and the pairing poll wait in tests.
    """

    def __init__(
        self,
        *,
        store: DeviceTokenStore,
        server_data_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] = _wait,
        computer_name: str | None = None,
    ) -> None:
        self._store = store
        self._server_data_dir = server_data_dir
        self._transport = transport
        self._clock = clock
        self._wait = wait
        computer = _computer_name() if computer_name is None else computer_name
        self.device_name = _device_name("vBot Desktop", computer)
        self.window_name = _device_name("vBot Desktop window", computer)
        self._sign_in_lost: Callable[[ServerAddress], None] | None = None

    def is_installation_server(self, server_url: str) -> bool:
        """Whether ``server_url`` is the installation's own server, signed in to locally."""
        address = _address(server_url)
        return address is not None and self._local_token(address) is not None

    def set_sign_in_lost_listener(self, listener: Callable[[ServerAddress], None] | None) -> None:
        """Register who learns that a server refused this Desktop's stored sign-in."""
        self._sign_in_lost = listener

    def signed_in(self, address: ServerAddress) -> bool:
        """Whether a device token is stored for the ``https://`` server (pairing done)."""
        if address.scheme != "https":
            return False
        stored = self._stored(address)
        return stored is not None and stored.token is not None

    # -- Window sign-in ----------------------------------------------------------

    def sign_in(self, address: ServerAddress, landing: str | None = None) -> SignIn:
        """Get a login ticket for the window, trying each credential in turn.

        ``landing`` is the WebUI page (path and query) the ticket redirects to;
        without it the server's start page. A refused local access token is
        read again once (the server writes a new one at each start); a refused
        device token is dropped. Without any valid credential the answer is
        ``pairing_required`` for an ``https://`` server and ``https_required``
        for any other.
        """
        params: dict[str, Any] = {"name": self.window_name}
        if landing is not None:
            params["next"] = landing
        local = self._local_token(address)
        if local is not None:
            answer = self._login_ticket(address, _Credential(local, _HOST_ACTOR), params)
            if not isinstance(answer, _Refused):
                return answer
            fresh = self._local_token(address)
            if fresh is not None and fresh != local:
                answer = self._login_ticket(address, _Credential(fresh, _HOST_ACTOR), params)
                if not isinstance(answer, _Refused):
                    return answer
            logger.warning("%s refused the local access token", address.origin)
        if address.scheme != "https":
            return SignIn(error_code=ERROR_HTTPS_REQUIRED)
        stored = self._stored(address)
        if stored is not None and stored.token is not None and stored.device_id is not None:
            answer = self._login_ticket(
                address, _Credential(stored.token, stored.device_id), params
            )
            if not isinstance(answer, _Refused):
                return answer
            self._drop(address, stored.token)
        return SignIn(pairing_required=True)

    # -- Pairing -----------------------------------------------------------------

    def start_pairing(self, address: ServerAddress, name: str | None = None) -> PairingRequest:
        """Ask the server for a pairing code; raises :class:`AccessFailure`.

        ``name`` is the device name the user confirmed (:meth:`pairing_name`);
        without one, :attr:`device_name`. Only an ``https://`` server is asked
        (``https_required`` otherwise).
        """
        if address.scheme != "https":
            raise AccessFailure(
                ERROR_HTTPS_REQUIRED, f"Pairing over plain http:// with {address.origin} refused"
            )
        name = self.pairing_name(name)
        try:
            response = self._post(address, PAIR_START_PATH, {"kind": DEVICE_KIND, "name": name})
        except httpx.RequestError as exc:
            raise AccessFailure(
                ERROR_SERVER_UNREACHABLE, f"Pairing could not reach {address.origin}"
            ) from exc
        payload = _json_object(response)
        if response.status_code != 200 or payload is None:
            code, retry_after = _failure(response)
            raise AccessFailure(
                _known_error(code),
                f"{address.origin} refused to start pairing (HTTP {response.status_code}, {code})",
                retry_after=retry_after,
            )
        request_id = payload.get("request_id")
        poll_secret = payload.get("poll_secret")
        code = payload.get("code")
        expires_in = _seconds(payload.get("expires_in"))
        poll_interval = _seconds(payload.get("poll_interval"))
        if not (
            isinstance(request_id, str)
            and _SECRET_PATTERN.fullmatch(request_id)
            and isinstance(poll_secret, str)
            and _SECRET_PATTERN.fullmatch(poll_secret)
            and isinstance(code, str)
            and _PAIRING_CODE_PATTERN.fullmatch(code)
            and expires_in is not None
            and poll_interval is not None
        ):
            raise AccessFailure(
                ERROR_SIGN_IN_FAILED, f"{address.origin} answered pairing with an invalid response"
            )
        logger.info("Desktop pairing started with %s", address.origin)
        return PairingRequest(
            address=address,
            code=code,
            request_id=request_id,
            poll_secret=poll_secret,
            poll_interval=max(1.0, min(poll_interval, MAX_POLL_WAIT_SECONDS)),
            expires_at=self._clock() + expires_in,
        )

    def pairing_name(self, name: Any = None) -> str:
        """The device name a pairing sends: ``name`` within the server's rules, else the default."""
        cleaned = _device_name(name, "") if isinstance(name, str) else ""
        return cleaned or self.device_name

    def await_pairing(self, request: PairingRequest, stop: threading.Event) -> PairingOutcome:
        """Poll until the pairing is decided, expires, fails, or ``stop`` is set.

        Polls every ``poll_interval``; a 429 waits its ``Retry-After``, and a
        poll without an answer (network, proxy) is repeated until the code
        expires. An approval stores the device token before it returns, even
        when ``stop`` was set meanwhile.
        """
        address = request.address
        interval = request.poll_interval
        while True:
            remaining = request.expires_at - self._clock()
            if remaining <= 0:
                return self._pairing_ended(address, PairingOutcome("expired"))
            if self._wait(stop, min(interval, remaining)):
                return PairingOutcome("cancelled")
            if self._clock() >= request.expires_at:
                return self._pairing_ended(address, PairingOutcome("expired"))
            interval = request.poll_interval
            try:
                response = self._post(
                    address,
                    PAIR_POLL_PATH,
                    {"request_id": request.request_id, "poll_secret": request.poll_secret},
                )
            except httpx.RequestError as exc:
                logger.info("Pairing poll to %s failed (%s)", address.origin, type(exc).__name__)
                continue
            payload = _json_object(response)
            if response.status_code == 200 and payload is not None:
                outcome = self._poll_answer(address, payload)
                if outcome is not None:
                    return self._pairing_ended(address, outcome)
                continue
            code, retry_after = _failure(response)
            if code == ERROR_RATE_LIMITED:
                interval = min(max(retry_after or interval, interval), MAX_POLL_WAIT_SECONDS)
                continue
            if code is None:
                # A proxy or a restarting server: the request may still be decided.
                logger.info("Pairing poll to %s got HTTP %s", address.origin, response.status_code)
                continue
            return self._pairing_ended(address, PairingOutcome("failed", _known_error(code)))

    # -- Native requests (ServerAuthorization) ------------------------------------

    def header(self, server_url: str) -> str | None:
        """Return the ``Authorization`` value for a request to ``server_url``, if any."""
        address = _address(server_url)
        credential = self._credential(address) if address is not None else None
        return None if credential is None else f"Bearer {credential.token}"

    def rejected(self, server_url: str, header: str | None) -> bool:
        """Handle a 401 for a request sent with ``header``; ``True`` to retry with a new one.

        A refused local access token is read again; a refused device token is
        dropped and the listener learns that the sign-in is lost, as it does for
        a request that had no credential at all.
        """
        address = _address(server_url)
        if address is None:
            return False
        token = header.removeprefix("Bearer ") if header is not None else None
        if token is not None and _LOCAL_TOKEN_PATTERN.fullmatch(token):
            fresh = self._local_token(address)
            return fresh is not None and fresh != token
        if token is not None and not self._drop(address, token):
            # A newer sign-in replaced the refused token meanwhile.
            current = self._credential(address)
            return current is not None and current.token != token
        listener = self._sign_in_lost
        if listener is not None:
            try:
                listener(address)
            except Exception:
                logger.warning("Sign-in lost listener failed for %s", address.origin, exc_info=True)
        return False

    # -- Forgetting ---------------------------------------------------------------

    def forget(self, address: ServerAddress) -> bool:
        """Forget the stored sign-in of a server; reports whether a device token was stored.

        The local copy goes at once. The server is then asked, in the
        background and best effort, to revoke this Desktop's Device; the
        browser Device of the window it signed in goes with it. A sign-in an
        earlier version stored for a plain ``http://`` server is only deleted:
        its token is never sent there.
        """
        stored = self._store.remove(address.origin)
        if stored is None or stored.token is None or stored.device_id is None:
            return False
        if address.scheme != "https":
            logger.info("Deleted the Desktop's plain-http sign-in for %s", address.origin)
            return True
        logger.info("Forgot the Desktop sign-in for %s", address.origin)
        credential = _Credential(stored.token, stored.device_id)
        threading.Thread(
            target=self._sign_out,
            args=(address, credential),
            name="vbot-desktop-sign-out",
            daemon=True,
        ).start()
        return True

    # -- Internals ----------------------------------------------------------------

    def _sign_out(self, address: ServerAddress, credential: _Credential) -> None:
        if self._call(address, credential, "devices.logout", {}) is not None:
            logger.info("Signed this Desktop out of %s", address.origin)

    def _poll_answer(
        self, address: ServerAddress, payload: dict[str, Any]
    ) -> PairingOutcome | None:
        status = payload.get("status")
        if status == "pending":
            return None
        if status in {"denied", "expired"}:
            return PairingOutcome(status)
        if status != "approved":
            return PairingOutcome("failed", ERROR_SIGN_IN_FAILED)
        token = payload.get("token")
        device = payload.get("device")
        device_id = device.get("id") if isinstance(device, dict) else None
        match = DEVICE_TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
        if match is None or match.group(1) != device_id or not isinstance(token, str):
            logger.warning("%s approved the pairing with an invalid device token", address.origin)
            return PairingOutcome("failed", ERROR_SIGN_IN_FAILED)
        try:
            self._store.save_device(address.origin, token, match.group(1))
        except CredentialStoreError, ValueError:
            return PairingOutcome("failed", ERROR_SIGN_IN_FAILED)
        logger.info("Desktop paired with %s as device %s", address.origin, match.group(1))
        return PairingOutcome("approved")

    @staticmethod
    def _pairing_ended(address: ServerAddress, outcome: PairingOutcome) -> PairingOutcome:
        if outcome.status != "approved":
            logger.info(
                "Desktop pairing with %s ended (%s%s)",
                address.origin,
                outcome.status,
                f", {outcome.error_code}" if outcome.error_code else "",
            )
        return outcome

    def _login_ticket(
        self, address: ServerAddress, credential: _Credential, params: dict[str, Any]
    ) -> SignIn | _Refused:
        try:
            response = self._post(
                address,
                RPC_PATH,
                {"method": "auth.login_ticket", "params": params},
                token=credential.token,
            )
        except httpx.RequestError:
            return SignIn(error_code=ERROR_SERVER_UNREACHABLE)
        code, retry_after = _failure(response)
        if response.status_code == 401 or code == "auth_required":
            return _REFUSED
        if code == "auth_disabled":
            # The server left the access mode ``authenticated`` meanwhile.
            return SignIn()
        if code is not None or response.status_code != 200:
            logger.warning(
                "%s refused the Desktop sign-in (HTTP %s, %s)",
                address.origin,
                response.status_code,
                code,
            )
            return SignIn(error_code=_known_error(code), retry_after=retry_after)
        payload = _json_object(response)
        result = payload.get("result") if payload is not None else None
        path = result.get("path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not _TICKET_PATH_PATTERN.fullmatch(path):
            logger.warning("%s answered the sign-in with an invalid ticket", address.origin)
            return SignIn(error_code=ERROR_SIGN_IN_FAILED)
        return SignIn(ticket_path=path)

    def _call(
        self, address: ServerAddress, credential: _Credential, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """One best-effort RPC with ``credential``; its result, or ``None`` after logging."""
        try:
            response = self._post(
                address, RPC_PATH, {"method": method, "params": params}, token=credential.token
            )
        except httpx.RequestError as exc:
            logger.warning("%s on %s failed (%s)", method, address.origin, type(exc).__name__)
            return None
        payload = _json_object(response)
        result = payload.get("result") if payload is not None else None
        if response.status_code != 200 or payload is None or payload.get("ok") is not True:
            code, _retry_after = _failure(response)
            logger.warning(
                "%s on %s was refused (HTTP %s, %s)",
                method,
                address.origin,
                response.status_code,
                code,
            )
            return None
        return result if isinstance(result, dict) else None

    def _post(
        self,
        address: ServerAddress,
        path: str,
        body: dict[str, Any],
        *,
        token: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        with httpx.Client(
            transport=self._transport,
            trust_env=False,
            verify=shared_ssl_context(),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
        ) as client:
            return client.post(f"{address.url.rstrip('/')}{path}", json=body, headers=headers)

    def _credential(self, address: ServerAddress) -> _Credential | None:
        local = self._local_token(address)
        if local is not None:
            return _Credential(local, _HOST_ACTOR)
        if address.scheme != "https":
            return None
        stored = self._stored(address)
        if stored is None or stored.token is None or stored.device_id is None:
            return None
        return _Credential(stored.token, stored.device_id)

    def _stored(self, address: ServerAddress) -> StoredSignIn | None:
        return self._store.get(address.origin)

    def _drop(self, address: ServerAddress, token: str) -> bool:
        try:
            dropped = self._store.drop_device(address.origin, token)
        except CredentialStoreError:
            return False
        if dropped:
            logger.warning(
                "%s refused this Desktop's sign-in; it has to pair again", address.origin
            )
        return dropped

    def _local_token(self, address: ServerAddress) -> str | None:
        """The installation's local access token for ``address``, read now.

        Mirrors ``core.access.local_token`` (the Desktop imports nothing of the
        server): the file is ``{"token": ..., "loopback": [...]}``, and the
        token goes only to ``http://`` and one of those loopback IP literals
        on the file's port.
        """
        if self._server_data_dir is None or address.scheme != "http":
            return None
        literal = _loopback_literal(address.host)
        if literal is None:
            return None
        path = self._server_data_dir / LOCAL_TOKEN_DIRECTORY / f"local-access-{address.port}.token"
        try:
            with path.open("rb") as handle:
                raw = handle.read(_LOCAL_TOKEN_MAX_BYTES + 1)
        except OSError:
            return None
        if len(raw) > _LOCAL_TOKEN_MAX_BYTES:
            return None
        try:
            document = json.loads(raw.decode("ascii"))
        except UnicodeError, ValueError:
            return None
        if not isinstance(document, dict):
            return None
        token, loopback = document.get("token"), document.get("loopback")
        if not isinstance(token, str) or not _LOCAL_TOKEN_PATTERN.fullmatch(token):
            return None
        if not isinstance(loopback, list):
            return None
        listening = {_loopback_literal(item) for item in loopback if isinstance(item, str)}
        return token if literal in listening else None


def _loopback_literal(host: str) -> str | None:
    """The compressed form of a loopback IP literal; ``None`` for names and other addresses."""
    try:
        address = ipaddress.ip_address(host.strip().removeprefix("[").removesuffix("]"))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and (
        address.scope_id is not None or address.ipv4_mapped is not None
    ):
        return None
    return address.compressed if address.is_loopback else None


def _address(server_url: str) -> ServerAddress | None:
    try:
        return parse_server_address(server_url.strip().rstrip("/"), None)
    except ValueError:
        return None


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _failure(response: httpx.Response) -> tuple[str | None, float | None]:
    """The error code of an error envelope (HTTP or RPC) and its retry delay, if any."""
    payload = _json_object(response)
    error = payload.get("error") if payload is not None and payload.get("ok") is False else None
    code = error.get("code") if isinstance(error, dict) else None
    retry_after = _seconds(response.headers.get("Retry-After"))
    data = error.get("data") if isinstance(error, dict) else None
    if retry_after is None and isinstance(data, dict):
        retry_after = _seconds(data.get("retry_after"))
    return (code if isinstance(code, str) else None), retry_after


def _known_error(code: str | None) -> str:
    return code if code in _PASSED_ON_ERRORS else ERROR_SIGN_IN_FAILED


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except TypeError, ValueError:
        return None
    return seconds if 0 < seconds < float("inf") else None


def _computer_name() -> str:
    try:
        return platform.node()
    except OSError:
        return ""


def _device_name(base: str, computer: str) -> str:
    """A device name within the server's rules: one line, at most 80 characters."""
    base, computer = _one_line(base), _one_line(computer)
    name = f"{base} on {computer}" if computer else base
    return name[:DEVICE_NAME_MAX_CHARS].rstrip()


def _one_line(text: str) -> str:
    """``text`` without control or formatting characters and with single spaces."""
    return " ".join(
        "".join(
            " " if unicodedata.category(character) in _HIDDEN_CATEGORIES else character
            for character in text
        ).split()
    )
