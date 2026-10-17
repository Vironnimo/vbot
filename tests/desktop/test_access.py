"""The Desktop's sign-in to servers in the access mode ``authenticated`` (desktop.access)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from desktop.access import (
    ERROR_ACCESS_DENIED,
    ERROR_DEVICES_UNAVAILABLE,
    ERROR_RATE_LIMITED,
    ERROR_SIGN_IN_FAILED,
    AccessFailure,
    PairingRequest,
    ServerAccess,
    SignIn,
)
from desktop.credentials import DeviceTokenStore
from desktop.server_address import ServerAddress
from tests.desktop.access_test_support import (
    DEVICE_NAME,
    LOCAL_TOKEN,
    WINDOW_NAME,
    FakeAccessServer,
    FakeDevice,
    build_access,
    token_store,
    write_local_token,
)

REMOTE = ServerAddress("https", "vbot.example.com", 443)
LOOPBACK = ServerAddress("http", "127.0.0.1", 8420)


class FakeTime:
    """A monotonic clock that the pairing wait advances; ``on_wait`` acts per wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.on_wait: list[Callable[[], None]] = []

    def clock(self) -> float:
        return self.now

    def wait(self, event: threading.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        if self.on_wait:
            self.on_wait.pop(0)()
        return event.is_set()


def _paired(
    server: FakeAccessServer, tmp_path: Path, address: ServerAddress = REMOTE
) -> tuple[ServerAccess, DeviceTokenStore, FakeDevice]:
    """A Desktop that paired with ``server`` earlier."""
    store = token_store(tmp_path)
    device = server.add_device()
    store.save_device(address.origin, device.token, device.id)
    return build_access(server, tmp_path, store=store), store, device


def _polls(server: FakeAccessServer) -> int:
    return sum(request.url.path == "/api/auth/pair/poll" for request in server.requests)


# -- Window sign-in ----------------------------------------------------------------------


def test_the_installations_own_server_is_signed_in_with_its_local_access_token(
    tmp_path: Path,
) -> None:
    server = FakeAccessServer()
    server.local_token = LOCAL_TOKEN
    write_local_token(tmp_path / "data", 8420)
    access = build_access(server, tmp_path, server_data_dir=tmp_path / "data")

    sign_in = access.sign_in(LOOPBACK)

    assert sign_in.ticket_path is not None
    assert sign_in.ticket_path.startswith("/auth/ticket/")
    assert server.bearers() == [f"Bearer {LOCAL_TOKEN}"]
    assert server.calls("auth.login_ticket") == [{"name": WINDOW_NAME}]
    assert access.header(LOOPBACK.url) == f"Bearer {LOCAL_TOKEN}"
    # The local access token is no pairing: there is no sign-in to forget.
    assert access.signed_in(LOOPBACK) is False


def test_a_refused_local_access_token_is_read_again_once(tmp_path: Path) -> None:
    server = FakeAccessServer()
    fresh = "vbl_" + "F" * 43
    server.local_token = fresh
    write_local_token(tmp_path / "data", 8420)  # still the token of the previous start
    # The server writes its new token while it answers.
    server.on_request = lambda _request: write_local_token(tmp_path / "data", 8420, fresh)
    access = build_access(server, tmp_path, server_data_dir=tmp_path / "data")

    sign_in = access.sign_in(LOOPBACK)

    assert sign_in.ticket_path is not None
    assert server.bearers() == [f"Bearer {LOCAL_TOKEN}", f"Bearer {fresh}"]


@pytest.mark.parametrize(
    ("address", "data_dir"),
    [
        pytest.param(REMOTE, True, id="remote-server"),
        pytest.param(ServerAddress("http", "127.0.0.1", 9999), True, id="other-port"),
        pytest.param(LOOPBACK, False, id="no-data-dir"),
    ],
)
def test_the_local_access_token_never_leaves_its_own_loopback_server(
    tmp_path: Path, address: ServerAddress, data_dir: bool
) -> None:
    server = FakeAccessServer()
    server.local_token = LOCAL_TOKEN
    write_local_token(tmp_path / "data", 8420)
    access = build_access(server, tmp_path, server_data_dir=tmp_path / "data" if data_dir else None)

    assert access.sign_in(address) == SignIn(pairing_required=True)
    assert access.header(address.url) is None
    assert server.requests == []


def test_a_paired_desktop_signs_in_with_its_device_token(tmp_path: Path) -> None:
    server = FakeAccessServer()
    access, _, device = _paired(server, tmp_path)

    sign_in = access.sign_in(REMOTE, "/?accessor=desktop")

    assert sign_in.ticket_path is not None
    assert server.bearers() == [f"Bearer {device.token}"]
    # The ticket lands the window on the page the Desktop asked for.
    assert server.calls("auth.login_ticket") == [
        {"name": WINDOW_NAME, "next": "/?accessor=desktop"}
    ]
    assert access.signed_in(REMOTE) is True
    # Native requests use the same credential, whatever the URL spelling.
    assert access.header("https://VBOT.example.com:443/") == f"Bearer {device.token}"
    assert access.header("https://vbot.example.com:8443/") is None


def test_a_refused_device_token_is_dropped_and_pairing_is_required(tmp_path: Path) -> None:
    server = FakeAccessServer()
    access, store, device = _paired(server, tmp_path)
    del server.devices[device.id]  # revoked on the server

    assert access.sign_in(REMOTE) == SignIn(pairing_required=True)
    assert access.signed_in(REMOTE) is False
    assert access.header(REMOTE.url) is None
    stored = store.get(REMOTE.origin)
    assert stored is None or stored.token is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            httpx.Response(403, json={"ok": False, "error": {"code": "access_denied"}}),
            SignIn(error_code=ERROR_ACCESS_DENIED),
            id="access-denied",
        ),
        pytest.param(
            httpx.Response(
                429,
                headers={"Retry-After": "30"},
                json={"ok": False, "error": {"code": "rate_limited"}},
            ),
            SignIn(error_code=ERROR_RATE_LIMITED, retry_after=30.0),
            id="rate-limited",
        ),
        # The server cannot check devices right now; the sign-in stays.
        pytest.param(
            httpx.Response(
                503,
                headers={"Retry-After": "30"},
                json={"ok": False, "error": {"code": "devices_unavailable"}},
            ),
            SignIn(error_code=ERROR_DEVICES_UNAVAILABLE, retry_after=30.0),
            id="devices-unavailable",
        ),
        pytest.param(
            httpx.Response(200, json={"ok": False, "error": {"code": "internal_error"}}),
            SignIn(error_code=ERROR_SIGN_IN_FAILED),
            id="rpc-error",
        ),
        # The server left the access mode ``authenticated`` meanwhile.
        pytest.param(
            httpx.Response(200, json={"ok": False, "error": {"code": "auth_disabled"}}),
            SignIn(),
            id="auth-disabled",
        ),
        pytest.param(
            httpx.Response(200, json={"ok": True, "result": {"path": "/elsewhere"}}),
            SignIn(error_code=ERROR_SIGN_IN_FAILED),
            id="invalid-ticket",
        ),
    ],
)
def test_a_sign_in_the_server_does_not_grant_names_its_reason(
    tmp_path: Path, answer: httpx.Response, expected: SignIn
) -> None:
    server = FakeAccessServer()
    access, _, _ = _paired(server, tmp_path)
    server.scripted["/api/rpc"] = [answer]

    assert access.sign_in(REMOTE) == expected
    # Only a refused credential is dropped.
    assert access.signed_in(REMOTE) is True


