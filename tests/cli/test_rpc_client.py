"""Tests for the shared CLI RPC transport client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import rpc_client
from cli.server_management import ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
    )


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: Any, trust_env: bool
    ) -> httpx.Response:
        del url, json
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


def test_rpc_call_uses_unbounded_read_timeout_for_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_request(monkeypatch)

    rpc_client.rpc_call(make_instance(tmp_path), "model.refresh_db", {})

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    # Read is unbounded (the server bounds the work) while connect still fails
    # fast on an unreachable server.
    assert timeout.read is None
    assert timeout.connect == rpc_client.RPC_TIMEOUT_SECONDS
