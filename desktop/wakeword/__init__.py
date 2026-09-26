"""Desktop Voice: wake phrase detection and spoken commands.

The package root stays light: the model catalog and engine types import only
the standard library. :class:`VoiceController` (and through it the audio
stack) loads on first access.
"""

from typing import TYPE_CHECKING, Any

from desktop.wakeword.engine import (
    MockWakewordEngine,
    MultiWakewordEngine,
    WakewordEngine,
    WakewordMatch,
    WakewordModelCatalog,
    WakewordModelDescriptor,
    WakewordModelError,
)

if TYPE_CHECKING:
    from desktop.wakeword.controller import VoiceController, VoiceRuntime


def __getattr__(name: str) -> Any:
    if name not in {"VoiceController", "VoiceRuntime"}:
        raise AttributeError(name)
    from desktop.wakeword import controller

    return getattr(controller, name)


__all__ = [
    "MockWakewordEngine",
    "MultiWakewordEngine",
    "VoiceController",
    "VoiceRuntime",
    "WakewordEngine",
    "WakewordMatch",
    "WakewordModelCatalog",
    "WakewordModelDescriptor",
    "WakewordModelError",
]
