"""Storage manager for data-directory setup, settings, and prompt fragments."""

from core.storage.storage import (
    DEFAULT_APPEARANCE_LANGUAGE,
    DEFAULT_DATA_DIR,
    DEFAULT_RECALL_SETTINGS,
    PHASE_TWO_DIRECTORIES,
    PROMPT_FRAGMENT_NAMES,
    SUPPORTED_APPEARANCE_LANGUAGES,
    ConfigProtocol,
    StorageError,
    StorageManager,
)

__all__ = [
    "ConfigProtocol",
    "DEFAULT_APPEARANCE_LANGUAGE",
    "DEFAULT_DATA_DIR",
    "DEFAULT_RECALL_SETTINGS",
    "PHASE_TWO_DIRECTORIES",
    "PROMPT_FRAGMENT_NAMES",
    "StorageError",
    "StorageManager",
    "SUPPORTED_APPEARANCE_LANGUAGES",
]
