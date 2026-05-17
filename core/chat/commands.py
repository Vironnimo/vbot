"""Slash command dispatch for chat entry points."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.chat.runs import ChatRunManager, RunNotFoundError

CommandHandler = Callable[[str, str], "CommandHandled"]


@dataclass(frozen=True)
class CommandHandled:
    """Result indicating command dispatch handled the message."""

    reply: str | None


@dataclass(frozen=True)
class NotACommand:
    """Result indicating message should continue through normal chat flow."""


DispatchResult = CommandHandled | NotACommand


class CommandDispatcher:
    """Dispatches built-in slash commands before run startup."""

    BUILT_IN_COMMANDS: dict[str, str] = {
        "stop": "Cancel the active run for this session.",
    }

    def __init__(self, chat_runs: ChatRunManager) -> None:
        self._chat_runs = chat_runs
        self._commands: dict[str, CommandHandler] = {
            "/stop": self._handle_stop,
        }

    def dispatch(self, agent_id: str, session_id: str, message_text: str) -> DispatchResult:
        """Dispatch one message as a built-in slash command when recognized."""
        normalized_text = message_text.strip().lower()
        handler = self._commands.get(normalized_text)
        if handler is None:
            return NotACommand()
        return handler(agent_id, session_id)

    def _handle_stop(self, agent_id: str, session_id: str) -> CommandHandled:
        try:
            self._chat_runs.cancel_by_session(agent_id, session_id)
        except RunNotFoundError:
            return CommandHandled(reply="No active run to cancel.")
        return CommandHandled(reply="Run cancelled.")
