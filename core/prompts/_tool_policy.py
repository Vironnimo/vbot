"""Prompt-time Tool visibility shared by schemas, catalogs, and block gates."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from core.prompts._types import JsonObject, PromptAgent, ToolPromptRegistry
from core.prompts.blocks import ToolAvailability
from core.tools.availability import (
    agent_tool_settings,
    apply_agent_target_tool_visibility,
    resolve_tool_access,
    subagent_allowed_agents,
)
from core.tools.on_demand import (
    LOAD_TOOLS_TOOL_NAME,
    loads_tools_on_demand,
    on_demand_tool_entries,
    on_demand_tools,
)
from core.tools.tools import ToolDefinitionProfileContext

# ``(registry name, summary)`` of each On-demand Tool, resolved on first use.
OnDemandEntries = Callable[[], tuple[tuple[str, str], ...]]


class PromptToolPolicy:
    """Resolve the same Agent policy for every prompt-time view of its Tools."""

    def __init__(self, registry: ToolPromptRegistry) -> None:
        self._registry = registry

    def tool_availability(
        self,
        agent: PromptAgent,
        *,
        effective_tool_names: Collection[str] | None = None,
        session_tool_grants: Sequence[str] = (),
    ) -> ToolAvailability:
        """Return which Tools *agent* can call, asked by registry name.

        With *effective_tool_names* (the Tools a request lists) this is plain
        membership. Otherwise the Agent's live Tool policy is resolved once, on the
        first question, through the same path the Tool list uses.
        """
        if effective_tool_names is not None:
            listed = effective_tool_names
            return lambda tool_name: tool_name in listed
        resolved: frozenset[str] | None = None

        def available(tool_name: str) -> bool:
            nonlocal resolved
            if resolved is None:
                resolved = frozenset(
                    str(definition.get("name"))
                    for definition in self.prompt_definitions(agent, session_tool_grants)
                )
            return tool_name in resolved

        return available

    def agent_tool_allowed(
        self,
        agent: PromptAgent,
        tool_name: str,
        *,
        effective_tool_names: Collection[str] | None = None,
        session_tool_grants: Sequence[str] = (),
    ) -> bool:
        """Return whether *tool_name* is in the agent's effective prompt tool set.

        With *effective_tool_names* (the Tools the request lists) this is plain
        membership. Otherwise it reuses the same prompt-definition path the tools
        block uses (allowlist + derived ``memory`` visibility), so gate 2 cannot
        drift from what the tool list actually shows. A *tool_name* ending in
        ``*`` is a prefix: any listed name starting with the rest matches.
        """
        if tool_name.endswith("*"):
            prefix = tool_name[:-1]

            def matches(name: object) -> bool:
                return isinstance(name, str) and name.startswith(prefix)
        else:

            def matches(name: object) -> bool:
                return name == tool_name

        if effective_tool_names is not None:
            return any(matches(name) for name in effective_tool_names)
        definitions = self.prompt_definitions(agent, session_tool_grants)
        return any(matches(definition.get("name")) for definition in definitions)

    def provider_definitions(
        self,
        agent: PromptAgent,
        session_tool_grants: Sequence[str] = (),
        *,
        ready_only: bool = True,
    ) -> list[JsonObject]:
        profile_context = ToolDefinitionProfileContext(
            agent_id=agent.id, project_id=getattr(agent, "project_id", None)
        )
        resolution = resolve_tool_access(
            agent.tool_access,
            self._registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=agent.workspace,
            session_tool_grants=session_tool_grants,
        )
        definitions = self._registry.provider_definitions(
            resolution.allowed_tools,
            session_grants=resolution.session_tool_grants,
            ready_only=ready_only,
            profile_context=profile_context,
        )
        return _with_load_tools(
            agent,
            apply_agent_target_tool_visibility(
                definitions,
                agent_id=agent.id,
                allowed_agents=subagent_allowed_agents(
                    agent_tool_settings(getattr(agent, "tools", {}))
                ),
            ),
            session_tool_grants,
            lambda: self._registry.provider_definitions(
                [LOAD_TOOLS_TOOL_NAME],
                include_internal=True,
                ready_only=ready_only,
                profile_context=profile_context,
            ),
        )

    def prompt_definitions(
        self,
        agent: PromptAgent,
        session_tool_grants: Sequence[str] = (),
    ) -> list[JsonObject]:
        profile_context = ToolDefinitionProfileContext(
            agent_id=agent.id, project_id=getattr(agent, "project_id", None)
        )
        resolution = resolve_tool_access(
            agent.tool_access,
            self._registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=agent.workspace,
            session_tool_grants=session_tool_grants,
        )
        definitions = self._registry.prompt_definitions(
            resolution.allowed_tools,
            session_grants=resolution.session_tool_grants,
            profile_context=profile_context,
        )
        return _with_load_tools(
            agent,
            apply_agent_target_tool_visibility(
                definitions,
                agent_id=agent.id,
                allowed_agents=subagent_allowed_agents(
                    agent_tool_settings(getattr(agent, "tools", {}))
                ),
            ),
            session_tool_grants,
            lambda: self._registry.prompt_definitions(
                [LOAD_TOOLS_TOOL_NAME], include_internal=True, profile_context=profile_context
            ),
        )

    def on_demand_entries(
        self,
        agent: PromptAgent,
        *,
        effective_tool_definitions: Sequence[Mapping[str, Any]] | None,
        on_demand_tools: Sequence[tuple[str, str]] | None,
        session_tool_grants: Sequence[str],
    ) -> OnDemandEntries:
        """Return the On-demand Tools one build lists, resolved on first use.

        Given *on_demand_tools* are used as they are. Without them, a build with
        *effective_tool_definitions* lists none, and a live build classifies the
        Tools the Agent's Tool list would offer.
        """
        if on_demand_tools is not None:
            given = tuple((str(name), str(summary)) for name, summary in on_demand_tools)
            return lambda: given
        if effective_tool_definitions is not None or not loads_tools_on_demand(agent):
            return tuple
        resolved: tuple[tuple[str, str], ...] | None = None

        def entries() -> tuple[tuple[str, str], ...]:
            nonlocal resolved
            if resolved is None:
                resolved = on_demand_tool_entries(
                    self._registry,
                    agent,
                    self.provider_definitions(agent, session_tool_grants),
                    session_tool_grants=session_tool_grants,
                )
            return resolved

        return entries


def _tool_names(
    definitions: Sequence[Mapping[str, Any]] | None, on_demand: OnDemandEntries
) -> frozenset[str] | None:
    """Return the Tools a build with *definitions* counts as available, or ``None`` (live).

    The On-demand Tools count: the Agent can call them.
    """
    if definitions is None:
        return None
    return frozenset(
        [*(str(definition["name"]) for definition in definitions), *(n for n, _ in on_demand())]
    )


def _with_load_tools(
    agent: PromptAgent,
    definitions: list[JsonObject],
    session_tool_grants: Sequence[str],
    load_tools: Callable[[], list[JsonObject]],
) -> list[JsonObject]:
    """Return *definitions* with ``load_tools`` appended once while one of them is on demand.

    ``load_tools`` belongs to the Agent's Tools exactly while it has a Tool to
    load, so an Agent whose Tool list keeps every Tool never gets it.
    """
    names = [str(definition.get("name")) for definition in definitions]
    if LOAD_TOOLS_TOOL_NAME in names or not on_demand_tools(
        agent, names, session_tool_grants=session_tool_grants
    ):
        return definitions
    return [*definitions, *load_tools()]
