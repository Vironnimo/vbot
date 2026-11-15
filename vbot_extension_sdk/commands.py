"""Command results shared by Chat, Channels and Extension command handlers."""

from typing import Any

from ._exports import resolve

_EXPORTS = dict.fromkeys(
    ("CommandFeedback", "CommandOutcome", "CommandRun", "ExtensionCommandContext"), "core.chat"
)


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = list(_EXPORTS)