# -- Pairing -----------------------------------------------------------------------------


def _start(
    tmp_path: Path, outcome: str = "pending", expires_in: int = 600
) -> tuple[FakeAccessServer, ServerAccess, FakeTime, PairingRequest]:
    server = FakeAccessServer()
    server.pairing_outcome = outcome
    server.pairing_expires_in = expires_in
    fake_time = FakeTime()
    access = build_access(server, tmp_path, clock=fake_time.clock, wait=fake_time.wait)
    request = access.start_pairing(REMOTE)
    return server, access, fake_time, request


def test_pairing_shows_a_code_and_stores_the_approved_device_token(tmp_path: Path) -> None:
    server, access, fake_time, request = _start(tmp_path)
    [pairing] = server.pairings.values()
    assert (pairing.kind, pairing.name) == ("desktop", DEVICE_NAME)
    assert request.code == pairing.code
    assert server.bearers() == [None]
    # Approved while the Desktop waits for its third poll.
    fake_time.on_wait = [
        lambda: None,
        lambda: None,
        lambda: server.decide(request.code, "approved"),
    ]

    outcome = access.await_pairing(request, threading.Event())

    assert outcome.status == "approved"
    assert fake_time.waits == [2.0, 2.0, 2.0]
    assert access.signed_in(REMOTE) is True
    [device] = server.devices.values()
    assert access.header(REMOTE.url) == f"Bearer {device.token}"


