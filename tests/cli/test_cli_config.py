"""Tests for config CLI parsing and RPC-backed config commands."""

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


def test_parse_args_supports_config_no_subcommand() -> None:
    args = cli_main.parse_args(
        ["config", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "config"
    assert args.command is None
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_parse_args_supports_config_get() -> None:
    args = cli_main.parse_args(["config", "get", "server_port"])

    assert args.area == "config"
    assert args.command == "get"
    assert args.key == "server_port"


def test_parse_args_supports_config_set() -> None:
    args = cli_main.parse_args(["config", "set", "server_port", "9000"])

    assert args.area == "config"
    assert args.command == "set"
    assert args.key == "server_port"
    assert args.value == "9000"


def test_config_show_posts_get_raw_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"settings": {"server_port": 8420}}})

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_show(instance)

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


def test_config_show_returns_empty_object_when_settings_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"settings": {}}})

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_show(instance)

    assert result == CommandResult(ok=True, message="{}", instance=instance)


def test_config_get_returns_json_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"settings": {"server_port": 8420}}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_get(instance, "server_port")

    assert result == CommandResult(ok=True, message="8420", instance=instance)


def test_config_get_exits_with_error_when_key_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"settings": {"other": 1, "server_port": 8420}}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_get(instance, "server_por")

    assert result == CommandResult(
        ok=False,
        message=(
            "key 'server_por' not found\n"
            "available keys: other, server_port\n"
            "did you mean: server_port"
        ),
        instance=instance,
    )


def test_config_set_posts_set_key_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"settings": {"x": 9000}}})

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_set(instance, "x", 9000)

    assert result.ok is True
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "settings.set_key",
                "params": {"key": "x", "value": 9000},
            },
            "timeout": 10.0,
        }
    ]


def test_config_set_confirms_with_key_equals_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"settings": {"x": 9000}}})

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_set(instance, "x", 9000)

    assert result == CommandResult(ok=True, message="x = 9000", instance=instance)


def test_coerce_config_value_parses_int() -> None:
    assert config_management.coerce_config_value("42") == 42


def test_coerce_config_value_parses_bool() -> None:
    assert config_management.coerce_config_value("true") is True


def test_coerce_config_value_parses_json_object() -> None:
    assert config_management.coerce_config_value('{"a":1}') == {"a": 1}


def test_coerce_config_value_falls_back_to_string() -> None:
    assert config_management.coerce_config_value("hello world") == "hello world"


def test_config_show_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error": {"code": "internal_error", "message": "boom"}},
        )

    monkeypatch.setattr(config_management.httpx, "post", fake_post)

    result = config_management.config_show(instance)

    assert result == CommandResult(
        ok=False,
        message="internal_error: boom",
        instance=instance,
    )
