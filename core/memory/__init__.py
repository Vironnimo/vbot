"""Memory domain public API.

Exports load on first access, so importing a light member such as
``MEMORY_PROMPT_MODES`` does not import the Memory service and its file stack.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.memory._definitions import (
        DEFAULT_MEMORY_PROMPT_MODE,
        MEMORY_BLOCK_ID,
        MEMORY_BLOCK_OWNER,
        MEMORY_FILES_PRODUCER_NAME,
        MEMORY_PROMPT_MODE_AGENT,
        MEMORY_PROMPT_MODE_AGENT_USER,
        MEMORY_PROMPT_MODE_OFF,
        MEMORY_PROMPT_MODES,
        MemoryBudgetError,
        MemoryEntry,
        MemoryError,
        MemoryMatchError,
        MemoryPromptMode,
        MemoryRevertError,
        MemoryRevertIncompleteError,
        MemoryRevertResult,
        MemoryScope,
        MemoryTextChange,
        MemoryWriter,
        validate_memory_prompt_mode,
    )
    from core.memory._file_backend import (
        FilePinnedMemoryBackend,
        match_memory_entries,
    )
    from core.memory._history import (
        MemoryChange,
        MemoryRevertConflict,
        MemoryRevision,
    )
    from core.memory.memory import (
        MemoryService,
        memory_block_definition,
        read_memory_files,
    )


_EXPORT_MODULES = {
    "DEFAULT_MEMORY_PROMPT_MODE": "core.memory._definitions",
    "FilePinnedMemoryBackend": "core.memory._file_backend",
    "MEMORY_BLOCK_ID": "core.memory._definitions",
    "MEMORY_BLOCK_OWNER": "core.memory._definitions",
    "MEMORY_FILES_PRODUCER_NAME": "core.memory._definitions",
    "MEMORY_PROMPT_MODE_AGENT": "core.memory._definitions",
    "MEMORY_PROMPT_MODE_AGENT_USER": "core.memory._definitions",
    "MEMORY_PROMPT_MODE_OFF": "core.memory._definitions",
    "MEMORY_PROMPT_MODES": "core.memory._definitions",
    "MemoryBudgetError": "core.memory._definitions",
    "MemoryChange": "core.memory._history",
    "MemoryEntry": "core.memory._definitions",
    "MemoryError": "core.memory._definitions",
    "MemoryMatchError": "core.memory._definitions",
    "MemoryPromptMode": "core.memory._definitions",
    "MemoryRevertConflict": "core.memory._history",
    "MemoryRevertError": "core.memory._definitions",
    "MemoryRevertIncompleteError": "core.memory._definitions",
    "MemoryRevertResult": "core.memory._definitions",
    "MemoryRevision": "core.memory._history",
    "MemoryScope": "core.memory._definitions",
    "MemoryService": "core.memory.memory",
    "MemoryTextChange": "core.memory._definitions",
    "MemoryWriter": "core.memory._definitions",
    "match_memory_entries": "core.memory._file_backend",
    "memory_block_definition": "core.memory.memory",
    "read_memory_files": "core.memory.memory",
    "validate_memory_prompt_mode": "core.memory._definitions",
}


def __getattr__(name: str) -> Any:
    """Load public Memory exports only when a caller requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_MEMORY_PROMPT_MODE",
    "FilePinnedMemoryBackend",
    "MEMORY_BLOCK_ID",
    "MEMORY_BLOCK_OWNER",
    "MEMORY_FILES_PRODUCER_NAME",
    "MEMORY_PROMPT_MODE_AGENT",
    "MEMORY_PROMPT_MODE_AGENT_USER",
    "MEMORY_PROMPT_MODE_OFF",
    "MEMORY_PROMPT_MODES",
    "MemoryBudgetError",
    "MemoryChange",
    "MemoryEntry",
    "MemoryError",
    "MemoryMatchError",
    "MemoryPromptMode",
    "MemoryRevertConflict",
    "MemoryRevertError",
    "MemoryRevertIncompleteError",
    "MemoryRevertResult",
    "MemoryRevision",
    "MemoryScope",
    "MemoryService",
    "MemoryTextChange",
    "MemoryWriter",
    "match_memory_entries",
    "memory_block_definition",
    "read_memory_files",
    "validate_memory_prompt_mode",
]
