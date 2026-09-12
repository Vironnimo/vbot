"""Vector: embedding behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
)
from core.recall.vector import _EMBED_BATCH_SIZE
from core.sessions import ChatSessionManager
from tests.core.recall.vector_helpers import (
    _StubEmbeddings,
    backend,
)

pytestmark = pytest.mark.asyncio


class _OverflowThenOkEmbeddings:
    """Raises a context-length overflow until the aggregate batch fits.

    This models providers that apply the token cap to one whole batch. Every
    individual text can fit unchanged once the backend recursively divides the
    request.
    """

    def __init__(self, *, max_chars: int, dimension: int = 4) -> None:
        self.max_chars = max_chars
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []

    def resolve_model_id(self) -> tuple[str, str]:
        return (self.provider_id, self.model_id)

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        del purpose
        self.embed_calls.append(list(texts))
        if sum(len(text) for text in texts) > self.max_chars:
            raise EmbeddingError(
                "Embeddings response contains no data: HTTP 400: This model's "
                "maximum context length is 8192 tokens. (parameter=input_tokens)"
            )
        vectors = tuple([1.0] + [0.0] * (self.dimension - 1) for _ in texts)
        return EmbeddingResult(
            vectors=vectors,
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
        )


class _AuthErrorEmbeddings:
    """Always raises a non-overflow embedding error (must not be retried)."""

    def __init__(self) -> None:
        self.embed_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        return ("openrouter", "stub-embed")

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        del texts, purpose
        self.embed_calls += 1
        raise EmbeddingError("401 Unauthorized: invalid API key")


async def test_run_embed_recursively_splits_overflowing_batch_without_changing_text(
    tmp_path: Path,
) -> None:
    """Aggregate overflow is recovered by dividing inputs, never their text."""

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=100)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    texts = ["a" * 60, "b" * 60, "c" * 60, "d" * 60]

    result = await recall._run_embed(texts)

    assert result.dimension == 4
    assert len(result.vectors) == len(texts)
    assert embeddings.embed_calls[0] == texts
    successful_calls = [call for call in embeddings.embed_calls if sum(map(len, call)) <= 100]
    assert [text for call in successful_calls for text in call] == texts


async def test_run_embed_does_not_retry_non_overflow_errors(tmp_path: Path) -> None:
    """Auth/network errors are not context-length overflows and must not be
    retried by the shrink loop — they re-raise on the first attempt.
    """

    sessions = ChatSessionManager(tmp_path)
    embeddings = _AuthErrorEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    with pytest.raises(EmbeddingError, match="Unauthorized"):
        await recall._run_embed(["x" * 1000])
    assert embeddings.embed_calls == 1


async def test_run_embed_never_truncates_a_single_overlong_text(tmp_path: Path) -> None:
    """A single overflow re-raises after one honest, unchanged provider call."""

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=100)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    text = "x" * 1000

    with pytest.raises(EmbeddingError):
        await recall._run_embed([text])
    assert embeddings.embed_calls == [[text]]


# _run_embed batching
async def test_run_embed_splits_texts_into_batches_above_batch_size(tmp_path: Path) -> None:
    """When ``texts > _EMBED_BATCH_SIZE`` the embedder receives multiple, ordered calls."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    # Two full batches plus a partial third batch.
    total = _EMBED_BATCH_SIZE + _EMBED_BATCH_SIZE + 3
    texts = [f"text-{index}" for index in range(total)]
    result = await recall._run_embed(texts)

    # Three separate calls — one per batch.
    assert embeddings.batch_sizes == [
        _EMBED_BATCH_SIZE,
        _EMBED_BATCH_SIZE,
        3,
    ]
    # Vectors arrive in input order: ``text-0`` is first, ``text-total-1`` last.
    assert len(result.vectors) == total
    # The default stub vector for "text-0" should equal the one for
    # "text-0" computed in isolation — same input → same output.
    assert result.vectors[0] == embeddings._vector_for("text-0")
    assert result.vectors[-1] == embeddings._vector_for(texts[-1])


async def test_run_embed_single_text_does_not_split(tmp_path: Path) -> None:
    """A single text fits in one batch — no splitting overhead."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    result = await recall._run_embed(["only-text"])

    assert embeddings.batch_sizes == [1]
    assert len(result.vectors) == 1
