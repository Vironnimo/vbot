"""Tests for provider CLI parsing and provider-management RPC commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import provider_management
from cli.server_management import CommandResult, ServerInstance
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


def test_parse_args_supports_provider_list_target_options() -> None:
    args = cli_main.parse_args(
        ["provider", "list", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "provider"
    assert args.command == "list"
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_provider_list_posts_connection_list_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result == CommandResult(ok=True, message="no connections configured", instance=instance)
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "connection.list", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_provider_list_formats_connection_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "connection.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "connections": [
                        {
                            "id": "openai:default",
                            "provider_id": "openai",
                            "type": "api_key",
                            "label": "OpenAI",
                            "usable": True,
                        },
                        {
                            "id": "openrouter:main",
                            "provider_id": "openrouter",
                            "type": "api_key",
                            "label": "OpenRouter",
                            "usable": False,
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result.ok is True
    assert "connections:" in result.message
    assert "openai:default" in result.message
    assert "openrouter:main" in result.message
    assert "usable: yes" in result.message
    assert "usable: no" in result.message


def test_provider_list_returns_empty_message_when_no_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "connection.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result == CommandResult(ok=True, message="no connections configured", instance=instance)


def test_provider_list_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "connection.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            500,
            json={"ok": False, "error": {"code": "provider_error", "message": "boom"}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result == CommandResult(ok=False, message="provider_error: boom", instance=instance)
