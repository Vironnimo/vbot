"""Server RPC prompt preview and handoff handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.projects.resolver import ConfigAgent
from core.tools.availability import ToolAccess
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    StubAdapter,
    StubProject,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_prompt_preview_returns_rendered_text_and_token_estimate(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["text"] == "System for coder"
    assert isinstance(result["tokens"], int)
    assert result["tokens"] > 0
    assert result["estimated"] is True


@pytest.mark.asyncio
async def test_prompt_preview_without_scope_uses_effective_agent_prompt(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "Effective custom system for coder"


@pytest.mark.asyncio
async def test_prompt_preview_explicit_default_scope_uses_default_prompt(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.preview",
            "params": {"agent_id": "coder", "scope": {"type": "default"}},
        },
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "System for coder"


@pytest.mark.asyncio
async def test_prompt_preview_uses_enabled_agent_scope(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.preview",
            "params": {"scope": {"type": "agent", "agent_id": "coder"}},
        },
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "Custom system for coder"


@pytest.mark.asyncio
async def test_prompt_preview_rejects_unknown_agent_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "nobody"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "nobody" in response["error"]["message"]


@pytest.mark.asyncio
async def test_prompt_preview_rejects_missing_agent_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


def _register_project_agent(state: SimpleNamespace, repo: Path) -> None:
    """Wire one project agent + its project anchor into the stub runtime."""
    repo.mkdir()
    state.runtime.projects.add(
        StubProject(
            project_id="vbot",
            display_name="vBot",
            cwd=str(repo),
            auto_load=("CONTEXT.md",),
        )
    )
    state.runtime.agent_resolver.register_project_agent(
        "vbot",
        ConfigAgent(
            id="builder",
            name="Builder",
            model="openai/gpt-5",
            temperature=None,
            tool_access=ToolAccess(mode="all"),
            allowed_skills=["*"],
            tools={},
            body="Imported builder body",
            source_path=repo / ".opencode" / "agents" / "builder.md",
            source_format="opencode",
        ),
    )


@pytest.mark.asyncio
async def test_prompt_preview_project_agent_renders_body_and_project_context(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _register_project_agent(state, tmp_path / "repo")

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "builder@vbot"}},
    )

    assert response["ok"] is True
    text = response["result"]["text"]
    # The config-agent body and the project's cwd both reached the builder — the
    # project-qualified preview now matches a real project-born run.
    assert "body=Imported builder body" in text
    assert "project_cwd=" in text


@pytest.mark.asyncio
async def test_prompt_preview_identity_agent_carries_no_project_context(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    # A bare (identity) address renders no body and no project files, so the
    # output stays byte-identical to before project addressing existed.
    assert response["result"]["text"] == "System for coder"


@pytest.mark.asyncio
async def test_prompt_preview_rooted_identity_agent_renders_project_context(
    tmp_path: Path,
) -> None:
    # An explicit Rooted Identity Agent carries its selected Project context.
    state = make_state(tmp_path, StubAdapter())
    coder_workspace = tmp_path / "coder-workspace"
    coder_workspace.mkdir()
    state.runtime.agents.update("coder", workspace=str(coder_workspace))
    state.runtime.projects.add(
        StubProject(
            project_id="vbot",
            display_name="vBot",
            cwd=str(coder_workspace),
            auto_load=("AGENTS.md",),
        )
    )
    state.runtime.agents.update("coder", root_project_id="vbot")

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    assert "project_cwd=" in response["result"]["text"]


@pytest.mark.asyncio
async def test_prompt_preview_rejects_unknown_project_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    _register_project_agent(state, tmp_path / "repo")

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "ghost@vbot"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "ghost" in response["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_for_same_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    bridged_runs: list[Any] = []
    state.chat_runs.add_run_started_callback(bridged_runs.append)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert result["reply"]
    assert result["data"]["command"] == "handoff"
    assert result["data"]["agent_id"] == "coder"
    new_session_id = result["data"]["session_id"]
    assert isinstance(new_session_id, str)
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    # The process-wide Run-start seam observes both the internal writer and the
    # receiving follow-up without command-specific server bridging.
    target_runs = [run for run in bridged_runs if run.session_id == new_session_id]
    assert len(target_runs) == 1
    assert target_runs[0].agent_id == "coder"
    # Wait for the receiving run to write its user message and finish.
    await target_runs[0].wait()
    new_session = state.runtime.chat_sessions.get("coder", new_session_id)
    new_history = new_session.load()
    user_messages = [message for message in new_history if message.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "OK"
    # The handoff-writing run used a system-reminder note on the source session.
    source_history = state.runtime.chat_sessions.get("coder", "session-one").load()
    assert any(message.role == "note" for message in source_history)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_for_other_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer", name="Reviewer")
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    bridged_runs: list[Any] = []
    state.chat_runs.add_run_started_callback(bridged_runs.append)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff agent:reviewer",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert result["data"]["command"] == "handoff"
    assert result["data"]["agent_id"] == "reviewer"
    new_session_id = result["data"]["session_id"]
    assert state.runtime.agents.get("reviewer").current_session_id == new_session_id
    target_runs = [run for run in bridged_runs if run.session_id == new_session_id]
    assert len(target_runs) == 1
    assert target_runs[0].agent_id == "reviewer"
    await target_runs[0].wait()
    new_session = state.runtime.chat_sessions.get("reviewer", new_session_id)
    new_history = new_session.load()
    user_messages = [message for message in new_history if message.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_with_agent_and_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer", name="Reviewer")
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    bridged_runs: list[Any] = []
    state.chat_runs.add_run_started_callback(bridged_runs.append)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff agent:reviewer don't forget the plates!",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    # The agent: prefix still targets reviewer, and the trailing text rides along
    # as the handoff-writing instruction.
    assert result["data"]["agent_id"] == "reviewer"
    target_session_id = result["data"]["session_id"]
    target_runs = [run for run in bridged_runs if run.session_id == target_session_id]
    assert len(target_runs) == 1
    await target_runs[0].wait()
    # The handoff-writing run runs as an internal note on the source session, so
    # its prompt — with the woven instruction — is persisted there.
    source_history = state.runtime.chat_sessions.get("coder", "session-one").load()
    notes = [message for message in source_history if message.role == "note"]
    assert any("don't forget the plates!" in str(note.content) for note in notes)


def test_build_handoff_prompt_weaves_instruction_and_preserves_base() -> None:
    from core.chat.commands import _build_handoff_prompt

    base = "Write a handoff for the next agent."

    # No instruction returns the base verbatim; the fragment's surrounding
    # whitespace is normalized away so a trailing newline never leaks through.
    assert _build_handoff_prompt(base, None) == base
    assert _build_handoff_prompt(base, "   ") == base
    assert _build_handoff_prompt(f"{base}\n", None) == base

    woven = _build_handoff_prompt(base, "keep the deployment notes")
    assert woven.startswith(base)
    assert "keep the deployment notes" in woven


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_with_missing_target_agent(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", current_session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff agent:ghost",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert "ghost" in result["reply"]
    assert "data" not in result
    # No new session was created and the source session remains current.
    sessions = state.runtime.chat_sessions.list_with_metadata("coder")
    assert [session["id"] for session in sessions] == ["session-one"]
    assert state.runtime.agents.get("coder").current_session_id == "session-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_reject_handoff_command_while_session_run_is_active(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_run_executor(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await state.chat_runs.start(
        agent_id="coder", session_id="session-one", executor=_blocking_run_executor, project_id=None
    )
    await started.wait()

    try:
        response = await dispatch_rpc(
            state,
            {
                "method": method,
                "params": {
                    "agent_id": "coder",
                    "session_id": "session-one",
                    "content": "/handoff",
                },
            },
        )
    finally:
        release.set()
        await active_run.wait()

    assert response["ok"] is True
    assert response["result"]["command_handled"] is True
    assert response["result"]["output"] == "toast"
    assert response["result"]["reply"]
    sessions = state.runtime.chat_sessions.list_with_metadata("coder")
    assert [session["id"] for session in sessions] == ["session-one"]
