"""Sign this computer's CLI in to a server that requires login, and out again.

``vbot devices pair <url>`` runs the pairing flow of
``access/accessor-contracts.md``: it starts a request as a ``cli`` Device named
``CLI on <computer name>``, prints the code the user approves on a signed-in
device or on the server's computer, polls until the request is decided and
stores the Device token (``cli/sign_ins.py``). The started request is stored
too, so that running the command again continues waiting for the same code
instead of starting another one. In a terminal it waits until the code
expires; otherwise it returns after ``PAIR_WAIT_SECONDS`` with the command that
continues.

``vbot devices logout <url>`` revokes this computer's Device on the server
(``devices.logout``) and deletes the stored sign-in, also when the server
cannot be reached.

Pairing needs the server's ``https://`` address: a Device token is only ever
sent there (``cli/server_client.py``). Over plain ``http://`` to another
computer nothing is sent; on this computer's loopback the status is asked first,
because the server's local access token may already sign this computer in.

Both talk to the pairing endpoints without a credential: a refused token
would count as a failed sign-in. Tokens and poll secrets are never printed, and
server-supplied text reaches the terminal only escaped (``inline_text``); a
code that is not ``XXXX-XXXX`` ends the command as a foreign answer.
"""

from __future__ import annotations

import math
import re
import socket
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from cli._progress import operation_progress
from cli._server_target import CommandResult, RpcFailure, ServerInstance, is_loopback_target
from cli.formatting import inline_text
from cli.sign_ins import PendingPairing, SignIn, SignInStore, SignInStoreError
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    import httpx

#: How long a run without a terminal waits for the approval before it returns.
PAIR_WAIT_SECONDS = 50.0
_TIMEOUT_SECONDS = 10.0
_NAME_LIMIT = 80
_STATUS_PATH = "/api/auth/status"
_START_PATH = "/api/auth/pair/start"
_POLL_PATH = "/api/auth/pair/poll"
_RPC_PATH = "/api/rpc"
#: The display form the server hands out; anything else is no vBot answer.
_PAIRING_CODE = re.compile(r"[0-9A-Z]{4}-[0-9A-Z]{4}")
#: Characters a device name must not carry (the server refuses them).
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_APPROVE_WHERE = (
    "Approve it only in vBot at {origin}: in its WebUI on a signed-in device (Settings > "
    "Access & devices > Add device), or on that server's computer with: vbot devices approve "
    "{code}. Never enter it for another address: whoever gets it approved there signs in as "
    "this CLI."
)


