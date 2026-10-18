"""Prompt environment substitutions, generated catalogs and block-owner gates."""

from __future__ import annotations

import platform
import socket
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_FILES_PRODUCER_NAME,
)
from core.prompts._context import (
    render_memory_files,
)
from core.prompts._formatting import (
    _format_channel_list,
    _format_on_demand_tool_list,
    _format_skill_catalog,
    _format_tool_list,
)
from core.prompts._tool_policy import OnDemandEntries, PromptToolPolicy
from core.prompts._types import (
    BLOCK_OWNER_IDENTITY,
    BLOCK_OWNER_SUBAGENT_SESSION,
    CHANNEL_LIST_PRODUCER,
    ChannelPromptMetadata,
    ChannelPromptRegistry,
    MemoryPromptProvider,
    PinnedSkillCatalog,
    PromptAgent,
    SkillPromptRegistry,
)
from core.prompts.blocks import (
    BlockProducer,
    BlockRenderContext,
)
from core.tools.availability import (
    memory_tool_enabled,
)
from core.tools.on_demand import (
    ON_DEMAND_TOOL_LIST_PRODUCER,
)
from core.utils.paths import model_path


class PromptRenderInputs:
    """Build request-local render inputs from the admitted registries and environment."""

    def __init__(
        self,
        tool_policy: PromptToolPolicy,
        skill_registry: SkillPromptRegistry,
        channel_registry: ChannelPromptRegistry | None,
        memory_provider: MemoryPromptProvider,
        *,
        vbot_version: str,
        vbot_root: Path,
        data_root: Path,
        server_hostname: str | None,
        operating_system: str | None,
        current_local_date: Callable[[], str] | None,
        timezone_name: Callable[[], str],
        loaded_extensions: Collection[str],
    ) -> None:
        self._tool_policy = tool_policy
        self._skill_registry = skill_registry
        self._channel_registry = channel_registry
        self._memory_provider = memory_provider
        self._vbot_version = vbot_version
        self._vbot_root = vbot_root
        self._data_root = data_root
        self._server_hostname = server_hostname
        self._operating_system = operating_system
        self._current_local_date = current_local_date
        self._timezone_name = timezone_name
        self._loaded_extensions = frozenset(loaded_extensions)

    def _runtime_replacements(
        self, agent: PromptAgent, thinking_effort: str | None = None
    ) -> dict[str, str]:
        """Return the build-time runtime-variable substitutions.

        Applied to every text block by the engine. The shared Runtime and Identity
        Environment resources carry these placeholders; treating them as build-time
        globals keeps replacement behavior uniform across resource-backed text blocks.
        ``thinking_effort`` replaces the Agent's own effort (see
        :meth:`build_system_prompt`).
        """
        effort = agent.thinking_effort if thinking_effort is None else thinking_effort
        thinking_effort = effort or "provider default"
        return {
            "{server_hostname}": self._server_hostname or socket.gethostname(),
            "{vbot_version}": self._vbot_version,
            "{operating_system}": self._operating_system or platform.platform(),
            "{model}": agent.model,
            "{agent_id}": agent.id,
            "{agent_name}": agent.name or agent.id,
            "{identity_workspace}": model_path(agent.workspace) if agent.workspace else "",
            "{vbot_root}": model_path(self._vbot_root.resolve()),
            "{data_root}": model_path(self._data_root.resolve()),
            "{thinking_effort}": thinking_effort,
            "{current_local_date}": self._resolve_current_local_date(),
            "{timezone}": self._timezone_name(),
        }

    def _resolve_current_local_date(self) -> str:
        if self._current_local_date is not None:
            return self._current_local_date()
        return _current_local_date(self._timezone_name())

    def _resolve_skill_registry(
        self, skill_registry: SkillPromptRegistry | None
    ) -> SkillPromptRegistry:
        """Return the per-call registry, or the configured global one when absent."""
        return skill_registry if skill_registry is not None else self._skill_registry

    def _build_producers(
        self,
        agent: PromptAgent,
        skill_registry: SkillPromptRegistry | None,
        skill_catalog: PinnedSkillCatalog | None = None,
        *,
        effective_tool_definitions: Sequence[Mapping[str, Any]] | None = None,
        on_demand: OnDemandEntries = tuple,
        session_tool_grants: Sequence[str] = (),
    ) -> dict[str, BlockProducer]:
        """Build the ``{generated:NAME}`` producer registry for this build.

        Each producer is a closure over the registries the manager already holds
        (and the per-call skill-registry override), so the list-formatting logic
        lives in one place. ``tool_list``/``channel_list``/``skill_catalog`` feed the
        core tools/channels/skills blocks; ``memory_files`` renders the
        ``USER.md``/``MEMORY.md`` ``<file>`` contents per the agent's memory mode
        (the embedded data half of the ``memory:guidance`` block — the file reading
        itself lives in the memory domain's :func:`read_memory_files`). When a
        prompt-epoch ``skill_catalog`` is given, its producer returns the frozen
        text instead of re-filtering the live registry; given
        ``effective_tool_definitions``, ``tool_list`` renders exactly those, and
        the live ``tool_list`` leaves out the On-demand Tools. ``on_demand_tool_list``
        renders *on_demand* as ``- name: summary`` lines.
        """
        active_skill_registry = self._resolve_skill_registry(skill_registry)

        def tool_list(context: BlockRenderContext) -> str:
            if effective_tool_definitions is not None:
                return _format_tool_list(
                    [
                        {"name": definition["name"], "description": definition["description"]}
                        for definition in effective_tool_definitions
                    ]
                )
            on_demand_names = {name for name, _summary in on_demand()}
            return _format_tool_list(
                [
                    definition
                    for definition in self._tool_policy.prompt_definitions(
                        context.agent, session_tool_grants
                    )
                    if definition.get("name") not in on_demand_names
                ]
            )

        def on_demand_tool_list(_context: BlockRenderContext) -> str:
            return _format_on_demand_tool_list(on_demand())

        def channel_list(context: BlockRenderContext) -> str:
            return _format_channel_list(self._agent_enabled_channels(context.agent))

        def skill_catalog_text(context: BlockRenderContext) -> str:
            if skill_catalog is not None:
                return skill_catalog.catalog_text
            return _format_skill_catalog(
                active_skill_registry.filter_allowed(context.agent.allowed_skills)
            )

        def memory_files(context: BlockRenderContext) -> str:
            if context.memory_files_context is not None:
                return context.memory_files_context
            return render_memory_files(
                self._memory_provider, context.agent, context.tool_available, context.read_observer
            )

        return {
            "tool_list": tool_list,
            ON_DEMAND_TOOL_LIST_PRODUCER: on_demand_tool_list,
            CHANNEL_LIST_PRODUCER: channel_list,
            "skill_catalog": skill_catalog_text,
            MEMORY_FILES_PRODUCER_NAME: memory_files,
        }

    def _is_owner_active(
        self,
        owner: str,
        agent: PromptAgent,
        *,
        effective_tool_names: Collection[str] | None = None,
        session_tool_grants: Sequence[str] = (),
        subagent_session: bool = False,
    ) -> bool:
        """Return whether a block's owner is active for *agent* (gate 2, D5).

        Reads the same seams the manager already applies — never a hardcoded or
        re-implemented gate:

        - ``always`` → always true.
        - ``identity`` → the Agent has an Identity/Memory Workspace.
        - ``memory`` → the memory tool is enabled for the agent.
        - ``tool:<name>`` → ``<name>`` is among *effective_tool_names* when given,
          else in the agent's effective allowed tools. A trailing ``*`` matches
          any listed Tool whose name starts with the text before it.
        - ``channel`` → the agent has at least one enabled Channel config.
        - ``subagent_session`` → the prompt is built for a Session linked to a
          Parent Agent (a Sub-Agent Session).
        - ``extension:<name>`` → the extension is in the loaded-extension set the
          runtime rebuilds and injects on every extension (re)load.
        - ``builtin:<kind>`` → the Agent is the built-in Agent of that kind.
        """
        if owner == "always":
            return True
        if owner == BLOCK_OWNER_IDENTITY:
            return bool(agent.workspace)
        if owner == "memory":
            mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
            return bool(agent.workspace) and memory_tool_enabled(mode)
        if owner == "channel":
            return bool(self._agent_enabled_channels(agent))
        if owner == BLOCK_OWNER_SUBAGENT_SESSION:
            return subagent_session
        tool_prefix = "tool:"
        if owner.startswith(tool_prefix):
            tool_name = owner[len(tool_prefix) :]
            return self._tool_policy.agent_tool_allowed(
                agent,
                tool_name,
                effective_tool_names=effective_tool_names,
                session_tool_grants=session_tool_grants,
            )
        extension_prefix = "extension:"
        if owner.startswith(extension_prefix):
            return owner[len(extension_prefix) :] in self._loaded_extensions
        builtin_prefix = "builtin:"
        if owner.startswith(builtin_prefix):
            return getattr(agent, "builtin", None) == owner[len(builtin_prefix) :]
        return False

    def _agent_enabled_channels(self, agent: PromptAgent) -> list[ChannelPromptMetadata]:
        channel_registry = self._channel_registry
        if channel_registry is None:
            return []

        enabled_channels: list[ChannelPromptMetadata] = []
        for channel in channel_registry.list_channels():
            if channel.agent_id != agent.id:
                continue
            if not channel.enabled:
                continue
            enabled_channels.append(channel)
        return enabled_channels


def _current_local_date(timezone_name: str) -> str:
    return datetime.now(UTC).astimezone(ZoneInfo(timezone_name)).date().isoformat()
