"""Format-neutral repository inputs. Only adapters interpret foreign settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

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
