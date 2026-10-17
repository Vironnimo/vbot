"""An in-memory vBot server in the access mode ``authenticated`` for Desktop sign-in tests.

It answers the pairing endpoints and the RPC methods the Desktop uses
(``auth.login_ticket``, ``devices.list``, ``devices.revoke``,
``devices.logout``) through ``httpx.MockTransport``, with the wire shapes of
``.vorch/domain-maps/access/accessor-contracts.md``.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from desktop.access import ServerAccess
from desktop.credentials import DEVICE_TOKENS_FILE_NAME, DeviceTokenStore

COMPUTER = "TESTPC"
DEVICE_NAME = f"vBot Desktop on {COMPUTER}"
WINDOW_NAME = f"vBot Desktop window on {COMPUTER}"
LOCAL_TOKEN = "vbl_" + "L" * 43


@dataclass
class FakeDevice:
    id: str
    token: str = field(repr=False)
    name: str
    kind: str
    approved_by: str
    created_at: str


@dataclass
class FakePairing:
    request_id: str
    poll_secret: str
    code: str
    kind: str
    name: str
    status: str = "pending"


class FakeAccessServer:
    """The access endpoints of one server; ``transport`` serves them to httpx."""

    def __init__(self) -> None:
        self.local_token: str | None = None
        self.devices: dict[str, FakeDevice] = {}
        self.pairings: dict[str, FakePairing] = {}
        self.tickets: dict[str, tuple[str, str]] = {}
        self.requests: list[httpx.Request] = []
        # Answers served before the regular ones, per path.
        self.scripted: dict[str, list[httpx.Response]] = {}
        # The status a new pairing request answers its polls with, and its lifetime.
        self.pairing_outcome = "pending"
        self.pairing_expires_in = 600
        # Requests per path that fail as unreachable before the server answers.
        self.unreachable: dict[str, int] = {}
        # Runs for every request before it is answered.
        self.on_request: Callable[[httpx.Request], None] | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    # -- Test controls ---------------------------------------------------------

    def add_device(
        self, *, kind: str = "desktop", name: str = DEVICE_NAME, approved_by: str = "host"
    ) -> FakeDevice:
        with self._lock:
            number = next(self._ids)
        device_id = f"dev_{number:016d}"
        device = FakeDevice(
            id=device_id,
            token=f"vbt_{device_id}_{number:0>43}",
            name=name,
            kind=kind,
            approved_by=approved_by,
            created_at=f"2026-10-17T10:{number:02d}:00Z",
        )
        self.devices[device_id] = device
        return device

    def decide(self, code: str, status: str) -> None:
        """Approve, deny or expire the pairing request showing ``code``."""
        for pairing in self.pairings.values():
            if pairing.code == code:
                pairing.status = status
                return
        raise AssertionError(f"no pairing request shows {code}")

    def redeem(self, path: str) -> FakeDevice:
        """Redeem a login ticket as the WebView would: a new browser Device."""
        actor, name = self.tickets.pop(path.removeprefix("/auth/ticket/"))
        return self.add_device(kind="browser", name=name, approved_by=actor)

    def calls(self, method: str) -> list[dict[str, Any]]:
        """The params of every RPC call of ``method``."""
        return [
            body["params"]
            for request in self.requests
            if request.url.path == "/api/rpc"
            and (body := json.loads(request.content))["method"] == method
        ]

    def bearers(self) -> list[str | None]:
        """The ``Authorization`` header of every request, in order."""
        return [request.headers.get("Authorization") for request in self.requests]

    # -- Transport -------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.on_request is not None:
            self.on_request(request)
        with self._lock:
            self.requests.append(request)
            if self.unreachable.get(request.url.path):
                self.unreachable[request.url.path] -= 1
                raise httpx.ConnectError("unreachable", request=request)
            queued = self.scripted.get(request.url.path)
            if queued:
                return queued.pop(0)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/auth/pair/start":
            return self._start(body)
        if request.url.path == "/api/auth/pair/poll":
            return self._poll(body)
        if request.url.path == "/api/rpc":
            return self._rpc(request, body)
        return httpx.Response(404)

    def _start(self, body: dict[str, Any]) -> httpx.Response:
        with self._lock:
            number = next(self._ids)
        pairing = FakePairing(
            request_id=f"req{number}",
            poll_secret=f"secret{number}",
            code=f"K7M2-Q{number:03d}",
            kind=body["kind"],
            name=body["name"],
            status=self.pairing_outcome,
        )
        self.pairings[pairing.request_id] = pairing
        return httpx.Response(
            200,
            json={
                "request_id": pairing.request_id,
                "code": pairing.code,
                "poll_secret": pairing.poll_secret,
                "expires_in": self.pairing_expires_in,
                "poll_interval": 2,
            },
        )

    def _poll(self, body: dict[str, Any]) -> httpx.Response:
        pairing = self.pairings.get(body.get("request_id", ""))
        if pairing is None or pairing.poll_secret != body.get("poll_secret"):
            return _error(404, "not_found")
        if pairing.status == "pending":
            return httpx.Response(200, json={"status": "pending", "expires_in": 500})
        if pairing.status != "approved":
            return httpx.Response(200, json={"status": pairing.status})
        del self.pairings[pairing.request_id]
        device = self.add_device(kind=pairing.kind, name=pairing.name)
        return httpx.Response(
            200,
            json={
                "status": "approved",
                "device": {"id": device.id, "name": device.name, "kind": device.kind},
                "token": device.token,
            },
        )

    def _rpc(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        principal = self._principal(request)
        if principal is None:
            return _error(401, "auth_required")
        method, params = body["method"], body.get("params") or {}
        if method == "auth.login_ticket":
            with self._lock:
                ticket = f"ticket{next(self._ids):020d}"
            self.tickets[ticket] = (principal, params.get("name", "Browser"))
            return _result({"ticket": ticket, "path": f"/auth/ticket/{ticket}", "expires_in": 60})
        if method == "devices.list":
            return _result(
                {
                    "devices": [
                        {
                            "id": device.id,
                            "name": device.name,
                            "kind": device.kind,
                            "created_at": device.created_at,
                            "last_used_at": None,
                            "last_client": None,
                            "approved_by": device.approved_by,
                            "current": device.id == principal,
                        }
                        for device in self.devices.values()
                    ]
                }
            )
        if method == "devices.revoke":
            if self.devices.pop(params["id"], None) is None:
                return _error(200, "device_not_found")
            return _result({"revoked": [params["id"]]})
        if method == "devices.logout":
            if self.devices.pop(principal, None) is None:
                return _error(200, "access_denied")
            return _result({"revoked": [principal]})
        return _error(200, "method_not_found")

    def _principal(self, request: httpx.Request) -> str | None:
        header = request.headers.get("Authorization")
        if header is None:
            return None
        token = header.removeprefix("Bearer ")
        if self.local_token is not None and token == self.local_token:
            return "host"
        return next((device.id for device in self.devices.values() if device.token == token), None)


def _result(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error": {"code": code, "message": code}})


def token_store(tmp_path: Path) -> DeviceTokenStore:
    return DeviceTokenStore(tmp_path / DEVICE_TOKENS_FILE_NAME)


def build_access(
    server: FakeAccessServer,
    tmp_path: Path,
    *,
    store: DeviceTokenStore | None = None,
    server_data_dir: Path | None = None,
    **options: Any,
) -> ServerAccess:
    """A :class:`ServerAccess` over ``server``; its token store defaults to ``tmp_path``."""
    return ServerAccess(
        store=token_store(tmp_path) if store is None else store,
        server_data_dir=server_data_dir,
        transport=server.transport,
        computer_name=COMPUTER,
        **options,
    )


def write_local_token(data_dir: Path, port: int, token: str = LOCAL_TOKEN) -> None:
    runtime = data_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / f"local-access-{port}.token").write_text(token, encoding="ascii")
