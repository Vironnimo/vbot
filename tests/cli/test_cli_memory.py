"""Tests for the pinned Memory CLI area."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import memory_management
from cli.server_management import ServerInstance
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


def memory_response(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    scopes = {
        "agent": [{"id": 1, "scope": "agent", "content": "Keep answers short"}],
        "user": [],
    }
    result: dict[str, Any] = {"agent_id": "assistant", "scopes": scopes}
    if entry is not None:
        result["entry"] = entry
    return result


def test_parse_args_memory_add_defaults_to_agent_scope() -> None:
    args = cli_main.parse_args(["memory", "add", "assistant", "--content", "Prefers brevity"])

    assert args.area == "memory"
    assert args.command == "add"
    assert args.agent == "assistant"
    assert args.scope == "agent"
    assert args.content == "Prefers brevity"


def test_parse_args_memory_replace_and_remove() -> None:
    replace = cli_main.parse_args(
        ["memory", "replace", "assistant", "--scope", "user", "3", "--content", "Updated"]
    )
    remove = cli_main.parse_args(["memory", "remove", "assistant", "3", "--yes"])

    assert (replace.command, replace.scope, replace.entry_id) == ("replace", "user", 3)
    assert (remove.command, remove.entry_id, remove.yes) == ("remove", 3, True)


def test_memory_list_formats_both_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "memory.list", "params": {"agent_id": "assistant"}}
        return httpx.Response(200, json={"ok": True, "result": memory_response()})

    monkeypatch.setattr(memory_management.httpx, "post", fake_post)

    result = memory_management.memory_list(instance, "assistant")

    assert result.ok is True
    assert result.message.splitlines() == [
        "pinned memory for assistant:",
        "agent scope:",
        "  #1: Keep answers short",
        "user scope:",
        "  (no entries)",
    ]


def test_memory_add_posts_entry_and_reports_result(
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
                "result": memory_response(entry={"id": 2, "scope": "user", "content": "New fact"}),
            },
        )

    monkeypatch.setattr(memory_management.httpx, "post", fake_post)

    result = memory_management.memory_add(instance, "assistant", "user", "New fact")

    assert result.ok is True
    assert result.message.splitlines() == [
        "added memory entry in assistant (scope: user)",
        "#2: New fact",
        "remaining entries: agent=1 user=0",
    ]
    assert calls == [
        {
            "method": "memory.add",
            "params": {"agent_id": "assistant", "scope": "user", "content": "New fact"},
        }
    ]


def test_memory_remove_requires_confirmation(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = memory_management.memory_remove(instance, "assistant", "agent", 1, False)

    assert result.ok is False
    assert "--yes" in result.message


def test_memory_commands_fail_on_rpc_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error": {"code": "invalid_request", "message": "unknown agent"}},
        )

    monkeypatch.setattr(memory_management.httpx, "post", fake_post)

    result = memory_management.memory_list(instance, "ghost")

    assert result.ok is False
    assert result.message.startswith("invalid_request:")
