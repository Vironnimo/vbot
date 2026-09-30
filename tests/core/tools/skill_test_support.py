"""Harness for the ``skill`` Tool: register it over a Skill registry and dispatch calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from core.skills.skills import SkillRegistry
from core.tools import (
    SKILL_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    ToolSkillActivationHook,
    register_skill_tool,
)

RegistryResolver = Callable[[str | None, str | None], SkillRegistry]


class SkillTool:
    """The registered ``skill`` Tool, called through registry dispatch like the executor."""

    def __init__(
        self,
        workspace: Path,
        registry: SkillRegistry | RegistryResolver,
        refresh: Callable[[], None] = lambda: None,
    ) -> None:
        self.workspace = workspace
        resolver = (
            (lambda _project_id, _agent_id: registry)
            if isinstance(registry, SkillRegistry)
            else registry
        )
        self.tools = ToolRegistry()
        register_skill_tool(self.tools, resolver, refresh)

    def details(self, arguments: dict[str, object], result: dict[str, Any]) -> list[Any]:
        """Return the detail blocks the user sees for one call."""
        shown: list[Any] = self.tools.display_for_call(SKILL_TOOL_NAME, arguments, result=result)[
            "details"
        ]
        return shown

    def call(
        self,
        arguments: dict[str, object],
        *,
        activation_hook: ToolSkillActivationHook | None = None,
        project_id: str | None = None,
        allowed_skills: list[str] | None = None,
    ) -> dict[str, Any]:
        context = ToolContext(
            agent_id="coder",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=SKILL_TOOL_NAME,
            tool_call_index=0,
            workspace=self.workspace,
            vbot_root=self.workspace,
            data_root=self.workspace,
            project_id=project_id,
            # Outside a rooted project run the effective Skill project equals project_id.
            skill_project_id=project_id,
            skill_activation_hook=activation_hook,
            allowed_skills=["*"] if allowed_skills is None else allowed_skills,
        )
        result = asyncio.run(self.tools.dispatch(context, arguments, [SKILL_TOOL_NAME]))
        return cast(dict[str, Any], result)
