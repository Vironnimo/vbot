"""Tests for model CLI parsing and RPC-backed model commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import model_management
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


def test_parse_args_supports_model_list() -> None:
    args = cli_main.parse_args(
        ["model", "list", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "model"
    assert args.command == "list"
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_parse_args_supports_model_refresh_no_provider() -> None:
    args = cli_main.parse_args(
        ["model", "refresh", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "model"
    assert args.command == "refresh"
    assert args.provider is None
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_parse_args_supports_model_refresh_with_provider() -> None:
    args = cli_main.parse_args(["model", "refresh", "openai"])

    assert args.area == "model"
    assert args.command == "refresh"
    assert args.provider == "openai"


def test_model_list_posts_model_list_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={"ok": True, "result": {"models": [{"id": "openai/gpt-4o"}]}},
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_list(instance)

    assert result.ok is True
    assert result.instance == instance
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "model.list", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_model_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "model.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "models": [
                        {
                            "id": "openai/gpt-4o",
                            "name": "GPT-4o",
                            "context_window": 128000,
                        },
                        {
                            "id": "anthropic/claude-sonnet-4",
                            "name": "Claude Sonnet 4",
                            "context_window": 200000,
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_list(instance)

    assert result.ok is True
    assert result.instance == instance
    assert result.message.splitlines() == [
        "models:",
        "- id: openai/gpt-4o  name: GPT-4o  context_window: 128000",
        "- id: anthropic/claude-sonnet-4  name: Claude Sonnet 4  context_window: 200000",
    ]


def test_model_list_returns_empty_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "model.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(200, json={"ok": True, "result": {"models": []}})

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_list(instance)

    assert result == CommandResult(ok=True, message="no models available", instance=instance)


def test_model_refresh_posts_refresh_db_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={"ok": True, "result": {"refreshed_count": 2, "model_count": 50}},
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_refresh(instance)

    assert result == CommandResult(
        ok=True,
        message="refreshed 2 providers (50 models)",
        instance=instance,
    )
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "model.refresh_db", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_model_refresh_posts_refresh_db_with_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"provider_id": "openai"}})

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_refresh(instance, provider_id="openai")

    assert result == CommandResult(ok=True, message="refreshed openai", instance=instance)
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
            "timeout": 10.0,
        }
    ]


def test_model_refresh_formats_global_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "model.refresh_db", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={"ok": True, "result": {"refreshed_count": 2, "model_count": 50}},
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_refresh(instance)

    assert result == CommandResult(
        ok=True,
        message="refreshed 2 providers (50 models)",
        instance=instance,
    )


def test_model_refresh_reports_failed_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that skipped an unreachable provider names it in the message."""

    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "refreshed_count": 1,
                    "model_count": 25,
                    "errors": [
                        {
                            "provider_id": "openrouter",
                            "connection_id": "openrouter:api-key",
                            "error": "503 upstream down",
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_refresh(instance)

    assert result == CommandResult(
        ok=True,
        message="refreshed 1 providers (25 models); 1 failed: openrouter:api-key",
        instance=instance,
    )


def test_model_list_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "model.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error": {"code": "internal_error", "message": "refresh failed"},
            },
        )

    monkeypatch.setattr(model_management.httpx, "post", fake_post)

    result = model_management.model_list(instance)

    assert result == CommandResult(
        ok=False,
        message="internal_error: refresh failed",
        instance=instance,
    )
