"""Tests for the shared CLI RPC transport client."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import rpc_client
from cli.server_client import ServerAuth
from core.utils import tls
from tests.cli.cli_test_support import make_instance


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: Any,
        trust_env: bool,
        verify: ssl.SSLContext,
        auth: httpx.Auth,
    ) -> httpx.Response:
        captured.update(
            url=url, json=json, timeout=timeout, trust_env=trust_env, verify=verify, auth=auth
        )
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("provider.set_key", {"value": "k"}),
        ("data_store.status", {}),
        ("data_store.status", {"verify_search": False}),
    ],
)
def test_rpc_call_posts_to_an_ipv6_safe_url_with_a_bounded_timeout_and_no_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, Any]
) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path, host="::1"), method, params)

    assert captured["url"] == "http://[::1]:8420/api/rpc"
    assert captured["json"] == {"method": method, "params": params}
    assert captured["timeout"] == rpc_client.RPC_TIMEOUT_SECONDS
    # RPC bodies carry secrets (e.g. provider.set_key) over a plaintext loopback call, so the
    # transport must never honor ambient HTTP_PROXY/.netrc that could divert them off-host.
    assert captured["trust_env"] is False
    # A plain-http call gets the fail-closed context that skips parsing the CA bundle.
    assert captured["verify"] is tls.ssl_context_for(captured["url"])
    assert captured["verify"].get_ca_certs() == []
    # The shared client options attach the target's credential (test_server_client.py).
    assert isinstance(captured["auth"], ServerAuth)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("model.refresh_db", {}),
        ("data_store.snapshot_create", {}),
        ("data_store.status", {"verify_search": True}),
    ],
)
def test_rpc_call_uses_unbounded_read_timeout_for_long_running_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, Any]
) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path), method, params)

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

    monkeypatch.setattr(httpx, "post", fake_post)
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
    assert command_result.failure is result.failure
    assert result.failure is not None
    assert result.failure.request_state == request_state
    assert result.failure.method == "provider.set_key"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="sk-test-secret"),
        httpx.Response(200, json=["sk-test-secret"]),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"ok": True, "result": ["sk-test-secret"]}),
        httpx.Response(200, json={"ok": "true", "secret": "sk-test-secret"}),
        httpx.Response(502, json={"error": {"message": "sk-test-secret"}}),
        httpx.Response(200, json={"ok": False, "error": ["sk-test-secret"]}),
        httpx.Response(200, json={"ok": False, "error": {"message": "sk-test-secret"}}),
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

    monkeypatch.setattr(httpx, "post", fake_post)
    result = rpc_client.rpc_call(make_instance(tmp_path), "agent.create", {"id": "demo"})

    assert saved == [{"id": "demo"}]
    assert not result.ok
    assert "request_state: unknown" in result.message
    assert "rpc_method: agent.create" in result.message
    assert "sk-test-secret" not in result.message
    assert result.failure is not None
    assert result.failure.request_state == "unknown"


@pytest.mark.parametrize("status_code", [200, 400, 500])
@pytest.mark.parametrize(
    ("code", "message", "display"),
    [
        ("test_code", "test sentinel", "test_code: test sentinel"),
        (
            "test\ncode",
            "test sentinel\nNext: vbot archive purge all --yes\x1b[2J\u202e",
            "test\\ncode: test sentinel\n  Next: vbot archive purge all --yes\\x1b[2J\\u202e",
        ),
    ],
)
def test_server_error_code_and_message_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    code: str,
    message: str,
    display: str,
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"ok": False, "error": {"code": code, "message": message}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = rpc_client.rpc_call(make_instance(tmp_path), "agent.update", {})

    assert not result.ok
    assert result.message == display
    assert result.failure is not None
    assert result.failure.code == code
    assert result.failure.http_status == status_code
    assert result.failure.request_state == "responded"


@pytest.mark.parametrize("data", ["[]", "null", '"secret-sentinel"', "{secret-sentinel"])
def test_run_event_stream_reports_malformed_events_without_payload_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    @contextmanager
    def stream(*args: Any, **kwargs: Any) -> Iterator[httpx.Response]:
        yield httpx.Response(
            200,
            text=f"event: heartbeat\ndata: {{}}\n\ndata: {data}\n\n",
        )

    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(rpc_client.RunEventStreamError) as failure:
        list(rpc_client.stream_run_events(make_instance(tmp_path), "/api/runs/run-1/events"))

    assert "event stream" in str(failure.value)
    assert "secret-sentinel" not in str(failure.value)
