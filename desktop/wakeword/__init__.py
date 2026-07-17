"""Desktop wakeword detection and voice pipeline."""

from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    MockWakewordEngine,
    OpenWakeWordEngine,
    WakewordEngine,
    WakewordModelCatalog,
    WakewordModelDescriptor,
    WakewordModelError,
)
from desktop.wakeword.worker import MockWakewordWorker, WakewordWorker, list_microphones

__all__ = [
    "DesktopBridge",
    "MockWakewordEngine",
    "MockWakewordWorker",
    "OpenWakeWordEngine",
    "WakewordEngine",
    "WakewordModelCatalog",
    "WakewordModelDescriptor",
    "WakewordModelError",
    "WakewordWorker",
    "list_microphones",
]
