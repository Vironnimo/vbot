"""Desktop wakeword detection and voice pipeline."""

from typing import TYPE_CHECKING, Any

from desktop.wakeword.bridge import DesktopBridge
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
    from desktop.wakeword.worker import MockWakewordWorker, WakewordWorker, list_microphones


def __getattr__(name: str) -> Any:
    if name not in {"MockWakewordWorker", "WakewordWorker", "list_microphones"}:
        raise AttributeError(name)
    from desktop.wakeword import worker

    return getattr(worker, name)


__all__ = [
    "DesktopBridge",
    "MockWakewordEngine",
    "MockWakewordWorker",
    "MultiWakewordEngine",
    "WakewordEngine",
    "WakewordMatch",
    "WakewordModelCatalog",
    "WakewordModelDescriptor",
    "WakewordModelError",
    "WakewordWorker",
    "list_microphones",
]
