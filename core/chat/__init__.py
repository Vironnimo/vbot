"""Chat domain public API."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.chat.chat import MAX_TOOL_ITERATIONS as MAX_TOOL_ITERATIONS
    from core.chat.chat import ChatLoop as ChatLoop
    from core.chat.chat import ChatMessage as ChatMessage
    from core.chat.chat import ToolCall as ToolCall
    from core.chat.commands import CommandAction as CommandAction
    from core.chat.commands import CommandDispatcher as CommandDispatcher
    from core.chat.commands import CommandHandled as CommandHandled
    from core.chat.commands import DispatchResult as DispatchResult
    from core.chat.commands import NotACommand as NotACommand
    from core.chat.errors import ChatError as ChatError
    from core.chat.errors import ChatMessageValidationError as ChatMessageValidationError
    from core.chat.errors import ChatSessionError as ChatSessionError
    from core.chat.errors import ToolIterationLimitError as ToolIterationLimitError
    from core.sessions import ChatSession as ChatSession
    from core.sessions import ChatSessionManager as ChatSessionManager

_EXPORT_MODULES = {
    "ChatError": "core.chat.errors",
    "ChatLoop": "core.chat.chat",
    "ChatMessage": "core.chat.chat",
    "ChatMessageValidationError": "core.chat.errors",
    "ChatSession": "core.sessions",
    "ChatSessionError": "core.chat.errors",
    "ChatSessionManager": "core.sessions",
    "CommandDispatcher": "core.chat.commands",
    "CommandAction": "core.chat.commands",
    "CommandHandled": "core.chat.commands",
    "DispatchResult": "core.chat.commands",
    "MAX_TOOL_ITERATIONS": "core.chat.chat",
    "NotACommand": "core.chat.commands",
    "ToolCall": "core.chat.chat",
    "ToolIterationLimitError": "core.chat.errors",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
