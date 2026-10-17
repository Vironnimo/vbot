"""``vbot devices pair`` and ``vbot devices logout`` against a scripted server and clock."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import device_pairing
from cli import main as cli_main
from cli._server_target import resolve_instance
from cli.device_pairing import devices_logout, devices_pair
from cli.server_management import CommandResult, ServerInstance
from cli.sign_ins import SignIn, SignInStore

ORIGIN = "https://vbot.example.com"
TOKEN = "vbt_dev7_" + "t" * 43
POLL_SECRET = "poll-secret-" + "s" * 30
_UNAUTHENTICATED = {
    "mode": "authenticated",
    "mode_label": "With login (internet)",
    "authenticated": False,
    "principal": None,
    "device": None,
}
_APPROVED = {"status": "approved", "device": {"id": "dev7", "name": "CLI on desk", "kind": "cli"}}


class FakeServer:
    """The pairing endpoints and ``devices.logout``, answering from scripted replies."""

    def __init__(self, polls: list[httpx.Response | Exception] | None = None) -> None:
        self.status: dict[str, Any] = dict(_UNAUTHENTICATED)
        self.start = httpx.Response(
            200,
            json={
                "request_id": "pr_1",
                "code": "K7QM-4XPA",
                "poll_secret": POLL_SECRET,
                "expires_in": 600,
                "poll_interval": 2,
            },
        )
        self.polls = list(polls or [approved()])
        self.logout: httpx.Response | Exception = httpx.Response(
            200, json={"ok": True, "result": {"revoked": ["dev7"]}}
        )
        self.requests: list[tuple[str, str | None, Any]] = []

    @property
    def paths(self) -> list[str]:
        return [path for path, _authorization, _body in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, request.headers.get("Authorization"), body))
        reply: httpx.Response | Exception
        if request.url.path == "/api/auth/status":
            reply = httpx.Response(200, json=self.status)
        elif request.url.path == "/api/auth/pair/start":
            reply = self.start
        elif request.url.path == "/api/auth/pair/poll":
            reply = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        elif request.url.path == "/api/rpc":
            reply = self.logout
        else:
            raise AssertionError(f"unexpected request {request.url}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def approved(token: str = TOKEN) -> httpx.Response:
    return httpx.Response(200, json={**_APPROVED, "token": token})


def pending() -> httpx.Response:
    return httpx.Response(200, json={"status": "pending", "expires_in": 500})


def refused(status: int, code: str, **headers: str) -> httpx.Response:
    body = {"ok": False, "error": {"code": code, "message": code}}
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def remote(tmp_path: Path) -> ServerInstance:
    return resolve_instance(url=ORIGIN, data_dir=tmp_path / "data")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _pair(
    instance: ServerInstance, server: FakeServer, clock: FakeClock, *, interactive: bool = False
) -> CommandResult:
    return devices_pair(
        instance,
        interactive=interactive,
        transport=server.transport(),
        clock=clock.time,
        sleep=clock.sleep,
        computer_name="desk",
    )


def test_pairing_stores_the_token_once_the_code_is_approved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeServer()
    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **options: client(**{**options, "transport": server.transport()})
    )
    monkeypatch.setattr(device_pairing.socket, "gethostname", lambda: "desk\x1b[2J")

    code = cli_main.run(["devices", "pair", "vbot.example.com"])
    out, err = capsys.readouterr()

    assert code == 0
    assert "Code: K7QM-4XPA" in out and "vbot devices approve K7QM-4XPA" in out
    assert f"Approve it only in vBot at {ORIGIN}" in out
    assert f"Signed in to {ORIGIN}" in out and f"--url {ORIGIN}" in out
    assert TOKEN not in out + err and POLL_SECRET not in out + err
    assert "Note: Read the server address vbot.example.com as https://vbot.example.com" in err
    assert server.paths == ["/api/auth/status", "/api/auth/pair/start", "/api/auth/pair/poll"]
    assert [authorization for _path, authorization, _body in server.requests] == [None] * 3
    assert server.requests[1][2] == {"kind": "cli", "name": "CLI on desk [2J"}
    assert server.requests[2][2] == {"request_id": "pr_1", "poll_secret": POLL_SECRET}
    stored = SignInStore().sign_in(ORIGIN)
    assert stored is not None and (stored.token, stored.device_id) == (TOKEN, "dev7")
    assert SignInStore().pending(ORIGIN) is None


@pytest.mark.parametrize(
    ("polls", "ok", "expected", "sleeps"),
    [
        pytest.param(
            [pending(), refused(429, "rate_limited", **{"Retry-After": "30"}), approved()],
            True,
            "Signed in",
            [2, 30],
            id="slowed-down-then-approved",
        ),
        pytest.param(
            [httpx.ConnectError("offline"), approved()], True, "Signed in", [2], id="network-blip"
        ),
        pytest.param(
            [httpx.Response(200, json={"status": "denied"})], False, "denied", [], id="denied"
        ),
        pytest.param(
            [httpx.Response(200, json={"status": "expired"})], False, "expired", [], id="expired"
        ),
        pytest.param(
            [pending(), refused(409, "auth_disabled")],
            False,
            "does not use sign-in",
            [2],
            id="login-turned-off",
        ),
    ],
)
def test_waiting_ends_with_the_decision(
    remote: ServerInstance,
    clock: FakeClock,
    polls: list[httpx.Response | Exception],
    ok: bool,
    expected: str,
    sleeps: list[float],
) -> None:
    result = _pair(remote, FakeServer(polls), clock)

    assert result.ok is ok
    assert expected in result.message
    assert clock.sleeps == sleeps
    assert (SignInStore().sign_in(ORIGIN) is not None) is ok
    assert SignInStore().pending(ORIGIN) is None


def test_an_unfinished_pairing_continues_with_the_same_code(
    remote: ServerInstance, clock: FakeClock
) -> None:
    server = FakeServer([pending()])

    waiting = _pair(remote, server, clock)
    server.polls = [approved()]
    finished = _pair(remote, server, clock)

    assert waiting.ok and waiting.attention
    assert waiting.message.startswith("Not finished: the code K7QM-4XPA still waits")
    assert f"vbot devices pair {ORIGIN} again" in waiting.message
    assert sum(clock.sleeps) == pytest.approx(device_pairing.PAIR_WAIT_SECONDS)
    assert finished.ok and "Signed in" in finished.message
    assert server.paths.count("/api/auth/pair/start") == 1


def test_a_terminal_waits_until_the_code_expires(remote: ServerInstance, clock: FakeClock) -> None:
    result = _pair(remote, FakeServer([pending()]), clock, interactive=True)

    assert not result.ok and "expired" in result.message
    assert sum(clock.sleeps) == pytest.approx(600)
    assert SignInStore().pending(ORIGIN) is None


def test_stopping_keeps_the_code_for_the_next_run(remote: ServerInstance, clock: FakeClock) -> None:
    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    result = devices_pair(
        remote,
        interactive=True,
        transport=FakeServer([pending()]).transport(),
        clock=clock.time,
        sleep=interrupt,
        computer_name="desk",
    )

    assert not result.ok and "Stopped waiting" in result.message
    stored = SignInStore().pending(ORIGIN)
    assert stored is not None and stored.code == "K7QM-4XPA"


@pytest.mark.parametrize(
    ("change", "expected", "requests"),
    [
        pytest.param(
            lambda server: server.status.update(
                principal="device",
                authenticated=True,
                device={"id": "dev7", "name": "CLI on desk", "kind": "cli"},
            ),
            'Already signed in to https://vbot.example.com as "CLI on desk"',
            ["/api/auth/status"],
            id="already-signed-in",
        ),
        pytest.param(
            lambda server: server.status.update(mode="lan", mode_label="Trusted network"),
            "admits this computer without sign-in",
            ["/api/auth/status"],
            id="no-login-mode",
        ),
    ],
)
def test_pairing_without_a_need_changes_nothing(
    remote: ServerInstance,
    clock: FakeClock,
    change: Callable[[FakeServer], None],
    expected: str,
    requests: list[str],
) -> None:
    server = FakeServer()
    change(server)

    result = _pair(remote, server, clock)

    assert result.ok and expected in result.message
    assert server.paths == requests
    assert SignInStore().sign_in(ORIGIN) is None


@pytest.mark.parametrize(
    ("start", "code", "expected"),
    [
        pytest.param(
            refused(429, "rate_limited", **{"Retry-After": "240"}),
            "rate_limited",
            "for 4 minutes",
            id="rate-limited",
        ),
        pytest.param(
            refused(409, "auth_disabled"), "auth_disabled", "does not use sign-in", id="off"
        ),
        pytest.param(
            httpx.Response(404, text="Not Found"), None, "did not answer like a vBot", id="foreign"
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "request_id": "pr_1",
                    "code": "\x1b]0;owned\x07K7QM-4XPA",
                    "poll_secret": POLL_SECRET,
                    "expires_in": 600,
                    "poll_interval": 2,
                },
            ),
            None,
            "did not answer like a vBot",
            id="code-steering-the-terminal",
        ),
    ],
)
def test_a_refused_start_stores_nothing(
    remote: ServerInstance,
    clock: FakeClock,
    start: httpx.Response,
    code: str | None,
    expected: str,
) -> None:
    server = FakeServer()
    server.start = start

    result = _pair(remote, server, clock)

    assert not result.ok and expected in result.message
    assert (result.failure.code if result.failure else None) == code
    assert SignInStore().pending(ORIGIN) is None


def test_pairing_over_plain_http_to_another_computer_sends_nothing(
    tmp_path: Path, clock: FakeClock
) -> None:
    server = FakeServer()
    instance = resolve_instance(url="http://192.168.1.5:8420", data_dir=tmp_path / "data")

    result = _pair(instance, server, clock)

    assert not result.ok and "https://" in result.message
    assert server.requests == []


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        pytest.param(None, "revoked", id="revoked"),
        pytest.param(refused(401, "auth_required"), "no longer accepted", id="already-revoked"),
        pytest.param(httpx.ConnectError("offline"), "stays listed", id="unreachable"),
    ],
)
def test_logout_revokes_the_device_and_always_deletes_the_sign_in(
    remote: ServerInstance, reply: httpx.Response | Exception | None, outcome: str
) -> None:
    SignInStore().save_sign_in(SignIn(ORIGIN, TOKEN, "dev7", "CLI on desk", "2026-10-17T10:00:00Z"))
    server = FakeServer()
    if reply is not None:
        server.logout = reply

    result = devices_logout(remote, transport=server.transport())

    assert result.ok and outcome in result.message
    assert TOKEN not in result.message
    assert server.requests[0][1:] == (
        f"Bearer {TOKEN}",
        {"method": "devices.logout", "params": {}},
    )
    assert SignInStore().sign_in(ORIGIN) is None
    assert not devices_logout(remote, transport=server.transport()).attention
    assert len(server.requests) == 1