@pytest.mark.parametrize("decision", ["denied", "expired"])
def test_a_pairing_the_server_ends_reports_how(tmp_path: Path, decision: str) -> None:
    server, access, fake_time, request = _start(tmp_path)
    fake_time.on_wait = [lambda: server.decide(request.code, decision)]

    assert access.await_pairing(request, threading.Event()).status == decision
    assert access.signed_in(REMOTE) is False


def test_a_pairing_expires_at_its_deadline_without_another_poll(tmp_path: Path) -> None:
    server, access, fake_time, request = _start(tmp_path, expires_in=9)

    assert access.await_pairing(request, threading.Event()).status == "expired"
    assert fake_time.waits == [2.0, 2.0, 2.0, 2.0, 1.0]
    assert _polls(server) == 4


def test_a_pairing_poll_without_an_answer_is_repeated(tmp_path: Path) -> None:
    server, access, fake_time, request = _start(tmp_path, "approved")
    server.unreachable["/api/auth/pair/poll"] = 1
    server.scripted["/api/auth/pair/poll"] = [
        httpx.Response(
            429, headers={"Retry-After": "7"}, json={"ok": False, "error": {"code": "rate_limited"}}
        ),
        httpx.Response(502, text="Bad gateway"),  # a proxy while the server restarts
    ]

    assert access.await_pairing(request, threading.Event()).status == "approved"
    # Unreachable, rate limited (waits as long as asked), proxy error, approved.
    assert fake_time.waits == [2.0, 2.0, 7.0, 2.0]


def test_a_cancelled_pairing_stops_polling(tmp_path: Path) -> None:
    server, access, fake_time, request = _start(tmp_path)
    stop = threading.Event()
    fake_time.on_wait = [lambda: None, stop.set]

    assert access.await_pairing(request, stop).status == "cancelled"
    assert _polls(server) == 1


def test_a_poll_the_server_refuses_fails_the_pairing(tmp_path: Path) -> None:
    server, access, _, request = _start(tmp_path)
    server.scripted["/api/auth/pair/poll"] = [
        httpx.Response(503, json={"ok": False, "error": {"code": "devices_unavailable"}})
    ]

    outcome = access.await_pairing(request, threading.Event())

    assert (outcome.status, outcome.error_code) == ("failed", "devices_unavailable")


