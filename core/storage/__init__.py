"""Storage manager for data-directory setup, settings, and prompt fragments."""

from core.storage.errors import StorageError
from core.storage.prompt_fragments import PROMPT_FRAGMENT_NAMES
from core.storage.settings_normalizers import (
    DEFAULT_APPEARANCE_LANGUAGE,
    DEFAULT_RECALL_SETTINGS,
    SUPPORTED_APPEARANCE_LANGUAGES,
)
from core.storage.storage import (
    DEFAULT_DATA_DIR,
    PHASE_TWO_DIRECTORIES,
    ConfigProtocol,
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
