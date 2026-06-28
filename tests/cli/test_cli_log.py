"""Tests for log CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import log_management
from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_log_list_posts_rpc_and_formats_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "log.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"files": ["2026-05-11", "2026-05-10"], "default_file": "2026-05-11"},
            },
        )

    monkeypatch.setattr(log_management.httpx, "post", fake_post)

    result = log_management.log_list(instance)

    assert result == CommandResult(
        ok=True,
        message="logs: default=2026-05-11\n- 2026-05-11\n- 2026-05-10",
        instance=instance,
    )


def test_log_read_posts_rpc_and_formats_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "log.read", "params": {"file": "2026-05-11"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "file": "2026-05-11",
                    "cursor": "cursor-1",
                    "entries": [
                        {
                            "timestamp": "2026-05-11 09:00:00",
                            "level": "info",
                            "logger_name": "vbot.server.app",
                            "message": "Ready",
                            "continuation": "trace line",
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(log_management.httpx, "post", fake_post)

    result = log_management.log_read(instance, "2026-05-11")

    assert result == CommandResult(
        ok=True,
        message=(
            "log: 2026-05-11\n"
            "cursor: cursor-1\n"
            "- 2026-05-11 09:00:00 [info] vbot.server.app - Ready\n"
            "  trace line"
        ),
        instance=instance,
    )


def test_run_dispatches_log_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[ServerInstance, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_read_log(resolved_instance: ServerInstance, file_name: str) -> CommandResult:
        calls.append((resolved_instance, file_name))
        return CommandResult(ok=True, message="log: 2026-05-11", instance=instance)

    exit_code = cli_main.run(
        ["log", "read", "2026-05-11"],
        resolve=fake_resolve,
        read_log_fn=fake_read_log,
    )

    assert exit_code == 0
    assert calls == [(instance, "2026-05-11")]
    assert capsys.readouterr().out.splitlines() == ["log: 2026-05-11"]
