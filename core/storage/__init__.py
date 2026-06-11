"""Storage manager for data-directory setup, settings persistence, and prompt fragments."""

from core.storage.errors import StorageError
from core.storage.prompt_fragments import PROMPT_FRAGMENT_NAMES
from core.storage.storage import (
    DEFAULT_DATA_DIR,
    PHASE_TWO_DIRECTORIES,
    ConfigProtocol,
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
