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


def test_provider_list_posts_connection_list_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result.ok is True
    assert result.instance is instance
    assert result.message.strip()
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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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
                            "accounts": [
                                {
                                    "id": "default",
                                    "usable": True,
                                    "source": "process_env",
                                    "credential_key": "OPENAI_API_KEY",
                                },
                                {
                                    "id": "work",
                                    "usable": False,
                                    "source": "data_dir",
                                    "credential_key": "OPENAI_API_KEY__WORK",
                                },
                            ],
                        },
                        {
                            "id": "openrouter:main",
                            "provider_id": "openrouter",
                            "type": "api_key",
                            "label": "OpenRouter",
                            "usable": False,
                            "accounts": [],
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result.ok is True
    assert "openai:default" in result.message
    assert "openrouter:main" in result.message
    assert "usable: yes" in result.message
    assert "usable: no" in result.message
    assert "default" in result.message
    assert "process_env" in result.message
    assert "work" in result.message
    assert "data_dir" in result.message


def test_provider_list_returns_empty_message_when_no_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "connection.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result.ok is True
    assert result.instance is instance
    assert result.message.strip()


def test_provider_list_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "connection.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            500,
            json={"ok": False, "error": {"code": "provider_error", "message": "boom"}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_list(instance)

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("provider_error:")


def test_provider_status_filters_provider_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "connection.list", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "connections": [
                        {
                            "id": "openai:api-key",
                            "provider_id": "openai",
                            "type": "api_key",
                            "label": "API Key",
                            "usable": True,
                        },
                        {
                            "id": "openrouter:api-key",
                            "provider_id": "openrouter",
                            "type": "api_key",
                            "label": "API Key",
                            "usable": False,
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_status(instance, "openrouter")

    assert result.ok is True
    assert "openrouter:api-key" in result.message
    assert "openai:api-key" not in result.message


def test_provider_status_returns_not_found_for_missing_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_status(instance, "openrouter", "openrouter:api-key")

    assert result.ok is False
    assert result.instance is instance
    assert "openrouter:api-key" in result.message


def test_provider_status_not_found_includes_candidates_and_suggestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "connections": [
                        {
                            "id": "openrouter:api-key",
                            "provider_id": "openrouter",
                            "type": "api_key",
                            "label": "API Key",
                            "usable": True,
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_status(instance, "openruter")

    assert result.ok is False
    assert result.instance is instance
    assert "openruter" in result.message
    assert "openrouter" in result.message


def test_provider_usage_posts_filter_and_formats_live_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.usage",
            "params": {"connections": ["openai:subscription"]},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "generated_at": "2026-07-20T16:00:00Z",
                    "providers": [
                        {
                            "connection": "openai:subscription",
                            "display_name": "OpenAI",
                            "plan": "Plus",
                            "windows": [
                                {
                                    "label": "5h",
                                    "used_percent": 42.5,
                                    "reset_at": "2026-07-20T18:00:00Z",
                                },
                                {"label": "Week", "used_percent": 12.0, "reset_at": None},
                            ],
                            "error": None,
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage(instance, ["openai:subscription"])

    assert result.ok is True
    assert result.message.splitlines() == [
        "provider usage:",
        "generated_at: 2026-07-20T16:00:00Z",
        "- OpenAI (openai:subscription)  plan: Plus",
        "  - 5h: used=42.5% remaining=57.5% reset_at=2026-07-20T18:00:00Z",
        "  - Week: used=12% remaining=88% reset_at=-",
    ]


def test_provider_usage_reports_provider_error_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "provider.usage", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "generated_at": "2026-07-20T16:00:00Z",
                    "providers": [
                        {
                            "connection": "github-copilot:oauth",
                            "display_name": "GitHub Copilot",
                            "plan": None,
                            "windows": [],
                            "error": "Network error",
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage(instance)

    assert result.message.splitlines()[-2:] == [
        "- GitHub Copilot (github-copilot:oauth)  plan: -",
        "  error: Network error",
    ]


def test_provider_set_key_posts_set_key_rpc_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "account": "default",
                    "credential_key": "OPENROUTER_API_KEY",
                    "configured": True,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_key(
        instance,
        provider_id="openrouter",
        connection_id="openrouter:api-key",
        value="sk-or-test",
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openrouter:api-key" in result.message
    assert "OPENROUTER_API_KEY" in result.message
    assert "account: default" in result.message
    assert "sk-or-test" not in result.message
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "provider.set_key",
                "params": {
                    "provider_id": "openrouter",
                    "value": "sk-or-test",
                    "connection_id": "openrouter:api-key",
                },
            },
            "timeout": 10.0,
        }
    ]


def test_provider_set_key_passes_account_and_reports_derived_key(
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
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "account": "work",
                    "credential_key": "OPENROUTER_API_KEY__WORK",
                    "configured": True,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_key(
        instance,
        provider_id="openrouter",
        value="sk-or-work",
        account="work",
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openrouter:api-key" in result.message
    assert "OPENROUTER_API_KEY__WORK" in result.message
    assert "account: work" in result.message
    assert calls == [
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-work", "account": "work"},
        }
    ]


def test_provider_set_key_can_refresh_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        if json["method"] == "provider.set_key":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "connection_id": "openrouter:api-key",
                        "account": "default",
                        "credential_key": "OPENROUTER_API_KEY",
                    },
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"provider_id": "openrouter", "model_count": 42}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_key(
        instance,
        provider_id="openrouter",
        value="sk-or-test",
        refresh_models=True,
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openrouter:api-key" in result.message
    assert "OPENROUTER_API_KEY" in result.message
    assert "42" in result.message
    assert calls == [
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-test"},
        },
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    ]


def test_parse_args_supports_provider_unset_key_options() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "unset-key",
            "openrouter",
            "--connection",
            "openrouter:api-key",
        ]
    )

    assert args.area == "provider"
    assert args.command == "unset-key"
    assert args.provider == "openrouter"
    assert args.connection == "openrouter:api-key"


def test_provider_unset_key_posts_unset_key_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "account": "default",
                    "credential_key": "OPENROUTER_API_KEY",
                    "removed": True,
                    "configured": False,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_unset_key(instance, provider_id="openrouter")

    assert result.ok is True
    assert result.instance is instance
    assert "openrouter:api-key" in result.message
    assert "OPENROUTER_API_KEY" in result.message
    assert "account: default" in result.message
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "provider.unset_key",
                "params": {"provider_id": "openrouter"},
            },
            "timeout": 10.0,
        }
    ]


def test_provider_unset_key_passes_account_through(
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
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "account": "work",
                    "credential_key": "OPENROUTER_API_KEY__WORK",
                    "removed": True,
                    "configured": False,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_unset_key(
        instance, provider_id="openrouter", account="work"
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openrouter:api-key" in result.message
    assert "OPENROUTER_API_KEY__WORK" in result.message
    assert "account: work" in result.message
    assert calls == [
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter", "account": "work"},
        }
    ]


def test_provider_unset_key_reports_remaining_process_env_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "account": "default",
                    "credential_key": "OPENROUTER_API_KEY",
                    "removed": False,
                    "configured": True,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_unset_key(instance, provider_id="openrouter")

    assert result.ok is True
    assert "OPENROUTER_API_KEY" in result.message
    assert "process environment" in result.message


def test_parse_args_supports_provider_oauth_commands() -> None:
    connect_args = cli_main.parse_args(
        ["provider", "connect", "openai", "--connection", "openai:subscription"]
    )
    status_args = cli_main.parse_args(
        ["provider", "connect-status", "openai", "--connection", "openai:subscription"]
    )

    assert connect_args.command == "connect"
    assert connect_args.provider == "openai"
    assert connect_args.connection == "openai:subscription"
    assert status_args.command == "connect-status"


def test_provider_connect_prints_device_flow_instructions(
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
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://example.com/device",
                    "expires_in": 900,
                    "account": "default",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(instance, "openai", "openai:subscription")

    assert result.ok is True
    assert "openai:subscription" in result.message
    assert "account: default" in result.message
    assert "user_code: ABCD-1234" in result.message
    assert "verification_uri: https://example.com/device" in result.message
    assert "expires_in_seconds: 900" in result.message
    assert "provider connect-status openai --connection openai:subscription" in result.message
    assert calls == [
        {
            "method": "provider.connect",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
    ]


def test_provider_connect_passes_account_and_suggests_account_status_command(
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
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://example.com/device",
                    "expires_in": 900,
                    "account": "work",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result.ok is True
    lines = result.message.splitlines()
    assert "openai:subscription" in result.message
    assert "account: work" in result.message
    assert lines[-1].endswith(
        "provider connect-status openai --connection openai:subscription --account work"
    )
    assert calls == [
        {
            "method": "provider.connect",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
    ]


def test_provider_disconnect_posts_disconnect_rpc(
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
                    "provider_id": "openai",
                    "connection_id": "openai:subscription",
                    "account": "default",
                    "status": "disconnected",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_disconnect(instance, "openai", "openai:subscription")

    assert result.ok is True
    assert result.instance is instance
    assert "openai:subscription" in result.message
    assert "account: default" in result.message
    assert calls == [
        {
            "method": "provider.disconnect",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
    ]


def test_provider_disconnect_passes_account_through(
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
                    "provider_id": "openai",
                    "connection_id": "openai:subscription",
                    "account": "work",
                    "status": "disconnected",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_disconnect(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openai:subscription" in result.message
    assert "account: work" in result.message
    assert calls == [
        {
            "method": "provider.disconnect",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
    ]


def test_provider_connect_status_formats_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.connection_status",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"connected": True, "flow_active": False, "account": "default"},
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect_status(instance, "openai", "openai:subscription")

    assert result == CommandResult(
        ok=True,
        message="openai:subscription: account=default connected=yes flow_active=no",
        instance=instance,
    )


def test_provider_connect_status_passes_account_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.connection_status",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"connected": False, "flow_active": True, "account": "work"},
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect_status(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result == CommandResult(
        ok=True,
        message="openai:subscription: account=work connected=no flow_active=yes",
        instance=instance,
    )


def test_provider_oauth_commands_surface_rpc_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {
                    "code": "oauth_not_supported",
                    "message": "provider connection 'openai:api-key' is not an OAuth connection",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(instance, "openai", "openai:api-key")

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("oauth_not_supported:")
    assert "openai:api-key" in result.message


def test_run_provider_set_key_dispatches_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path, port=8765)
    calls: list[tuple[str, Any]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_set_key(
        resolved_instance: ServerInstance,
        provider_id: str,
        value: str,
        connection_id: str | None,
        refresh_models: bool,
        account: str | None,
    ) -> CommandResult:
        calls.append(
            (
                "provider.set_key",
                {
                    "instance": resolved_instance,
                    "provider_id": provider_id,
                    "value": value,
                    "connection_id": connection_id,
                    "refresh_models": refresh_models,
                    "account": account,
                },
            )
        )
        return CommandResult(
            ok=True,
            message="set openrouter:api-key credential OPENROUTER_API_KEY",
            instance=resolved_instance,
        )

    exit_code = cli_main.run(
        [
            "provider",
            "set-key",
            "openrouter",
            "sk-or-test",
            "--connection",
            "openrouter:api-key",
            "--account",
            "work",
            "--host",
            "localhost",
            "--port",
            "8765",
            "--data-dir",
            "data",
        ],
        resolve=fake_resolve,
        set_provider_key=fake_set_key,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        (
            "provider.set_key",
            {
                "instance": instance,
                "provider_id": "openrouter",
                "value": "sk-or-test",
                "connection_id": "openrouter:api-key",
                "refresh_models": False,
                "account": "work",
            },
        ),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "set openrouter:api-key credential OPENROUTER_API_KEY"
    ]


def test_parse_args_supports_provider_enable_and_disable() -> None:
    enable_args = cli_main.parse_args(["provider", "enable", "ollama"])
    disable_args = cli_main.parse_args(
        ["provider", "disable", "ollama", "--connection", "ollama:local"]
    )

    assert enable_args.area == "provider"
    assert enable_args.command == "enable"
    assert enable_args.provider == "ollama"
    assert enable_args.connection is None
    assert disable_args.command == "disable"
    assert disable_args.connection == "ollama:local"


def test_provider_set_enabled_with_explicit_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                    "provider_id": "ollama",
                    "connection_id": "ollama:local",
                    "enabled": True,
                    "configured": True,
                    "reachable": True,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_enabled(instance, "ollama", True, "ollama:local")

    assert result.ok is True
    assert "ollama:local" in result.message
    assert "reachable" in result.message
    assert calls == [
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": True,
            },
        }
    ]


def test_provider_set_enabled_reports_unreachable_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "ollama",
                    "connection_id": "ollama:local",
                    "enabled": True,
                    "configured": True,
                    "reachable": False,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_enabled(instance, "ollama", True, "ollama:local")

    assert result.ok is True
    assert "ollama:local" in result.message
    assert "not reachable" in result.message


def test_provider_set_enabled_resolves_single_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --connection, a single-connection provider resolves automatically."""
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        if json["method"] == "connection.list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "connections": [
                            {
                                "id": "openrouter:api-key",
                                "provider_id": "openrouter",
                                "type": "api_key",
                                "label": "API Key",
                                "enabled": True,
                                "usable": True,
                                "accounts": [],
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "enabled": False,
                    "configured": True,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_enabled(instance, "openrouter", False)

    assert result.ok is True
    assert "openrouter:api-key" in result.message
    assert calls[0]["method"] == "connection.list"
    assert calls[1] == {
        "method": "connection.set_enabled",
        "params": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "enabled": False,
        },
    }


def test_provider_set_enabled_requires_connection_for_multi_connection_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "connections": [
                        {
                            "id": "ollama:local",
                            "provider_id": "ollama",
                            "type": "none",
                            "label": "Local",
                            "enabled": False,
                            "usable": False,
                            "accounts": [],
                        },
                        {
                            "id": "ollama:cloud",
                            "provider_id": "ollama",
                            "type": "api_key",
                            "label": "Ollama Cloud",
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_enabled(instance, "ollama", True)

    assert result.ok is False
    assert "pass --connection" in result.message
    assert "ollama:cloud" in result.message
    assert "ollama:local" in result.message


def test_provider_set_enabled_reports_missing_credential_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "ollama",
                    "connection_id": "ollama:cloud",
                    "enabled": True,
                    "configured": False,
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_set_enabled(instance, "ollama", True, "ollama:cloud")

    assert result.ok is True
    assert "provider set-key ollama" in result.message


def test_provider_usage_history_formats_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.usage_history",
            "params": {"since": "2026-08-01T00:00:00Z"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "generated_at": "2026-08-25T12:00:00+00:00",
                    "samples": [
                        {
                            "sampled_at": "2026-08-01T10:00:00+00:00",
                            "providers": [
                                {
                                    "connection": "openai:subscription",
                                    "windows": [
                                        {"label": "5h", "used_percent": 42.0, "reset_at": "-"}
                                    ],
                                },
                                {"connection": "broken:api-key", "error": "timeout"},
                            ],
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history(instance, since="2026-08-01T00:00:00Z")

    assert result.ok is True
    assert result.message.splitlines() == [
        "provider usage history:",
        "generated_at: 2026-08-25T12:00:00+00:00",
        "- 2026-08-01T10:00:00+00:00",
        "  openai:subscription: - 5h: used=42% remaining=58% reset_at=-",
        "  broken:api-key: error: timeout",
    ]


def test_provider_usage_history_reports_empty_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"generated_at": "t0", "samples": []}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history(instance)

    assert result.ok is True
    assert "no recorded usage samples" in result.message


def test_provider_usage_history_clear_requires_confirmation(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = provider_management.provider_usage_history_clear(instance, False)

    assert result.ok is False
    assert "--yes" in result.message


def test_provider_usage_history_clear_posts_clear_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"deleted_samples": 12, "deleted_files": 2}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history_clear(instance, True)

    assert result.ok is True
    assert result.message == "deleted provider usage history: 12 samples across 2 files"
    assert calls == [{"method": "provider.usage_history.clear", "params": {}}]


def test_parse_args_supports_usage_history_commands() -> None:
    history = cli_main.parse_args(["provider", "usage-history"])
    clear = cli_main.parse_args(["provider", "usage-history-clear", "--yes"])

    assert (history.command, history.since, history.until) == ("usage-history", None, None)
    assert (clear.command, clear.yes) == ("usage-history-clear", True)