@pytest.mark.parametrize(
    ("answer", "error_code", "retry_after"),
    [
        pytest.param(
            httpx.Response(
                429,
                headers={"Retry-After": "120"},
                json={"ok": False, "error": {"code": "rate_limited"}},
            ),
            ERROR_RATE_LIMITED,
            120.0,
            id="rate-limited",
        ),
        pytest.param(
            httpx.Response(200, json={"request_id": "r", "code": "nope"}),
            ERROR_SIGN_IN_FAILED,
            None,
            id="invalid-answer",
        ),
    ],
)
def test_a_pairing_the_server_does_not_start_raises_its_reason(
    tmp_path: Path, answer: httpx.Response, error_code: str, retry_after: float | None
) -> None:
    server = FakeAccessServer()
    server.scripted["/api/auth/pair/start"] = [answer]
    access = build_access(server, tmp_path)

    with pytest.raises(AccessFailure) as failure:
        access.start_pairing(REMOTE)

    assert (failure.value.error_code, failure.value.retry_after) == (error_code, retry_after)


def test_no_token_ticket_code_or_secret_reaches_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        server, access, fake_time, request = _start(tmp_path)
        fake_time.on_wait = [lambda: server.decide(request.code, "approved")]
        access.await_pairing(request, threading.Event())
        access.sign_in(REMOTE)
        access.rejected(REMOTE.url, access.header(REMOTE.url))

    [device] = server.devices.values()
    secrets = [device.token, request.code, request.poll_secret, *server.tickets]
    assert caplog.records
    assert [secret for secret in secrets if secret in caplog.text] == []


# -- Native requests -----------------------------------------------------------------------


def test_a_refused_device_token_is_dropped_and_the_lost_sign_in_reported(
    tmp_path: Path,
) -> None:
    server = FakeAccessServer()
    access, _, _ = _paired(server, tmp_path)
    lost: list[ServerAddress] = []
    access.set_sign_in_lost_listener(lost.append)

    assert access.rejected(REMOTE.url, access.header(REMOTE.url)) is False
    assert access.header(REMOTE.url) is None
    assert lost == [REMOTE]
    # So is a request refused without any credential.
    assert access.rejected(REMOTE.url, None) is False
    assert lost == [REMOTE, REMOTE]


def test_a_refusal_of_a_replaced_token_retries_with_the_new_one(tmp_path: Path) -> None:
    server = FakeAccessServer()
    earlier = server.add_device()
    access, _, _ = _paired(server, tmp_path)  # paired again meanwhile
    lost: list[ServerAddress] = []
    access.set_sign_in_lost_listener(lost.append)

    assert access.rejected(REMOTE.url, f"Bearer {earlier.token}") is True
    assert access.signed_in(REMOTE) is True
    assert lost == []


def test_a_refused_local_access_token_retries_only_when_the_file_changed(tmp_path: Path) -> None:
    server = FakeAccessServer()
    write_local_token(tmp_path / "data", 8420)
    access = build_access(server, tmp_path, server_data_dir=tmp_path / "data")
    header = access.header(LOOPBACK.url)

    assert access.rejected(LOOPBACK.url, header) is False
    renewed = "vbl_" + "N" * 43
    write_local_token(tmp_path / "data", 8420, renewed)
    assert access.rejected(LOOPBACK.url, header) is True
    assert access.header(LOOPBACK.url) == f"Bearer {renewed}"


# -- Forgetting ------------------------------------------------------------------------


def test_forgetting_a_sign_in_signs_the_desktop_and_its_window_out(tmp_path: Path) -> None:
    server = FakeAccessServer()
    access, store, _ = _paired(server, tmp_path)
    server.redeem(access.sign_in(REMOTE).ticket_path or "")

    assert access.forget(REMOTE) is True
    assert access.signed_in(REMOTE) is False
    assert store.get(REMOTE.origin) is None
    # The server is told in the background; the window's Device goes with the Desktop's.
    deadline = time.monotonic() + 5
    while server.devices and time.monotonic() < deadline:
        time.sleep(0.005)
    assert server.devices == {}
    assert [len(server.calls(method)) for method in ("devices.logout", "devices.revoke")] == [1, 0]
    assert access.forget(REMOTE) is False