class _StopError(Exception):
    """End the command with ``result``."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__(result.message)
        self.result = result


@dataclass
class _Pairing:
    instance: ServerInstance
    client: httpx.Client
    store: SignInStore
    clock: Callable[[], float]
    sleep: Callable[[float], None]

    @property
    def origin(self) -> str:
        return self.instance.url

    def run(self, *, name: str, interactive: bool) -> CommandResult:
        status = self._status()
        principal, device = status.get("principal"), status.get("device")
        if principal == "device" and isinstance(device, Mapping):
            return self._done(
                f'Already signed in to {self.origin} as "{_shown(device.get("name"))}" '
                f"(device {_shown(device.get('id'))}); nothing was changed."
            )
        if status.get("mode") != "authenticated":
            return self._done(
                f"The server at {self.origin} runs in the access mode "
                f"'{_shown(status.get('mode_label') or status.get('mode'))}' and admits this "
                "computer without sign-in; nothing was changed."
            )
        if principal == "host":
            return self._done(
                f"This computer signs in to {self.origin} with the server's local access "
                "token; it needs no pairing. Nothing was changed."
            )
        if urlsplit(self.origin).scheme != "https":
            raise _StopError(self._done(self._plain_http_refusal(), ok=False))
        pending = self.store.pending(self.origin)
        resumed = (
            pending is not None
            and pending.expires_at - self.clock() > 1
            and _PAIRING_CODE.fullmatch(pending.code) is not None
        )
        if pending is None or not resumed:
            if self.store.sign_in(self.origin) is not None:
                print(
                    f"Note: The server refused the stored sign-in for {self.origin} (revoked or "
                    "expired); pairing again.",
                    file=sys.stderr,
                )
            pending = self._start(name)
        self._announce(pending, resumed=resumed)
        deadline = pending.expires_at
        if not interactive:
            deadline = min(deadline, self.clock() + PAIR_WAIT_SECONDS)
        try:
            return self._wait(pending, deadline)
        except KeyboardInterrupt:
            return self._done(
                f"Stopped waiting. The code {pending.code} stays valid for "
                f"{_minutes(pending.expires_at - self.clock())}; run vbot devices pair "
                f"{self.origin} again to keep waiting for it.",
                ok=False,
            )

    # Requests -----------------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        from cli.server_client import ServerAuth

        response = self._send("GET", _STATUS_PATH, auth=ServerAuth(self.instance))
        if response.status_code != 200:
            raise _StopError(self._refused(response, "GET " + _STATUS_PATH, "Nothing was changed."))
        body = _json(response)
        if body is None or not isinstance(body.get("mode"), str):
            raise _StopError(self._done(self._not_vbot(response), ok=False))
        return body

    def _start(self, name: str) -> PendingPairing:
        response = self._send("POST", _START_PATH, json={"kind": "cli", "name": name})
        if response.status_code != 200:
            raise _StopError(self._refused(response, "POST " + _START_PATH, "Nothing was stored."))
        body = _json(response) or {}
        request_id, code, secret = (body.get(key) for key in ("request_id", "code", "poll_secret"))
        expires_in, interval = body.get("expires_in"), body.get("poll_interval")
        if not (
            all(isinstance(value, str) and value for value in (request_id, code, secret))
            and _PAIRING_CODE.fullmatch(str(code))
            and isinstance(expires_in, (int, float))
            and isinstance(interval, (int, float))
        ):
            raise _StopError(self._done(self._not_vbot(response), ok=False))
        pending = PendingPairing(
            origin=self.origin,
            request_id=str(request_id),
            poll_secret=str(secret),
            code=str(code),
            name=name,
            expires_at=self.clock() + float(expires_in),
            poll_interval=max(1.0, float(interval)),
        )
        self.store.save_pending(pending)
        return pending

    def _announce(self, pending: PendingPairing, *, resumed: bool) -> None:
        first = (
            f"Continuing the pairing with {self.origin}; the code waits for approval."
            if resumed
            else f'Started pairing with {self.origin} as "{pending.name}".'
        )
        lines = [
            first,
            f"Code: {pending.code}",
            _APPROVE_WHERE.format(origin=self.origin, code=pending.code),
            f"The code is valid for {_minutes(pending.expires_at - self.clock())}.",
        ]
        print("\n".join(lines), flush=True)
        operation_progress(f"Waiting for the approval of {pending.code}")

    def _wait(self, pending: PendingPairing, deadline: float) -> CommandResult:
        problem = ""
        while True:
            now = self.clock()
            if now >= pending.expires_at:
                self.store.drop_pending(self.origin)
                return self._expired(pending)
            if now >= deadline:
                return self._not_finished(pending, problem)
            pause = pending.poll_interval
            try:
                response = self.client.post(
                    _POLL_PATH,
                    json={"request_id": pending.request_id, "poll_secret": pending.poll_secret},
                )
            except _request_errors() as exc:
                problem = f"The last check could not reach the server ({type(exc).__name__})."
            else:
                body = _json(response) or {}
                result = body.get("status") if response.status_code == 200 else None
                if result == "approved":
                    return self._approved(pending, body)
                if result in {"denied", "expired"}:
                    self.store.drop_pending(self.origin)
                    if result == "expired":
                        return self._expired(pending)
                    return self._done(
                        f"The pairing request {pending.code} was denied; nothing was stored.",
                        ok=False,
                    )
                refusal = 400 <= response.status_code < 500 and response.status_code != 429
                if refusal and _error(response).get("code"):
                    # Sign-in turned off, or this connection refused: waiting cannot help.
                    if response.status_code == 409:
                        self.store.drop_pending(self.origin)
                    raise _StopError(
                        self._refused(response, "POST " + _POLL_PATH, "Nothing was stored.")
                    )
                if response.status_code == 429:
                    pause = max(pause, _retry_after(response) or 0.0)
                    problem = "The server asked to check less often."
                elif result != "pending":
                    problem = (
                        f"The last check got an unexpected answer (HTTP {response.status_code})."
                    )
                else:
                    problem = ""
            self.sleep(max(0.0, min(pause, deadline - self.clock())))

    def _approved(self, pending: PendingPairing, body: Mapping[str, Any]) -> CommandResult:
        device = body.get("device")
        device = device if isinstance(device, Mapping) else {}
        device_id, token = _shown(device.get("id")), body.get("token")
        named = device.get("name")
        name = named if isinstance(named, str) else pending.name
        if not isinstance(token, str):
            return self._done(
                f"The server approved device {device_id} but sent no token; nothing was stored. "
                "Revoke that device in the WebUI and pair again.",
                ok=False,
            )
        try:
            self.store.save_sign_in(
                SignIn(self.origin, token, str(device.get("id") or ""), name, utc_now_timestamp())
            )
        except SignInStoreError as exc:
            return self._done(
                f"The server approved device {device_id}, but this computer could not store its "
                f"token: {exc}. Revoke device {device_id} in the WebUI (Settings > Access & "
                "devices), then pair again.",
                ok=False,
            )
        return self._done(
            f'Signed in to {self.origin} as "{_shown(name)}" (device {device_id}). Commands '
            f"reach this server with --url {self.origin}.\n"
            f"Sign out with: vbot devices logout {self.origin}"
        )

    def _expired(self, pending: PendingPairing) -> CommandResult:
        return self._done(
            f"The code {pending.code} expired before it was approved; nothing was stored. Run "
            f"vbot devices pair {self.origin} again for a new code.",
            ok=False,
        )

    def _not_finished(self, pending: PendingPairing, problem: str) -> CommandResult:
        lines = [
            f"Not finished: the code {pending.code} still waits for approval; it stays valid "
            f"for {_minutes(pending.expires_at - self.clock())}.",
            *([problem] if problem else []),
            f"Run vbot devices pair {self.origin} again to keep waiting for the same code.",
        ]
        return CommandResult(
            ok=True,
            message="\n".join(lines),
            instance=self.instance,
            attention=(f"the code {pending.code} waits for approval",),
        )

    # Helpers ------------------------------------------------------------------

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, path, **kwargs)
        except _request_errors() as exc:
            raise _StopError(
                self._done(
                    f"Could not reach {self.origin} ({type(exc).__name__}); nothing was changed. "
                    "Check the address and the network connection.",
                    ok=False,
                    failure=RpcFailure(f"{method} {path}", _request_state(exc)),
                )
            ) from exc

    def _refused(self, response: httpx.Response, request: str, effect: str) -> CommandResult:
        from cli.server_client import access_refusal

        error = _error(response)
        code = error.get("code") if isinstance(error.get("code"), str) else None
        if code is None:
            return self._done(self._not_vbot(response), ok=False)
        explanation = access_refusal(self.instance, code)
        if code == "rate_limited":
            wait = _retry_after(response)
            explanation = (
                "The server refuses more pairing attempts from this computer"
                + (f" for {_minutes(wait)}" if wait else " for a few minutes")
                + ": too many attempts in a short time."
            )
        elif code == "auth_disabled":
            explanation = f"The server at {self.origin} does not use sign-in right now."
        message = f"{explanation or _shown(error.get('message'))} {effect}"
        return self._done(
            message,
            ok=False,
            failure=RpcFailure(request, "responded", code, response.status_code),
        )

    def _plain_http_refusal(self) -> str:
        from cli.server_client import AUTH_REQUIRED, access_refusal

        reason = access_refusal(self.instance, AUTH_REQUIRED) or ""
        return (
            f"{reason} Pairing needs an https:// address: over plain http:// whatever listens at "
            f"{self.origin} would receive the Device token. If this address is a tunnel to "
            "another computer's server, pair with that server's https:// address or run vbot "
            "on that computer. Nothing was changed."
        ).strip()

    def _not_vbot(self, response: httpx.Response) -> str:
        return (
            f"The server at {self.origin} did not answer like a vBot server (HTTP "
            f"{response.status_code}); nothing was changed. Check the address."
        )

    def _done(
        self, message: str, *, ok: bool = True, failure: RpcFailure | None = None
    ) -> CommandResult:
        return CommandResult(ok=ok, message=message, instance=self.instance, failure=failure)


def devices_pair(
    instance: ServerInstance,
    *,
    interactive: bool | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    computer_name: str | None = None,
) -> CommandResult:
    """Sign this computer's CLI in to the server ``instance`` names (module docstring).

    ``interactive`` defaults to whether stdin and stdout are terminals;
    ``transport``, ``clock`` and ``sleep`` replace the network and time in tests.
    """

    if urlsplit(instance.url).scheme != "https" and not is_loopback_target(instance):
        return CommandResult(
            ok=False,
            message=(
                f"Pairing needs the server's https:// address: over {instance.url} the Device "
                "token would cross the network unencrypted. Nothing was sent."
            ),
            instance=instance,
        )
    if interactive is None:
        interactive = all(
            stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)
        )
    name = device_name("CLI", socket.gethostname() if computer_name is None else computer_name)
    try:
        with _client(instance, transport) as client:
            pairing = _Pairing(instance, client, SignInStore(), clock, sleep)
            return pairing.run(name=name, interactive=interactive)
    except _StopError as stop:
        return stop.result
    except SignInStoreError as exc:
        return CommandResult(ok=False, message=f"{exc}. Nothing was changed.", instance=instance)


def devices_logout(
    instance: ServerInstance, *, transport: httpx.BaseTransport | None = None
) -> CommandResult:
    """Revoke this computer's Device on the server and delete its stored sign-in."""

    store = SignInStore()
    origin = instance.url
    try:
        sign_in = store.sign_in(origin)
        if sign_in is None:
            removed = store.forget(origin)
            message = (
                f"Deleted this computer's unusable sign-in and pairing for {origin}."
                if removed
                else f"This computer has no sign-in for {origin}; nothing was changed."
            )
            return CommandResult(ok=True, message=message, instance=instance)
        outcome, revoked = _revoke_own_device(instance, sign_in, transport)
        store.forget(origin)
    except SignInStoreError as exc:
        return CommandResult(ok=False, message=f"{exc}. Nothing was changed.", instance=instance)
    device = f'"{_shown(sign_in.device_name)}" (device {_shown(sign_in.device_id)})'
    if revoked == "revoked":
        message = (
            f"Signed out of {origin}: revoked {device} there and deleted this computer's sign-in."
        )
    elif revoked == "gone":
        message = (
            f"Signed out of {origin}: the server no longer accepted {device}, and this computer's "
            "sign-in is deleted."
        )
    else:
        message = (
            f"Deleted this computer's sign-in for {origin}, but {outcome}, so {device} stays "
            "listed there until it is revoked in the WebUI (Settings > Access & devices) or "
            "unused for 90 days."
        )
    return CommandResult(
        ok=True,
        message=message,
        instance=instance,
        attention=() if revoked != "kept" else ("the Device stays listed on the server",),
    )


