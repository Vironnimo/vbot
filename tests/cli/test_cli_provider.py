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


def test_parse_args_supports_provider_set_key_options() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "set-key",
            "--provider",
            "openrouter",
            "--connection",
            "openrouter:api-key",
            "--value",
            "sk-or-test",
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
    assert args.refresh_models is True
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_parse_args_supports_provider_status_options() -> None:
    args = cli_main.parse_args(
        [
            "provider",
            "status",
            "--provider",
            "openrouter",
            "--connection",
            "openrouter:api-key",
        ]
    )

    assert args.area == "provider"
    assert args.command == "status"
    assert args.provider == "openrouter"
    assert args.connection == "openrouter:api-key"


def test_provider_set_key_help_is_informative(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(["provider", "set-key", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Write an API key to the target data-dir .env" in output
    assert "--refresh-models" in output


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


def test_provider_status_filters_provider_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"connections": []}})

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_status(instance, "openrouter", "openrouter:api-key")

    assert result == CommandResult(
        ok=False,
        message="provider status not found: openrouter:api-key",
        instance=instance,
    )


def test_provider_status_not_found_includes_candidates_and_suggestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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

    assert result == CommandResult(
        ok=False,
        message=(
            "provider status not found: openruter\n"
            "available providers: openrouter\n"
            "did you mean: openrouter"
        ),
        instance=instance,
    )


def test_provider_set_key_posts_set_key_rpc_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
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

    assert result == CommandResult(
        ok=True,
        message="set openrouter:api-key credential OPENROUTER_API_KEY",
        instance=instance,
    )
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


def test_provider_set_key_can_refresh_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        if json["method"] == "provider.set_key":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "connection_id": "openrouter:api-key",
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

    assert result == CommandResult(
        ok=True,
        message=(
            "set openrouter:api-key credential OPENROUTER_API_KEY\nrefreshed openrouter (42 models)"
        ),
        instance=instance,
    )
    assert calls == [
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-test"},
        },
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    ]


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
            "--provider",
            "openrouter",
            "--connection",
            "openrouter:api-key",
            "--value",
            "sk-or-test",
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
            },
        ),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "set openrouter:api-key credential OPENROUTER_API_KEY"
    ]
