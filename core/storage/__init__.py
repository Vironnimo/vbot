"""Storage package with a lightweight canonical-layout import boundary."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.storage.layout import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    DataDirectoryInitializationResult,
    DataDirectoryLayout,
    initialize_data_directory,
)

if TYPE_CHECKING:
    from core.storage.errors import StorageError
    from core.storage.prompt_blocks import BLOCK_NAMESPACES, PromptBlockStore
    from core.storage.prompt_fragments import PROMPT_FRAGMENT_NAMES
    from core.storage.storage import DEFAULT_DATA_DIR, ConfigProtocol, StorageManager
    from core.storage.temp_files import (
        TEMPORARY_FILE_RETENTION,
        TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS,
        TemporaryFileLease,
        TemporaryFileManager,
    )

_LAZY_EXPORTS = {
    "BLOCK_NAMESPACES": ("core.storage.prompt_blocks", "BLOCK_NAMESPACES"),
    "ConfigProtocol": ("core.storage.storage", "ConfigProtocol"),
    "DEFAULT_DATA_DIR": ("core.storage.storage", "DEFAULT_DATA_DIR"),
    "PROMPT_FRAGMENT_NAMES": ("core.storage.prompt_fragments", "PROMPT_FRAGMENT_NAMES"),
    "PromptBlockStore": ("core.storage.prompt_blocks", "PromptBlockStore"),
    "StorageError": ("core.storage.errors", "StorageError"),
    "StorageManager": ("core.storage.storage", "StorageManager"),
    "TEMPORARY_FILE_RETENTION": (
        "core.storage.temp_files",
        "TEMPORARY_FILE_RETENTION",
    ),
    "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS": (
        "core.storage.temp_files",
        "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS",
    ),
    "TemporaryFileLease": ("core.storage.temp_files", "TemporaryFileLease"),
    "TemporaryFileManager": ("core.storage.temp_files", "TemporaryFileManager"),
}

__all__ = [
    "BLOCK_NAMESPACES",
    "ConfigProtocol",
    "DATA_DIRECTORY_RELATIVE_PATHS",
    "DEFAULT_DATA_DIR",
    "DataDirectoryInitializationResult",
    "DataDirectoryLayout",
    "PROMPT_FRAGMENT_NAMES",
    "PromptBlockStore",
    "StorageError",
    "StorageManager",
    "TEMPORARY_FILE_RETENTION",
    "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS",
    "TemporaryFileLease",
    "TemporaryFileManager",
    "initialize_data_directory",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
