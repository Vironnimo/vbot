"""Tests for cli provider custom."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import provider_management
from tests.cli.cli_provider_test_support import (
    make_instance,
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


def test_parse_args_supports_custom_provider_save_and_delete() -> None:
    save_args = cli_main.parse_args(
        [
            "provider",
            "custom-save",
            "local-ai",
            "--name",
            "Local AI",
            "--base-url",
            "http://127.0.0.1:8080/v1",
            "--auth",
            "none",
            "--model",
            "chat-model",
            "--model",
            "image-model",
        ]
    )
    delete_args = cli_main.parse_args(["provider", "custom-delete", "local-ai"])

    assert save_args.command == "custom-save"
    assert save_args.provider == "local-ai"
    assert save_args.adapter == "openai_compatible"
    assert save_args.model == ["chat-model", "image-model"]
    assert delete_args.command == "custom-delete"
    assert delete_args.provider == "local-ai"


def test_provider_custom_save_posts_secret_once_and_formats_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider": {
                        "id": "local-ai",
                        "model_count": 1,
                        "usable": True,
                    }
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_custom_save(
        instance,
        "local-ai",
        name="Local AI",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        auth="api_key",
        api_key="secret",
        models_endpoint="/models",
        model_ids=["chat-model"],
    )

    assert result.ok is True
    assert "local-ai" in result.message
    assert "1" in result.message
    assert "usable" in result.message
    assert calls == [
        {
            "method": "provider.custom_save",
            "params": {
                "provider": {
                    "id": "local-ai",
                    "name": "Local AI",
                    "adapter": "openai_compatible",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "auth": "api_key",
                    "models_endpoint": "/models",
                    "models": {
                        "chat-model": {
                            "name": "chat-model",
                            "capabilities": {},
                        }
                    },
                },
                "api_key": "secret",
            },
        }
    ]
    assert "secret" not in result.message


def test_provider_custom_list_and_delete_use_custom_rpc_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        if json["method"] == "provider.custom_list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "providers": [
                            {
                                "id": "local-ai",
                                "name": "Local AI",
                                "auth": "none",
                                "base_url": "http://127.0.0.1:8080/v1",
                                "model_count": 2,
                                "credentials_configured": True,
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"deleted": True}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    listed = provider_management.provider_custom_list(instance)
    deleted = provider_management.provider_custom_delete(instance, "local-ai")

    assert "id: local-ai" in listed.message
    assert deleted.ok is True
    assert "local-ai" in deleted.message
    assert calls == [
        {"method": "provider.custom_list", "params": {}},
        {
            "method": "provider.custom_delete",
            "params": {"provider_id": "local-ai"},
        },
    ]


def test_parse_args_supports_provider_set_key_options() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "set-key",
            "openrouter",
            "sk-or-test",
            "--connection",
            "openrouter:api-key",
            "--refresh-models",
            "--host",
            "localhost",
            "--port",
            "8700",
            "--data-dir",
            "dev",
        ]
    )

    assert args.area == "provider"
    assert args.command == "set-key"
    assert args.provider == "openrouter"
    assert args.connection == "openrouter:api-key"
    assert args.value == "sk-or-test"
    assert args.account is None
    assert args.refresh_models is True
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_parse_args_supports_provider_account_option() -> None:
    set_key_args = cli_main.parse_args(
        ["provider", "set-key", "openrouter", "sk-or-test", "--account", "work"]
    )
    unset_key_args = cli_main.parse_args(
        ["provider", "unset-key", "openrouter", "--account", "work"]
    )
    connect_args = cli_main.parse_args(
        [
            "provider",
            "connect",
            "openai",
            "--connection",
            "openai:subscription",
            "--account",
            "work",
        ]
    )

    assert set_key_args.account == "work"
    assert unset_key_args.account == "work"
    assert connect_args.account == "work"


def test_parse_args_supports_provider_status_options() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "status",
            "openrouter",
            "--connection",
            "openrouter:api-key",
        ]
    )

    assert args.area == "provider"
    assert args.command == "status"
    assert args.provider == "openrouter"
    assert args.connection == "openrouter:api-key"


def test_parse_args_supports_provider_usage_connection_filters() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "usage",
            "--connection",
            "openai:subscription",
            "--connection",
            "github-copilot:oauth",
        ]
    )

    assert args.area == "provider"
    assert args.command == "usage"
    assert args.connection == ["openai:subscription", "github-copilot:oauth"]


def test_provider_set_key_help_is_informative(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(["provider", "set-key", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--refresh-models" in output
    assert "--account" in output
