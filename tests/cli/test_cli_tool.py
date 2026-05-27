"""Tests for tool catalog CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import tool_management
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


def test_tool_list_posts_rpc_and_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "tool.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "tools": [
                        {"name": "read_file", "description": "Read a file"},
                        {"name": "edit_file", "description": "Edit a file"},
                    ]
                },
            },
        )

    monkeypatch.setattr(tool_management.httpx, "post", fake_post)

    result = tool_management.tool_list(instance)

    assert result == CommandResult(
        ok=True,
        message="tools:\n- read_file  Read a file\n- edit_file  Edit a file",
        instance=instance,
    )


def test_tool_list_rejects_malformed_rpc_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(tool_management.httpx, "post", fake_post)

    result = tool_management.tool_list(instance)

    assert result == CommandResult(
        ok=False,
        message="RPC result missing tools list",
        instance=instance,
    )


def test_run_dispatches_tool_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[ServerInstance] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_list_tools(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(resolved_instance)
        return CommandResult(ok=True, message="tools:\n- read_file  Read a file", instance=instance)

    exit_code = cli_main.run(
        ["tool", "list"],
        resolve=fake_resolve,
        list_tools_fn=fake_list_tools,
    )

    assert exit_code == 0
    assert calls == [instance]
    assert capsys.readouterr().out.splitlines() == ["tools:", "- read_file  Read a file"]
