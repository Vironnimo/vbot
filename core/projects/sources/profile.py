"""Format-neutral repository inputs. Only adapters interpret foreign settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from core.permissions import PermissionRules

type TranslationStatus = Literal["applied", "translated", "not_supported", "overridden"]


@dataclass(frozen=True)
class Translation:
    setting: str
    status: TranslationStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"setting": self.setting, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class AgentTargetRule:
    pattern: str
    allowed: bool


@dataclass(frozen=True)
class ToolRule:
    """An ordered rule over vBot Tool names, with last-match precedence."""

    pattern: str
    allowed: bool


# The vBot Tools a Profile without an explicit Tool list gets: the counterparts
# of a coding agent's built-in Tools plus vBot's own display Tools. A foreign
# Agent without a Tool list has every Tool of its own coding agent, not every
# vBot Tool; anything else reaches a Team member through a vBot override.
DEFAULT_PROFILE_TOOLS: tuple[str, ...] = (
    "read",
    "apply_patch",
    "search_files",
    "bash",
    "terminal",
    "web_fetch",
    "web_search",
    "subagent",
    "skill",
    "status",
    "show_widget",
    "portal",
)


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    display_name: str
    description: str
    model: str
    temperature: float | None
    body: str
    source: str
    source_path: Path
    # An explicit Tool list; ``None`` means ``DEFAULT_PROFILE_TOOLS``.
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    tool_rules: tuple[ToolRule, ...] = ()
    agent_target_rules: tuple[AgentTargetRule, ...] = ()
    thinking_effort: str | None = None
    top_p: float | None = None
    preload_skills: tuple[str, ...] = ()
    skill_rules: tuple[AgentTargetRule, ...] = ()
    translations: tuple[Translation, ...] = ()
    unavailable_reason: str | None = None
    # Permission Rules translated from the repository's own permission settings;
    # ``None`` when the source names none (a vBot override or the defaults apply).
    permission_rules: PermissionRules | None = None
    # Repository files besides ``source_path`` this profile is read from (shared
    # settings, a referenced prompt file), also candidates that do not exist
    # yet such as ``.claude/settings.local.json``: a restricted Session of the
    # member may read them where its rules allow but never change them.
    rule_files: tuple[Path, ...] = ()

    @property
    def tools(self) -> tuple[str, ...]:
        """The Tools this Profile starts from, before its rules and denials."""
        if self.allowed_tools is None:
            return DEFAULT_PROFILE_TOOLS
        known = tuple(name for name in DEFAULT_PROFILE_TOOLS if name in self.allowed_tools)
        return (*known, *sorted(self.allowed_tools - set(known)))

    @property
    def status(self) -> str:
        if self.unavailable_reason:
            return "needs_attention"
        if any(item.status == "not_supported" for item in self.translations):
            return "limited"
        return "ready"


@dataclass(frozen=True)
class SourceSelection:
    id: str
    enabled: bool = True
    agent_paths: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, str | bool | list[str]]:
        result: dict[str, str | bool | list[str]] = {"id": self.id, "enabled": self.enabled}
        if self.agent_paths is not None:
            result["agent_paths"] = list(self.agent_paths)
        return result

    def includes(self, root: Path, path: Path) -> bool:
        if self.agent_paths is None:
            return True
        relative = PurePosixPath(path.relative_to(root).as_posix())
        return any(relative.match(pattern) for pattern in self.agent_paths)
