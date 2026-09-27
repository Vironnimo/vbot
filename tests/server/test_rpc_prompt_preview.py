"""Server RPC prompt preview handler."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    assert response["error"]["code"] == "agent_not_found"
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
    assert response["error"]["code"] == "agent_not_found"
    assert "ghost" in response["error"]["message"]
