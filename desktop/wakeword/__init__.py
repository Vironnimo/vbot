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
    from desktop.wakeword.controller import VoiceController
    from desktop.wakeword.listener import VoiceRuntime


def __getattr__(name: str) -> Any:
    if name not in {"VoiceController", "VoiceRuntime"}:
        raise AttributeError(name)
    import importlib

    module = "controller" if name == "VoiceController" else "listener"
    return getattr(importlib.import_module(f"desktop.wakeword.{module}"), name)


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
