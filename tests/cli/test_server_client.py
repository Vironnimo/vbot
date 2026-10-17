"""The shared server client options: which credential each target's requests carry."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from cli._server_target import resolve_instance
from cli.server_client import http_options, websocket_target
from cli.server_management import ServerInstance
from cli.sign_ins import SignIn, SignInStore
from core.access.local_token import create_local_access_token
from tests.cli.cli_test_support import make_instance

DEVICE_TOKEN = "vbt_dev1_" + "d" * 43


def _sign_in(origin: str) -> None:
    SignInStore().save_sign_in(
        SignIn(origin, DEVICE_TOKEN, "dev1", "CLI on test", "2026-10-17T00:00:00Z")
    )


def _sent_authorization(instance: ServerInstance, url: str | None = None) -> list[str | None]:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True, "result": {}})

    with httpx.Client(transport=httpx.MockTransport(handler), **http_options(instance)) as client:
        client.post(url or f"{instance.url}/api/rpc", json={})
    return seen


@pytest.mark.parametrize(
    ("target", "local_token", "signed_in_origin", "expected"),
    [
        pytest.param({"host": "127.0.0.1"}, True, None, "local", id="loopback-local-token"),
        pytest.param({"host": "::1"}, True, None, "local", id="ipv6-loopback"),
        pytest.param({"host": "0.0.0.0"}, True, None, "local", id="wildcard-connects-loopback"),
        pytest.param({"host": "192.168.1.5"}, True, None, None, id="lan-address-never-local"),
        pytest.param(
            {"url": "https://vbot.example.com"},
            True,
            "https://vbot.example.com",
            "device",
            id="remote-device-sign-in",
        ),
        pytest.param(
            {"url": "https://vbot.example.com"},
            False,
            "https://vbot.example.com:8443",
            None,
            id="sign-in-of-another-port",
        ),
        pytest.param(
            {"url": "http://localhost:9000"},
            False,
            "http://localhost:9000",
            "device",
            id="tunnel-without-local-token",
        ),
    ],
)
def test_each_target_gets_its_own_credential(
    tmp_path: Path,
    target: dict[str, str],
    local_token: bool,
    signed_in_origin: str | None,
    expected: str | None,
) -> None:
    data_dir = tmp_path / "data"
    instance = resolve_instance(data_dir=data_dir, port=8420, **target)
    token = create_local_access_token(data_dir, 8420).value if local_token else None
    if signed_in_origin is not None:
        _sign_in(signed_in_origin)

    sent = _sent_authorization(instance)

    authorization = {"local": f"Bearer {token}", "device": f"Bearer {DEVICE_TOKEN}", None: None}
    assert sent == [authorization[expected]]


def test_a_request_to_another_origin_carries_no_credential(tmp_path: Path) -> None:
    instance = resolve_instance(data_dir=tmp_path / "data", url="https://vbot.example.com")
    _sign_in(instance.url)

    assert _sent_authorization(instance, "https://elsewhere.example.com/api/rpc") == [None]


def test_a_rejected_local_token_is_read_again_once(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    old = create_local_access_token(instance.data_dir, instance.port).value
    current: list[str] = []
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if not current:
            # The server restarted after the client read the token.
            current.append(create_local_access_token(instance.data_dir, instance.port).value)
        if request.headers.get("Authorization") == f"Bearer {current[0]}":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(401, json={"ok": False, "error": {"code": "auth_required"}})

    with httpx.Client(transport=httpx.MockTransport(handler), **http_options(instance)) as client:
        first = client.post(f"{instance.url}/api/rpc", json={})
        second = client.post(f"{instance.url}/api/rpc", json={})

    assert first.status_code == second.status_code == 200
    assert seen == [f"Bearer {old}", f"Bearer {current[0]}", f"Bearer {current[0]}"]


@pytest.mark.parametrize("remote", [False, True], ids=["unchanged-local-token", "device-token"])
def test_a_refused_credential_that_did_not_change_is_not_sent_again(
    tmp_path: Path, remote: bool
) -> None:
    if remote:
        instance = resolve_instance(data_dir=tmp_path / "data", url="https://vbot.example.com")
        _sign_in(instance.url)
    else:
        instance = make_instance(tmp_path)
        create_local_access_token(instance.data_dir, instance.port)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"ok": False, "error": {"code": "auth_required"}})

    with httpx.Client(transport=httpx.MockTransport(handler), **http_options(instance)) as client:
        assert client.post(f"{instance.url}/api/rpc", json={}).status_code == 401

    assert len(requests) == 1


@pytest.mark.parametrize(
    ("target", "scheme", "verified"),
    [({"host": "127.0.0.1"}, "ws", False), ({"url": "https://vbot.example.com"}, "wss", True)],
)
def test_websocket_target_carries_the_credential_and_verifies_wss(
    tmp_path: Path, target: dict[str, str], scheme: str, verified: bool
) -> None:
    data_dir = tmp_path / "data"
    instance = resolve_instance(data_dir=data_dir, port=8420, **target)
    token = create_local_access_token(data_dir, 8420).value
    _sign_in("https://vbot.example.com")

    url, options = websocket_target(instance, "/ws?accessor=tui")

    assert url == f"{scheme}://{instance.url.partition('://')[2]}/ws?accessor=tui"
    expected = DEVICE_TOKEN if verified else token
    assert options["additional_headers"] == {"Authorization": f"Bearer {expected}"}
    assert options["proxy"] is None
    assert isinstance(options.get("ssl"), ssl.SSLContext) is verified


def test_a_named_sign_in_source_replaces_the_cli_sign_ins_and_is_read_again_after_a_401(
    tmp_path: Path,
) -> None:
    remote = resolve_instance(data_dir=tmp_path / "data", url="https://vbot.example.com")
    loopback = make_instance(tmp_path)
    local = create_local_access_token(loopback.data_dir, loopback.port).value
    _sign_in(remote.url)
    # The Desktop signs in again while the tray's client still holds the old token.
    desktop_tokens = ["vbt_dev2_" + "a" * 43, "vbt_dev2_" + "b" * 43]
    asked: list[str] = []

    def desktop_sign_in(origin: str) -> str | None:
        asked.append(origin)
        return desktop_tokens[min(len(asked), 2) - 1]

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") == f"Bearer {desktop_tokens[1]}":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(401, json={"ok": False, "error": {"code": "auth_required"}})

    options = http_options(remote, device_sign_in=desktop_sign_in)
    with httpx.Client(transport=httpx.MockTransport(handler), **options) as client:
        answered = client.post(f"{remote.url}/api/rpc", json={})
    _url, stream = websocket_target(loopback, "/ws", device_sign_in=desktop_sign_in)

    assert answered.status_code == 200
    assert seen == [f"Bearer {desktop_tokens[0]}", f"Bearer {desktop_tokens[1]}"]
    assert asked == [remote.url, remote.url]
    # This computer's own server still gets its local access token.
    assert stream["additional_headers"] == {"Authorization": f"Bearer {local}"}
