"""Storage manager for data-directory setup, settings, and prompt fragments."""

from core.storage.storage import (
    DEFAULT_DATA_DIR,
    PHASE_TWO_DIRECTORIES,
    PROMPT_FRAGMENT_NAMES,
    ConfigProtocol,
    StorageError,
    StorageManager,
)

__all__ = [
    "ConfigProtocol",
    "DEFAULT_DATA_DIR",
    "PHASE_TWO_DIRECTORIES",
    "PROMPT_FRAGMENT_NAMES",
    "StorageError",
    "StorageManager",
]
