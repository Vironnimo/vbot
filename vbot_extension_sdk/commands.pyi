# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from typing import (
    Any,
)

from .channels import ContentBlock as ContentBlock

@dataclass(frozen=True)
class CommandFeedback:
    kind: Any
    text: str

@dataclass(frozen=True)
class CommandOutcome:
    command: str
    feedback: CommandFeedback | None = ...
    facts: Mapping[str, object] = ...
    navigation: Any | None = ...
    runs: tuple[CommandRun, ...] = ...
    resource_changes: tuple[Any, ...] = ...

@dataclass(frozen=True)
class CommandRun:
    role: Any
    run: Any

@dataclass(frozen=True)
class ExtensionCommandContext:
    agent_id: str
    session_id: str
    project_id: str | None
    reply_surface: Any

    async def start_run(
        self, content: str | list[ContentBlock], *, internal: bool = ...
    ) -> Any: ...
    def report_change(self, change: Any) -> None: ...
