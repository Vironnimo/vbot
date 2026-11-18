"""Owner-declared profiles for operator conversations inside Extension pages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.agents.temporary import TemporaryAgentConfig
    from core.permissions import PermissionCeiling
    from core.settings.agent_defaults import AgentDefaults
    from core.tools import ToolRegistry


@dataclass(frozen=True)
class AppChatDeclaration:
    id: str
    name: str
    instructions: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    prompt_blocks: tuple[str, ...]
    model: str
    # The declaration must not change when a caller edits its original settings.
    tool_settings_json: str
    permission_ceiling: PermissionCeiling | None

    @classmethod
    def create(
        cls,
        profile_id: str,
        *,
        name: str,
        instructions: str,
        tools: Sequence[str],
        skills: Sequence[str],
        prompt_blocks: Sequence[str],
        model: str,
        tool_settings: Mapping[str, Any] | None = None,
        permission_ceiling: Mapping[str, Any] | None = None,
    ) -> AppChatDeclaration:
        from core.agents import normalize_agent_tools
        from core.extensions._declarations import _is_page_id
        from core.permissions import PermissionCeiling

        if not _is_page_id(profile_id):
            raise ValueError("App chat profile id must be a local id")
        if any(not isinstance(item, str) or not item.strip() for item in (name, instructions)):
            raise ValueError("App chat profile requires a name and role instructions")
        if not isinstance(model, str):
            raise ValueError("App chat model must be a Model binding")
        for label, names in (
            ("tools", tools),
            ("skills", skills),
            ("prompt_blocks", prompt_blocks),
        ):
            if (
                not isinstance(names, (tuple, list))
                or any(
                    not isinstance(item, str) or not item.strip() or item == "*" for item in names
                )
                or len(set(names)) != len(names)
            ):
                raise ValueError(f"App chat {label} must be explicit unique names")
        if any(":" not in block for block in prompt_blocks):
            raise ValueError("App chat prompt blocks require fully qualified ids")
        settings = normalize_agent_tools(tool_settings)
        if set(settings).difference(tools):
            raise ValueError("App chat Tool settings require the Tool to be selected")
        if settings.get("bash", {}).get("allowed_env"):
            raise ValueError("App chat profiles cannot grant permanent environment access")
        try:
            settings_json = json.dumps(settings, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("App chat Tool settings must contain JSON values") from error
        ceiling = None
        if permission_ceiling is not None:
            ceiling = PermissionCeiling.from_ceiling_dict(permission_ceiling)
            if any(layer.source == "agent" for layer in ceiling.layers):
                raise ValueError("An App chat Permission Ceiling must contain inherited layers")
        return cls(
            profile_id,
            name,
            instructions,
            tuple(tools),
            tuple(skills),
            tuple(prompt_blocks),
            model,
            settings_json,
            ceiling,
        )

    @cached_property
    def revision(self) -> str:
        fields = asdict(self)
        if self.tool_settings_json == "{}":
            del fields["tool_settings_json"]
        if self.permission_ceiling is None:
            del fields["permission_ceiling"]
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def configuration(
        self, model: str, defaults: AgentDefaults, directory: Path
    ) -> TemporaryAgentConfig:
        from core.agents.temporary import AppChatAgentConfig
        from core.tools.availability import ToolAccess

        if isinstance(defaults.permission_rules, str):
            raise ValueError("Repair the default Agent Permission Rules before starting a chat")
        return AppChatAgentConfig(
            model=model,
            name=self.name,
            instructions=self.instructions,
            cwd=directory,
            tool_access=ToolAccess(mode="selected", allowed=self.tools, fixed=True, exact=True),
            allowed_skills=list(self.skills),
            tools=json.loads(self.tool_settings_json),
            project_memory_enabled=False,
            prompt_blocks=list(dict.fromkeys(("core:agent_body", *self.prompt_blocks))),
            permission_rules=defaults.permission_rules,
            permission_ceiling=(
                self.permission_ceiling.to_ceiling_dict() if self.permission_ceiling else None
            ),
            profile_id=self.id,
            profile_revision=self.revision,
        )

    def unavailable_tool(self, owner: str, registry: ToolRegistry) -> str | None:
        from core.tools import ToolNotFoundError

        for name in self.tools:
            try:
                tool = registry.get(name)
            except ToolNotFoundError:
                return name
            if tool.internal or (tool.session_scoped and tool.extension != owner):
                return name
        return None
