"""Tests for cli provider."""

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
    assert "provider status ollama --connection ollama:cloud" in result.message
