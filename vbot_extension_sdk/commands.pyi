# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from typing import (
    Any,
    Never,
)

from .channels import ContentBlock

@dataclass(frozen=True)
class CommandFeedback:
    kind: Any
    text: str

@dataclass(frozen=True)
class CommandOutcome:
    command: str
    feedback: CommandFeedback | None = None
    facts: Mapping[str, object] = ...
    navigation: Any | None = None
    runs: tuple[CommandRun, ...] = ()
    resource_changes: tuple[Any, ...] = ()

@dataclass(frozen=True)
class CommandRun:
    role: Any
    run: Any

@dataclass(frozen=True, init=False, match_args=False)
class ExtensionCommandContext:
    # The host supplies this context to the registered command handler.
    def __init__(self, _host: Never) -> None: ...
    agent_id: str
    session_id: str
    project_id: str | None
    reply_surface: Any

    async def start_run(
        self, content: str | list[ContentBlock], *, internal: bool = False
    ) -> Any: ...
    def report_change(self, change: Any) -> None: ...
