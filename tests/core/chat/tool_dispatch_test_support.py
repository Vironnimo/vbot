"""Shared fixtures and fakes for tool dispatch behavior tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.chat.messages import JsonObject, ToolCall
from core.chat.tool_dispatch import (
    ToolDispatchContext,
)
from core.chat.tool_dispatch import (
    _dispatch_tool_calls as _dispatch_resolved_tool_calls,
)
from core.runs import Run
from core.sessions import ChatSessionManager
from core.skills import SkillRegistry
from core.tools import (
    ToolContract,
    ToolRegistry,
)
from core.tools.availability import ToolAccess


@dataclass(frozen=True)
class _StubAgent:
    id: str
    workspace: Path
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    tools: dict[str, object] | None = None
    memory_prompt_mode: str = "agent_user"

    @property
    def tool_access(self) -> ToolAccess:
        if self.allowed_tools is None or "*" in self.allowed_tools:
            return ToolAccess(mode="all")
        return ToolAccess(mode="selected", allowed=tuple(self.allowed_tools))


class _StubRuntime:
    """Minimal stand-in for the runtime attributes ``_dispatch_tool_calls`` reads."""

    def __init__(self, tools: ToolRegistry, data_dir: Path) -> None:
        self.tools = tools
        self.storage = _StubStorage(data_dir)
        self.system_prompts = _StubSystemPrompts()
        self.extensions = None


class _StubStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _StubSystemPrompts:
    vbot_root = Path.cwd()


def _build_session(tmp_path: Path, agent_id: str = "coder", session_id: str = "session-one") -> Any:
    manager = ChatSessionManager(tmp_path)
    return manager.create(agent_id, session_id=session_id)


def _build_runtime_and_agent(tmp_path: Path, tools: ToolRegistry) -> tuple[Any, _StubAgent]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    agent = _StubAgent(id="coder", workspace=workspace, allowed_tools=["*"])
    runtime = _StubRuntime(tools, tmp_path)
    return runtime, agent


def _decode_tool_result(message_content: object) -> JsonObject:
    assert isinstance(message_content, str)
    return cast(JsonObject, json.loads(message_content))


async def _dispatch_tool_calls(
    runtime: Any,
    agent: Any,
    tool_calls: list[ToolCall],
    session: Any,
    run: Run,
    *,
    nesting_depth: int,
    project_cwd: Path | None = None,
    project_id: str | None = None,
    skill_project_id: str | None = None,
    skill_registry: SkillRegistry | None = None,
    tool_restriction: Sequence[str] | None = None,
    base_allowed_tools: Sequence[str] | None = None,
    session_tool_grants: tuple[str, ...] = (),
    tool_contracts: Mapping[str, ToolContract] | None = None,
    tool_denial_resolver: Callable[[str], str | None] | None = None,
) -> tuple[list[Any], list[JsonObject]]:
    """Adapt runtime-shaped fixtures to the production Run-local context."""
    return await _dispatch_resolved_tool_calls(
        ToolDispatchContext(
            registry=runtime.tools,
            extension_registry=runtime.extensions,
            agent=agent,
            session=session,
            run=run,
            nesting_depth=nesting_depth,
            vbot_root=Path(runtime.system_prompts.vbot_root),
            data_root=Path(runtime.storage.data_dir),
            project_cwd=project_cwd,
            project_id=project_id,
            skill_project_id=skill_project_id,
            skill_registry=skill_registry,
            tool_restriction=tool_restriction,
            base_allowed_tools=base_allowed_tools,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts or {},
            tool_denial_resolver=tool_denial_resolver,
        ),
        tool_calls,
    )
