"""Tests for public Settings path CLI parsing and RPC commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import config_management
from cli import main as cli_main
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


def test_parse_args_supports_public_config_commands() -> None:
    get_args = cli_main.parse_args(["config", "get", "web_search.provider", "--details"])
    unset_args = cli_main.parse_args(["config", "unset", "defaults.agent.temperature"])
    list_args = cli_main.parse_args(["config", "list", "web_search"])

    assert (get_args.command, get_args.path, get_args.details) == (
        "get",
        "web_search.provider",
        True,
    )
    assert (unset_args.command, unset_args.path) == (
        "unset",
        "defaults.agent.temperature",
    )
    assert (list_args.command, list_args.prefix) == ("list", "web_search")


def test_parse_args_supports_atomic_config_patch() -> None:
    args = cli_main.parse_args(
        [
            "config",
            "patch",
            "--set",
            "web_search.provider",
            "searxng",
            "--set",
            "web_search.searxng.base_url",
            "https://search.example",
            "--unset",
            "defaults.agent.temperature",
        ]
    )

    assert args.set_values == [
        ["web_search.provider", "searxng"],
        ["web_search.searxng.base_url", "https://search.example"],
    ]
    assert args.unset_paths == ["defaults.agent.temperature"]


def test_config_raw_posts_get_raw_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"settings": {"server_port": 8420}}})

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_raw(instance)

    assert result == CommandResult(
        ok=True,
        message='{\n  "server_port": 8420\n}',
        instance=instance,
    )
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "settings.get_raw", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_config_effective_posts_settings_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "settings.values", "params": {}}
        return httpx.Response(
            200,
            json={"ok": True, "result": {"settings": {"web_search": {"provider": "brave"}}}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_effective(instance)

    assert result.ok is True
    assert result.message == '{\n  "web_search": {\n    "provider": "brave"\n  }\n}'


def test_config_list_formats_catalog_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "settings.catalog", "params": {"prefix": "web_search"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "settings": [
                        {
                            "path": "web_search.provider",
                            "type": "string",
                            "application": "live",
                            "value": "brave",
                            "source": "default",
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_list(instance, "web_search")

    assert result.message == 'web_search.provider = "brave" (string, live, source=default)'


def test_config_get_returns_effective_json_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "settings.get_path", "params": {"path": "server.port"}}
        return httpx.Response(
            200,
            json={"ok": True, "result": {"setting": {"path": "server.port", "value": 8420}}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_get(instance, "server.port")

    assert result == CommandResult(ok=True, message="8420", instance=instance)


def test_config_describe_formats_source_default_and_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "settings.get_path",
            "params": {"path": "web_search.provider", "allow_missing": True},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "setting": {
                        "path": "web_search.provider",
                        "value": "searxng",
                        "configured": True,
                        "configured_value": "searxng",
                        "source": "configured",
                        "default": "brave",
                        "type": "string",
                        "allowed_values": [
                            "brave",
                            "exa",
                            "firecrawl",
                            "searxng",
                            "serper",
                            "tavily",
                        ],
                        "nullable": False,
                        "unsettable": True,
                        "application": "live",
                        "restart_required": False,
                        "description": "Provider used by web_search.",
                    }
                },
            },
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_describe(instance, "web_search.provider")

    assert "value: searxng" in result.message
    assert "source: configured" in result.message
    assert "default: brave" in result.message
    assert "application: live" in result.message
    assert "restart_required: false" in result.message


def test_config_set_posts_single_atomic_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                    "changed": ["web_search.provider"],
                    "changes": [
                        {
                            "path": "web_search.provider",
                            "value": "searxng",
                            "configured": True,
                            "configured_value": "searxng",
                            "application": "live",
                        }
                    ],
                    "restart_required": False,
                },
            },
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_set(instance, "web_search.provider", "searxng")

    assert calls[0]["json"] == {
        "method": "settings.patch",
        "params": {
            "operations": [{"op": "set", "path": "web_search.provider", "value": "searxng"}]
        },
    }
    assert 'web_search.provider = "searxng"' in result.message
    assert "application: live" in result.message
    assert "restart_required: no" in result.message


def test_config_patch_reports_pending_restart_value(
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
                    "changed": ["server.port"],
                    "changes": [
                        {
                            "path": "server.port",
                            "value": 8420,
                            "configured": True,
                            "configured_value": 9000,
                            "pending_value": 9000,
                            "application": "restart",
                        }
                    ],
                    "restart_required": True,
                },
            },
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_patch(
        instance, [{"op": "set", "path": "server.port", "value": 9000}]
    )

    assert "pending: 9000" in result.message
    assert "application: restart" in result.message
    assert "restart_required: yes" in result.message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("42", 42), ("true", True), ('{"a":1}', {"a": 1}), ("hello world", "hello world")],
)
def test_coerce_config_value(raw: str, expected: Any) -> None:
    assert config_management.coerce_config_value(raw) == expected


def test_config_raw_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error": {"code": "internal_error", "message": "boom"}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_raw(instance)

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("internal_error:")
