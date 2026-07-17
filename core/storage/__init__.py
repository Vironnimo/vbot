"""Storage manager for data-directory setup, settings persistence, and prompt fragments."""

from core.storage.errors import StorageError
from core.storage.prompt_blocks import BLOCK_NAMESPACES, PromptBlockStore
from core.storage.prompt_fragments import PROMPT_FRAGMENT_NAMES
from core.storage.storage import (
    DEFAULT_DATA_DIR,
    PHASE_TWO_DIRECTORIES,
    ConfigProtocol,
    StorageManager,
)
from core.storage.temp_files import (
    TEMPORARY_FILE_RETENTION,
    TEMPORARY_FILE_ROOT,
    TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS,
    TemporaryFileLease,
    TemporaryFileManager,
)

__all__ = [
    "BLOCK_NAMESPACES",
    "ConfigProtocol",
    "DEFAULT_DATA_DIR",
    "PHASE_TWO_DIRECTORIES",
    "PROMPT_FRAGMENT_NAMES",
    "PromptBlockStore",
    "StorageError",
    "StorageManager",
    "TEMPORARY_FILE_RETENTION",
    "TEMPORARY_FILE_ROOT",
    "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS",
    "TemporaryFileLease",
    "TemporaryFileManager",
]
