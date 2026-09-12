"""Tests for cli provider credentials."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import provider_management
from cli.server_management import CommandResult, ServerInstance
from tests.cli.cli_provider_test_support import (
    make_instance,
)


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


def test_set_key_preserves_save_and_reports_discovery_failure(tmp_path, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        result = (
            {"connection_id": "openai:api-key", "credential_key": "OPENAI_API_KEY"}
            if len(calls) == 1
            else {
                "provider_id": "openai",
                "errors": [{"connection_id": "openai:api-key", "error": "discovery-sentinel"}],
            }
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(provider_management.httpx, "post", post)
    result = provider_management.provider_set_key(
        make_instance(tmp_path), "openai", "secret-sentinel", refresh_models=True
    )
    assert not result.ok
    assert [call["method"] for call in calls] == ["provider.set_key", "model.refresh_db"]
    assert "OPENAI_API_KEY" in result.message
    assert "discovery-sentinel" in result.message
    assert "secret-sentinel" not in result.message
