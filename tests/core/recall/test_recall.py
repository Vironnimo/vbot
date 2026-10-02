"""Recall backend registry: built-ins, registration rules and the backend contract."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest

from core.recall import (
    FIRST_PARTY_RECALL_BACKENDS,
    RECALL_BACKEND_CANONICAL_SCAN,
    RECALL_BACKEND_HYBRID,
    RECALL_BACKEND_SQLITE_FTS,
    RECALL_BACKEND_VECTOR,
    CanonicalSessionRecallBackend,
    HybridRecallBackend,
    PassageIndex,
    RecallBackendContext,
    RecallBackendRegistry,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager


@pytest.fixture
def context(tmp_path: Path, sessions: ChatSessionManager) -> RecallBackendContext:
    return RecallBackendContext(data_dir=tmp_path, sessions=sessions)


def _canonical(context: RecallBackendContext) -> CanonicalSessionRecallBackend:
    return CanonicalSessionRecallBackend(context.sessions)


def test_builtins_create_the_first_party_backends_over_one_passage_index(
    context: RecallBackendContext,
) -> None:
    expected = {
        RECALL_BACKEND_SQLITE_FTS: SqliteFtsRecallBackend,
        RECALL_BACKEND_VECTOR: VectorRecallBackend,
        RECALL_BACKEND_HYBRID: HybridRecallBackend,
    }
    index = PassageIndex(context.data_dir)
    registry = RecallBackendRegistry.with_builtins(passage_index=index)

    assert frozenset(expected) == FIRST_PARTY_RECALL_BACKENDS
    assert registry.names() == sorted(expected)
    for name, backend_type in expected.items():
        assert isinstance(registry.create(name, context), backend_type)
    # The semantic backends share the Runtime's index instead of opening their own.
    vector = registry.create(RECALL_BACKEND_VECTOR, context)
    hybrid = registry.create(RECALL_BACKEND_HYBRID, context)
    assert isinstance(vector, VectorRecallBackend) and vector.index is index
    assert isinstance(hybrid, HybridRecallBackend) and hybrid.index is index
    # The internal live scan is not selectable.
    for name in (RECALL_BACKEND_CANONICAL_SCAN, "missing"):
        with pytest.raises(KeyError):
            registry.create(name, context)
    index.close()


@pytest.mark.parametrize(
    "name",
    ["CamelCase", "Mixed_Case", "   ", RECALL_BACKEND_CANONICAL_SCAN, "alpha"],
    ids=["camel-case", "mixed-case", "blank", "reserved", "duplicate"],
)
def test_register_rejects_invalid_reserved_and_duplicate_names(name: str) -> None:
    registry = RecallBackendRegistry()
    registry.register("alpha", _canonical)

    with pytest.raises(ValueError):
        registry.register(name, _canonical)

    assert registry.names() == ["alpha"]


class _InvalidCapabilities(CanonicalSessionRecallBackend):
    @override
    def search_capabilities(self) -> Any:
        return {"result_unit": "message"}


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda context: object(), "must implement search_capabilities and search_page"),
        (
            lambda context: _InvalidCapabilities(context.sessions),
            "returned invalid search capabilities",
        ),
    ],
    ids=["missing-methods", "invalid-capabilities"],
)
def test_create_rejects_a_backend_without_the_search_contract(
    context: RecallBackendContext,
    factory: Callable[[RecallBackendContext], Any],
    message: str,
) -> None:
    registry = RecallBackendRegistry()
    registry.register("extension", factory)

    with pytest.raises(ValueError, match=message):
        registry.create("extension", context)
