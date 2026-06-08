"""Tests for the recall backend registry."""

from __future__ import annotations

import pytest

from core.recall import (
    FIRST_PARTY_RECALL_BACKENDS,
    RECALL_BACKEND_HYBRID,
    RECALL_BACKEND_JSONL_SCAN,
    RECALL_BACKEND_SQLITE_FTS,
    RECALL_BACKEND_VECTOR,
    JsonlSessionRecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager


@pytest.fixture
def registry() -> RecallBackendRegistry:
    return RecallBackendRegistry.with_builtins()


@pytest.fixture
def context(tmp_path) -> RecallBackendContext:
    return RecallBackendContext(
        data_dir=tmp_path,
        sessions=ChatSessionManager(tmp_path),
    )


def test_first_party_recall_backends_include_vector() -> None:
    """The ``vector`` backend is part of the first-party backend set."""

    assert RECALL_BACKEND_VECTOR in FIRST_PARTY_RECALL_BACKENDS
    assert (
        frozenset(
            {
                RECALL_BACKEND_JSONL_SCAN,
                RECALL_BACKEND_SQLITE_FTS,
                RECALL_BACKEND_VECTOR,
                RECALL_BACKEND_HYBRID,
            }
        )
        == FIRST_PARTY_RECALL_BACKENDS
    )


def test_registry_with_builtins_registers_all_backends(
    registry: RecallBackendRegistry,
) -> None:
    assert sorted(registry.names()) == sorted(FIRST_PARTY_RECALL_BACKENDS)


def test_registry_create_returns_expected_backend_type(
    registry: RecallBackendRegistry,
    context: RecallBackendContext,
) -> None:
    assert isinstance(
        registry.create(RECALL_BACKEND_JSONL_SCAN, context), JsonlSessionRecallBackend
    )
    assert isinstance(registry.create(RECALL_BACKEND_SQLITE_FTS, context), SqliteFtsRecallBackend)
    assert isinstance(registry.create(RECALL_BACKEND_VECTOR, context), VectorRecallBackend)


def test_registry_create_unknown_backend_raises_key_error(
    registry: RecallBackendRegistry,
    context: RecallBackendContext,
) -> None:
    with pytest.raises(KeyError, match="unknown recall backend"):
        registry.create("missing", context)


def test_registry_rejects_duplicate_registration() -> None:
    registry = RecallBackendRegistry()
    registry.register("alpha", lambda context: JsonlSessionRecallBackend(context.sessions))
    with pytest.raises(ValueError, match="already registered"):
        registry.register("alpha", lambda context: JsonlSessionRecallBackend(context.sessions))


def test_registry_rejects_non_lowercase_snake_case_names() -> None:
    registry = RecallBackendRegistry()
    with pytest.raises(ValueError, match="lowercase snake_case"):
        registry.register("CamelCase", lambda context: JsonlSessionRecallBackend(context.sessions))
    with pytest.raises(ValueError, match="lowercase snake_case"):
        registry.register("Mixed_Case", lambda context: JsonlSessionRecallBackend(context.sessions))


def test_registry_passes_extended_context_to_vector_backend(
    tmp_path,
) -> None:
    """The vector factory receives the full context (embeddings + model registry)."""

    captured: dict[str, object] = {}

    class _StubEmbeddings:
        pass

    class _StubModels:
        pass

    def factory(context: RecallBackendContext) -> VectorRecallBackend:
        captured["embeddings"] = context.embeddings
        captured["model_registry"] = context.model_registry
        return VectorRecallBackend(context)

    registry = RecallBackendRegistry()
    registry.register(RECALL_BACKEND_VECTOR, factory)

    embeddings = _StubEmbeddings()
    models = _StubModels()
    context = RecallBackendContext(
        data_dir=tmp_path,
        sessions=ChatSessionManager(tmp_path),
        embeddings=embeddings,
        model_registry=models,
    )

    backend = registry.create(RECALL_BACKEND_VECTOR, context)

    assert isinstance(backend, VectorRecallBackend)
    assert captured["embeddings"] is embeddings
    assert captured["model_registry"] is models
