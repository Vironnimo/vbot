"""Tests for debug CLI parsing, RPC commands, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import debug_management
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


def test_parse_args_supports_debug_trace_and_probe() -> None:
    trace_args = cli_main.parse_args(["debug", "trace", "abc123"])
    probe_args = cli_main.parse_args(["debug", "probe", "openai", "--connection", "openai:api-key"])

    assert trace_args.area == "debug"
    assert trace_args.command == "trace"
    assert trace_args.trace_id == "abc123"
    assert probe_args.command == "probe"
    assert probe_args.provider == "openai"
    assert probe_args.connection == "openai:api-key"


def test_debug_status_formats_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "debug.status", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "enabled": True,
                    "trace_limit": 50,
                    "trace_count": 3,
                    "data_directory": "C:/data",
                },
            },
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_status(instance)

    assert result == CommandResult(
        ok=True,
        message="enabled=yes trace_limit=50 trace_count=3 data_directory=C:/data",
        instance=instance,
    )


def test_debug_trace_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "debug.trace_list", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "traces": [
                        {
                            "trace_id": "abc123",
                            "type": "model_probe",
                            "timestamp": "2026-06-11T08:00:00+00:00",
                            "duration_ms": 412,
                            "provider_id": "openai",
                            "model_id": "",
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_trace_list(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "traces:",
        (
            "- id=abc123 type=model_probe timestamp=2026-06-11T08:00:00+00:00 "
            "duration_ms=412 provider=openai model=-"
        ),
    ]


def test_debug_trace_show_dumps_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "debug.trace_get", "params": {"trace_id": "abc123"}}
        return httpx.Response(
            200,
            json={"ok": True, "result": {"trace": {"trace_id": "abc123", "type": "model_probe"}}},
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_trace_show(instance, "abc123")

    assert result.ok is True
    assert result.message.splitlines() == [
        "{",
        '  "trace_id": "abc123",',
        '  "type": "model_probe"',
        "}",
    ]


def test_debug_trace_clear_posts_clear_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"cleared": True}})

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_trace_clear(instance)

    assert result == CommandResult(ok=True, message="cleared all debug traces", instance=instance)
    assert calls == [{"method": "debug.trace_clear", "params": {}}]


def test_debug_model_probe_formats_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "debug.model_probe",
            "params": {"provider_id": "openai", "connection_id": "openai:api-key"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "trace_id": "abc123",
                    "status_code": 200,
                    "duration_ms": 412,
                    "raw_response": "{}",
                    "model_preview": {
                        "model_count": 2,
                        "models": [
                            {"id": "gpt-5.2", "name": "GPT-5.2"},
                            {"id": "gpt-4o", "name": "GPT-4o"},
                        ],
                    },
                },
            },
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_model_probe(instance, "openai", "openai:api-key")

    assert result.ok is True
    assert result.message.splitlines() == [
        "probe openai: status_code=200 duration_ms=412 trace_id=abc123",
        "model_count: 2",
        "first 2 models:",
        "- gpt-5.2",
        "- gpt-4o",
        "full raw response stored in the trace; read it with: debug trace abc123",
    ]


def test_debug_commands_surface_disabled_error(
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
                "error": {"code": "domain_error", "message": "debug mode is not enabled"},
            },
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    result = debug_management.debug_trace_list(instance)

    assert result == CommandResult(
        ok=False, message="domain_error: debug mode is not enabled", instance=instance
    )


def test_run_dispatches_debug_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "enabled": False,
                    "trace_limit": 50,
                    "trace_count": 0,
                    "data_directory": "C:/data",
                },
            },
        )

    monkeypatch.setattr(debug_management.httpx, "post", fake_post)

    exit_code = cli_main.run(["debug", "status", "--port", "8765"], resolve=fake_resolve)

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "enabled=no trace_limit=50 trace_count=0 data_directory=C:/data"
    ]
