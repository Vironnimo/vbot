"""Tests for the shared CLI RPC transport client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import rpc_client
from cli.server_management import ServerInstance, build_server_base_url
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, host: str = "127.0.0.1", port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host=host,
        port=port,
        data_dir=data_dir,
        url=build_server_base_url(host, port),
        log_path=resolve_daily_log_path(data_dir),
    )


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: Any, trust_env: bool
    ) -> httpx.Response:
        captured["url"] = url
        del json
        captured["timeout"] = timeout
        captured["trust_env"] = trust_env
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(rpc_client.httpx, "post", fake_post)
    return captured


def test_rpc_call_uses_default_timeout_for_ordinary_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path), "settings.get_raw", {})

    assert captured["timeout"] == rpc_client.RPC_TIMEOUT_SECONDS


def test_rpc_call_ignores_environment_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RPC bodies carry secrets (e.g. provider.set_key) over a plaintext loopback call, so the
    # transport must never honor ambient HTTP_PROXY/.netrc that could divert them off-host.
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path), "provider.set_key", {"value": "sk-secret"})

    assert captured["trust_env"] is False


def test_rpc_call_uses_ipv6_safe_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path, host="::1"), "settings.get_raw", {})

    assert captured["url"] == "http://[::1]:8420/api/rpc"


@pytest.mark.parametrize(
    "method",
    ["model.refresh_db", "session_store.snapshot_create"],
)
def test_rpc_call_uses_unbounded_read_timeout_for_long_running_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path), method, {})

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    # Read is unbounded after the local server accepts the operation, while
    # connect still fails fast on an unreachable server.
    assert timeout.read is None
    assert timeout.connect == rpc_client.RPC_TIMEOUT_SECONDS
    assert timeout.write == rpc_client.RPC_TIMEOUT_SECONDS
    assert timeout.pool == rpc_client.RPC_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("error_type", "request_state"),
    [
        (httpx.ConnectError, "not_sent"),
        (httpx.ConnectTimeout, "not_sent"),
        (httpx.PoolTimeout, "not_sent"),
        (httpx.ReadTimeout, "unknown"),
        (httpx.ReadError, "unknown"),
        (httpx.WriteTimeout, "unknown"),
        (httpx.WriteError, "unknown"),
        (httpx.RemoteProtocolError, "unknown"),
    ],
)
def test_transport_failure_reports_delivery_state_without_replay_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.RequestError],
    request_state: str,
) -> None:
    instance = make_instance(tmp_path, host="::1", port=9876)
    requests: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        requests.append(kwargs["json"])
        # Exception text can contain request data and must never be rendered.
        raise error_type("sk-test-secret", request=httpx.Request("POST", url))

    monkeypatch.setattr(rpc_client.httpx, "post", fake_post)
    result = rpc_client.rpc_call(instance, "provider.set_key", {"value": "sk-test-secret"})

    assert not result.ok
    assert result.instance is instance
    assert len(requests) == 1
    assert requests[0]["params"]["value"] == "sk-test-secret"
    assert f"request_state: {request_state}" in result.message
    assert "rpc_method: provider.set_key" in result.message
    assert f"server: {instance.url}" in result.message
    assert error_type.__name__ in result.message
    assert "sk-test-secret" not in result.message
    command_result = result.to_command_result()
    assert not command_result.ok
    assert command_result.message == result.message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="sk-test-secret"),
        httpx.Response(200, json=["sk-test-secret"]),
        httpx.Response(200, json={"ok": True, "result": ["sk-test-secret"]}),
        httpx.Response(200, json={"ok": "true", "secret": "sk-test-secret"}),
    ],
)
def test_malformed_response_preserves_applied_mutation_and_reports_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    saved: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        del url
        # The receiver applied the write before its unusable response arrived.
        saved.append(kwargs["json"]["params"])
        return response

    monkeypatch.setattr(rpc_client.httpx, "post", fake_post)
    result = rpc_client.rpc_call(make_instance(tmp_path), "agent.create", {"id": "demo"})

    assert saved == [{"id": "demo"}]
    assert not result.ok
    assert "request_state: unknown" in result.message
    assert "rpc_method: agent.create" in result.message
    assert "sk-test-secret" not in result.message


@pytest.mark.parametrize("status_code", [200, 400, 500])
def test_server_error_code_and_message_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"ok": False, "error": {"code": "test_code", "message": "test sentinel"}},
        )

    monkeypatch.setattr(rpc_client.httpx, "post", fake_post)
    result = rpc_client.rpc_call(make_instance(tmp_path), "agent.update", {})

    assert not result.ok
    assert result.message == "test_code: test sentinel"
