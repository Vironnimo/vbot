"""Session recall read-model backends."""

from core.recall.canonical import CanonicalSessionRecallBackend
from core.recall.hybrid import HybridRecallBackend
from core.recall.passage_index import PassageIndex, PassageIndexError, VectorHeader
from core.recall.recall import (
    DEFAULT_RECALL_BACKEND,
    FIRST_PARTY_RECALL_BACKENDS,
    RECALL_BACKEND_CANONICAL_SCAN,
    RECALL_BACKEND_HYBRID,
    RECALL_BACKEND_SQLITE_FTS,
    RECALL_BACKEND_VECTOR,
    SEMANTIC_RECALL_BACKENDS,
    JsonObject,
    RecallBackend,
    RecallBackendContext,
    RecallBackendFactory,
    RecallBackendRegistry,
    RecallMatchMode,
    RecallOrder,
    RecallResultType,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    SupportsClose,
    SupportsSessionRemoval,
)
from core.recall.semantic_indexer import IndexFailure, IndexStatus, SemanticIndexer
from core.recall.sqlite_fts import SqliteFtsRecallBackend
from core.recall.vector import VectorRecallBackend

__all__ = [
    "DEFAULT_RECALL_BACKEND",
    "FIRST_PARTY_RECALL_BACKENDS",
    "HybridRecallBackend",
    "IndexFailure",
    "IndexStatus",
    "JsonObject",
    "CanonicalSessionRecallBackend",
    "PassageIndex",
    "PassageIndexError",
    "RECALL_BACKEND_HYBRID",
    "RECALL_BACKEND_CANONICAL_SCAN",
    "RECALL_BACKEND_SQLITE_FTS",
    "RECALL_BACKEND_VECTOR",
    "RecallBackend",
    "RecallBackendContext",
    "RecallBackendFactory",
    "RecallBackendRegistry",
    "RecallMatchMode",
    "RecallOrder",
    "RecallResultType",
    "RecallSearchError",
    "RecallSearchCapabilities",
    "RecallSearchHit",
    "RecallSearchPage",
    "RecallSearchRequest",
    "SEMANTIC_RECALL_BACKENDS",
    "SemanticIndexer",
    "SupportsClose",
    "SupportsSessionRemoval",
    "SqliteFtsRecallBackend",
    "VectorHeader",
    "VectorRecallBackend",
]
