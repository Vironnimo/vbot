"""Session recall read-model backends."""

from core.recall.jsonl import JsonlSessionRecallBackend
from core.recall.recall import (
    DEFAULT_RECALL_BACKEND,
    FIRST_PARTY_RECALL_BACKENDS,
    RECALL_BACKEND_JSONL_SCAN,
    RECALL_BACKEND_SQLITE_FTS,
    RECALL_BACKEND_VECTOR,
    JsonObject,
    RecallBackend,
    RecallBackendContext,
    RecallBackendFactory,
    RecallBackendRegistry,
    RecallMatchMode,
    RecallRequest,
    RecallSortMode,
)
from core.recall.sqlite_fts import SqliteFtsRecallBackend
from core.recall.vector import VectorRecallBackend
from core.recall.vector_store import (
    SessionVectorRecord,
    VectorHeader,
    VectorStore,
    VectorStoreError,
)

__all__ = [
    "DEFAULT_RECALL_BACKEND",
    "FIRST_PARTY_RECALL_BACKENDS",
    "JsonObject",
    "JsonlSessionRecallBackend",
    "RECALL_BACKEND_JSONL_SCAN",
    "RECALL_BACKEND_SQLITE_FTS",
    "RECALL_BACKEND_VECTOR",
    "RecallBackend",
    "RecallBackendContext",
    "RecallBackendFactory",
    "RecallBackendRegistry",
    "RecallMatchMode",
    "RecallRequest",
    "RecallSortMode",
    "SessionVectorRecord",
    "SqliteFtsRecallBackend",
    "VectorHeader",
    "VectorRecallBackend",
    "VectorStore",
    "VectorStoreError",
]