def device_name(label: str, computer: str) -> str:
    """``<label> on <computer>`` without hidden characters, within the server's 80 characters."""

    computer = " ".join(
        "".join(
            " " if unicodedata.category(character) in _HIDDEN_CATEGORIES else character
            for character in computer
        ).split()
    )
    return f"{label} on {computer}"[:_NAME_LIMIT].rstrip() if computer else label


def _revoke_own_device(
    instance: ServerInstance, sign_in: SignIn, transport: httpx.BaseTransport | None
) -> tuple[str, Literal["revoked", "gone", "kept"]]:
    """Call ``devices.logout`` as the Device itself; returns (reason, outcome)."""

    try:
        with _client(instance, transport) as client:
            response = client.post(
                _RPC_PATH,
                json={"method": "devices.logout", "params": {}},
                headers={"Authorization": f"Bearer {sign_in.token}"},
            )
    except _request_errors() as exc:
        return f"the server could not be reached ({type(exc).__name__})", "kept"
    if response.status_code == 401:
        return "", "gone"
    body = _json(response) or {}
    if response.status_code == 200 and body.get("ok") is True:
        return "", "revoked"
    error = _error(response)
    reason = _shown(error.get("code") or f"HTTP {response.status_code}")
    return f"the server refused to revoke it ({reason})", "kept"


def _client(instance: ServerInstance, transport: httpx.BaseTransport | None) -> httpx.Client:
    import httpx

    from cli.server_client import http_options

    return httpx.Client(
        base_url=instance.url,
        timeout=_TIMEOUT_SECONDS,
        transport=transport,
        **http_options(instance, sign_in=False),
    )


def _request_errors() -> type[Exception]:
    import httpx

    return httpx.RequestError


def _request_state(exc: Exception) -> Literal["not_sent", "unknown"]:
    import httpx

    not_sent = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    return "not_sent" if isinstance(exc, not_sent) else "unknown"


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error(response: httpx.Response) -> Mapping[str, Any]:
    """The ``error`` object of a refusal envelope; empty when there is none."""

    error = (_json(response) or {}).get("error")
    return error if isinstance(error, Mapping) else {}


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(1.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return None


def _minutes(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _shown(value: object) -> str:
    """Server-supplied text on one line, its hidden characters escaped."""

    return inline_text(value).strip() or "-"
