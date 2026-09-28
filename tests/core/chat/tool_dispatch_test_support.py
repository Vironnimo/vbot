"""Shared fixtures and fakes for tool dispatch behavior tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.chat.messages import ChatMessage, JsonObject, ToolCall
from core.chat.tool_dispatch import ToolDispatchContext
from core.chat.tool_dispatch import _dispatch_tool_calls as _dispatch_resolved_tool_calls
from core.runs import Run
from core.sessions import ChatSessionManager
from core.skills import SkillRegistry
from core.tools import ToolContract, ToolRegistry
from core.tools.availability import ToolAccess


@dataclass(frozen=True)
class StubDispatchAgent:
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


@dataclass
class Dispatched:
    """One dispatch batch: persisted Tool messages, media outputs and the owning Run."""

    messages: list[ChatMessage]
    media_outputs: list[JsonObject]
    run: Run
    session: Any

    @property
    def results(self) -> list[JsonObject]:
        return [decode_tool_result(message.content) for message in self.messages]

    def events(self, event_type: str) -> list[Any]:
        return [event for event in self.run.events if event.type == event_type]


class ToolDispatchHarness:
    """A Session, Run and Agent wired to a Tool registry the way the chat loop wires them."""

    def __init__(
        self,
        tmp_path: Path,
        tools: ToolRegistry,
        *,
        allowed_tools: list[str] | None = None,
        agent_tools: dict[str, object] | None = None,
        allowed_skills: list[str] | None = None,
        extensions: Any = None,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        self.tmp_path = tmp_path
        self.tools = tools
        self.extensions = extensions
        self.agent = StubDispatchAgent(
            id="coder",
            workspace=workspace,
            allowed_tools=["*"] if allowed_tools is None else allowed_tools,
            allowed_skills=allowed_skills,
            tools=agent_tools,
        )
        self.session = ChatSessionManager(tmp_path).create("coder", session_id="session-one")
        self.run = self.new_run()

    def new_run(self, run_id: str = "run-one") -> Run:
        return Run(run_id=run_id, agent_id=self.agent.id, session_id=self.session.id)

    async def dispatch(
        self,
        tool_calls: list[ToolCall],
        *,
        run: Run | None = None,
        project_cwd: Path | None = None,
        project_id: str | None = None,
        skill_registry: SkillRegistry | None = None,
        tool_restriction: Sequence[str] | None = None,
        base_allowed_tools: Sequence[str] | None = None,
        session_tool_grants: tuple[str, ...] = (),
        tool_contracts: Mapping[str, ToolContract] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        removed_tool_names: frozenset[str] = frozenset(),
    ) -> Dispatched:
        active_run = run or self.run
        messages, media_outputs = await _dispatch_resolved_tool_calls(
            ToolDispatchContext(
                registry=self.tools,
                extension_registry=self.extensions,
                agent=self.agent,
                session=self.session,
                run=active_run,
                nesting_depth=0,
                vbot_root=Path.cwd(),
                data_root=self.tmp_path,
                project_cwd=project_cwd,
                project_id=project_id,
                skill_project_id=None,
                skill_registry=skill_registry,
                tool_restriction=tool_restriction,
                base_allowed_tools=base_allowed_tools,
                session_tool_grants=session_tool_grants,
                tool_contracts=tool_contracts or {},
                tool_denial_resolver=tool_denial_resolver,
                removed_tool_names=removed_tool_names,
            ),
            tool_calls,
        )
        return Dispatched(list(messages), list(media_outputs), active_run, self.session)


def call(name: str, call_id: str | None = None, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id or f"call-{name}", name=name, arguments=arguments)


def decode_tool_result(message_content: object) -> JsonObject:
    assert isinstance(message_content, str)
    return cast(JsonObject, json.loads(message_content))
