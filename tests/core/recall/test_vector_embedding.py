"""The ``vector`` backend's provider calls: context-overflow splitting without text changes.

``_run_embed`` is tested directly because an honest provider overflow is impractical
to provoke through a search.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.model_tasks import EmbeddingError, EmbeddingResult
from core.sessions import ChatSessionManager
from tests.core.recall.recall_test_support import StubEmbeddings, vector_backend

pytestmark = pytest.mark.asyncio

_OVERFLOW = (
    "Embeddings response contains no data: HTTP 400: This model's "
    "maximum context length is 8192 tokens. (parameter=input_tokens)"
)


class _OverflowEmbeddings(StubEmbeddings):
    """Rejects a call whose aggregate text exceeds *max_chars*, like a batch token cap.

    With *drift* every call reports a different served model.
    """

    def __init__(self, *, max_chars: int, drift: bool = False) -> None:
        super().__init__()
        self.max_chars = max_chars
        self.drift = drift

    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        if self.drift:
            self.response_model_id = f"served/embed-{len(self.embed_calls)}"
        if sum(len(text) for text in texts) > self.max_chars:
            self.embed_calls.append(list(texts))
            self.embed_purposes.append(purpose)
            raise EmbeddingError(_OVERFLOW)
        return await super().embed(texts, purpose=purpose)


class _UnauthorizedEmbeddings(StubEmbeddings):
    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        raise EmbeddingError("401 Unauthorized: invalid API key")


async def test_overflowing_batch_is_split_recursively_without_changing_text(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    embeddings = _OverflowEmbeddings(max_chars=100)
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    texts = ["a" * 60, "b" * 60, "c" * 60, "d" * 60]

    result = await recall._run_embed(texts)

    assert result.dimension == 4
    assert [list(vector) for vector in result.vectors] == [
        embeddings.vector_for(text) for text in texts
    ]
    assert embeddings.embed_calls[0] == texts
    accepted = [call for call in embeddings.embed_calls if sum(map(len, call)) <= 100]
    assert [text for call in accepted for text in call] == texts


@pytest.mark.parametrize(
    ("embeddings", "texts"),
    [
        # Only a context overflow is split; other errors are not retried.
        (_UnauthorizedEmbeddings(), ["x" * 60, "y" * 60]),
        # A single overlong text is never truncated here.
        (_OverflowEmbeddings(max_chars=100), ["x" * 1000]),
    ],
    ids=["non-overflow-error", "single-overlong-text"],
)
async def test_unsplittable_failure_raises_after_one_unchanged_call(
    tmp_path: Path, sessions: ChatSessionManager, embeddings: StubEmbeddings, texts: list[str]
) -> None:
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)

    with pytest.raises(EmbeddingError):
        await recall._run_embed(texts)

    assert embeddings.embed_calls == [texts]


async def test_split_requests_must_come_from_one_served_model(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    recall = vector_backend(
        tmp_path, sessions, embeddings=_OverflowEmbeddings(max_chars=100, drift=True)
    )

    with pytest.raises(EmbeddingError, match="response model changed mid-batch"):
        await recall._run_embed(["a" * 60, "b" * 60])
