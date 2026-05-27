"""Tests for vBot CLI agent management commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import agent_management
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


def agent_payload(agent_id: str = "coder") -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": "Coder",
        "model": "openai/gpt-5.2",
        "fallback_model": "anthropic/claude-sonnet-4",
        "workspace": "C:/data/workspace-coder",
        "temperature": 0.4,
        "thinking_effort": "high",
        "allowed_tools": ["*"],
        "allowed_skills": ["debugging"],
        "current_session_id": "session-one",
        "context_window": 256000,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }


def test_agent_list_posts_rpc_and_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "agent.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={"ok": True, "result": {"agents": [agent_payload()]}},
        )

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_list(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "agents:",
        "- id=coder name=Coder model=openai/gpt-5.2 "
        "fallback_model=anthropic/claude-sonnet-4 temperature=0.4 "
        "thinking_effort=high current_session_id=session-one context_window=256000",
    ]


def test_agent_show_posts_rpc_and_formats_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"method": "agent.get", "params": {"id": "coder"}}
        return httpx.Response(200, json={"ok": True, "result": agent_payload()})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_show(instance, "coder")

    assert result.ok is True
    assert result.message.splitlines() == [
        "agent:",
        "id: coder",
        "name: Coder",
        "model: openai/gpt-5.2",
        "fallback_model: anthropic/claude-sonnet-4",
        "workspace: C:/data/workspace-coder",
        "temperature: 0.4",
        "thinking_effort: high",
        "allowed_tools: *",
        "allowed_skills: debugging",
        "current_session_id: session-one",
        "context_window: 256000",
        "created_at: 2026-01-01T00:00:00+00:00",
        "updated_at: 2026-01-02T00:00:00+00:00",
    ]


def test_agent_create_posts_mutable_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.2",
                "allowed_tools": ["read_file"],
                "allowed_skills": ["debugging"],
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"id": "writer"}})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_create(
        instance,
        "writer",
        "Writer",
        {
            "model": "openai/gpt-5.2",
            "allowed_tools": ["read_file"],
            "allowed_skills": ["debugging"],
        },
    )

    assert result == CommandResult(ok=True, message="created writer", instance=instance)


def test_agent_update_posts_null_and_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "agent.update",
            "params": {
                "id": "coder",
                "temperature": None,
                "thinking_effort": "none",
                "allowed_tools": [],
                "allowed_skills": ["vbot-cli"],
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"id": "coder"}})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_update(
        instance,
        "coder",
        {
            "temperature": None,
            "thinking_effort": "none",
            "allowed_tools": [],
            "allowed_skills": ["vbot-cli"],
        },
    )

    assert result == CommandResult(ok=True, message="updated coder", instance=instance)


def test_agent_update_rejects_empty_changes(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = agent_management.agent_update(instance, "coder", {})

    assert result == CommandResult(
        ok=False,
        message=(
            "no agent fields provided; use one of: --name, --model, --fallback-model, "
            "--temperature, --clear-temperature, --thinking-effort, --clear-thinking-effort, "
            "--allowed-tools, --allowed-skills, --current-session-id"
        ),
        instance=instance,
    )


def test_agent_delete_posts_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"method": "agent.delete", "params": {"id": "writer"}}
        return httpx.Response(200, json={"ok": True, "result": {"agent_id": "writer"}})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_delete(instance, "writer")

    assert result == CommandResult(ok=True, message="deleted writer", instance=instance)
