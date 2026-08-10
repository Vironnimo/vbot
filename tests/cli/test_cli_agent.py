"""Tests for vBot CLI agent management commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import agent_management
from cli import main as cli_main
from cli.server_management import ServerInstance
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
        "root_project_id": "vbot",
        "temperature": 0.4,
        "thinking_effort": "high",
        "memory_prompt_mode": "agent_user",
        "custom_system_prompt_enabled": False,
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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "agent.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "agents": [agent_payload("writer"), agent_payload()],
                    "order_revision": 3,
                },
            },
        )

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_list(instance)

    assert result.ok is True
    assert result.message.splitlines()[1:] == [
        "- id=writer name=Coder model=openai/gpt-5.2 "
        "fallback_model=anthropic/claude-sonnet-4 temperature=0.4 "
        "thinking_effort=high current_session_id=session-one context_window=256000",
        "- id=coder name=Coder model=openai/gpt-5.2 "
        "fallback_model=anthropic/claude-sonnet-4 temperature=0.4 "
        "thinking_effort=high current_session_id=session-one context_window=256000",
    ]


def test_agent_show_posts_rpc_and_formats_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "agent.get", "params": {"id": "coder"}}
        return httpx.Response(200, json={"ok": True, "result": agent_payload()})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_show(instance, "coder")

    assert result.ok is True
    assert result.message.splitlines()[1:] == [
        "id: coder",
        "name: Coder",
        "model: openai/gpt-5.2",
        "fallback_model: anthropic/claude-sonnet-4",
        "workspace: C:/data/workspace-coder",
        "project: vbot",
        "temperature: 0.4",
        "thinking_effort: high",
        "memory_prompt_mode: agent_user",
        "custom_system_prompt_enabled: no",
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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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

    assert result.ok is True
    assert result.instance is instance
    assert "writer" in result.message


def test_agent_update_posts_null_and_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "agent.update",
            "params": {
                "id": "coder",
                "temperature": None,
                "thinking_effort": "none",
                "allowed_tools": [],
                "allowed_skills": ["vbot-cli"],
                "workspace": "C:/agents/coder",
                "copy_workspace_identity_files": True,
                "root_project_id": "vbot",
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
            "workspace": "C:/agents/coder",
            "copy_workspace_identity_files": True,
            "root_project_id": "vbot",
        },
    )

    assert result.ok is True
    assert result.instance is instance
    assert "coder" in result.message


def test_agent_update_rejects_empty_changes(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = agent_management.agent_update(instance, "coder", {})

    assert result.ok is False
    assert result.instance is instance
    for option in (
        "--name",
        "--model",
        "--fallback-model",
        "--temperature",
        "--thinking-effort",
        "--memory-prompt-mode",
        "--allowed-tools",
        "--allowed-skills",
        "--workspace",
        "--project",
        "--current-session-id",
    ):
        assert option in result.message


def test_agent_update_requires_workspace_target_when_copying_files(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = agent_management.agent_update(
        instance,
        "coder",
        {"copy_workspace_identity_files": True},
    )

    assert result.ok is False
    assert result.instance is instance
    assert "--copy-workspace-files" in result.message
    assert "--workspace" in result.message
    assert "--default-workspace" in result.message


def test_agent_delete_posts_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "agent.delete", "params": {"id": "writer"}}
        return httpx.Response(200, json={"ok": True, "result": {"agent_id": "writer"}})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_delete(instance, "writer")

    assert result.ok is True
    assert result.instance is instance
    assert "writer" in result.message


def test_agent_rename_posts_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "agent.rename",
            "params": {"id": "writer", "new_id": "researcher"},
        }
        return httpx.Response(200, json={"ok": True, "result": {"id": "researcher"}})

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_rename(instance, "writer", "researcher")

    assert result.ok is True
    assert result.instance is instance
    assert "writer" in result.message
    assert "researcher" in result.message


def test_agent_rename_rejects_same_id_without_rpc(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = agent_management.agent_rename(instance, "writer", "writer")

    assert result.ok is False
    assert result.instance is instance
    assert result.message.strip()


def test_agent_update_maps_clear_delegation_and_policy_flags() -> None:
    args = cli_main.parse_args(
        [
            "agent",
            "update",
            "coder",
            "--clear-model",
            "--clear-fallback-model",
            "--subagent-allow",
            "reviewer",
            "librarian",
            "--compaction-policy",
            '{"enabled":false}',
        ]
    )

    assert cli_main._agent_changes_from_args(args) == {
        "model": "",
        "fallback_model": "",
        "tools": {"subagent": {"allowed_agents": ["reviewer", "librarian"]}},
        "compaction_policy": {"enabled": False},
    }


def test_agent_create_full_response_confirms_saved_state(
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
                    "id": "librarian",
                    "name": "Librarian",
                    "model": "openai/gpt-5",
                    "fallback_model": "",
                    "workspace": "C:/agents/librarian/workspace",
                    "default_workspace": "C:/agents/librarian/workspace",
                    "root_project_id": "second-brain",
                    "temperature": 0.2,
                    "thinking_effort": "high",
                    "memory_prompt_mode": "full",
                    "custom_system_prompt_enabled": True,
                    "allowed_tools": ["read"],
                    "allowed_skills": ["vbot-cli"],
                    "tools": {"subagent": {"allowed_agents": []}},
                    "compaction_policy": None,
                    "effective_compaction_policy": {"enabled": True},
                    "effective": {"model": {"value": "openai/gpt-5", "source": "agent"}},
                    "current_session_id": "session-1",
                    "context_window": 128000,
                    "created_at": "now",
                    "updated_at": "now",
                },
            },
        )

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_create(instance, "librarian", "Librarian", {})

    assert result.ok is True
    assert "workspace: C:/agents/librarian/workspace" in result.message
    assert "project: second-brain" in result.message
    assert 'effective_sources: {"model":"agent"}' in result.message
    assert "vbot model list --task chat" not in result.message


def test_agent_create_warns_and_gives_recovery_when_no_model_is_effective(
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
                    "id": "librarian",
                    "name": "Librarian",
                    "model": "",
                    "fallback_model": "",
                    "workspace": "C:/agents/librarian/workspace",
                    "root_project_id": None,
                    "temperature": None,
                    "thinking_effort": None,
                    "memory_prompt_mode": "full",
                    "custom_system_prompt_enabled": False,
                    "allowed_tools": [],
                    "allowed_skills": [],
                    "current_session_id": "session-1",
                    "context_window": None,
                    "created_at": "now",
                    "updated_at": "now",
                },
            },
        )

    monkeypatch.setattr(agent_management.httpx, "post", fake_post)

    result = agent_management.agent_create(instance, "librarian", "Librarian", {})

    assert result.ok is True
    assert "vbot model list --task chat" in result.message
    assert "vbot agent update librarian --model <model-id>" in result.message
