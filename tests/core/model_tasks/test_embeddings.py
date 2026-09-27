"""Tests for the provider-neutral ``EmbeddingService``.

The Provider client is replaced by a recording fake; its wire behavior is
covered in ``test_embeddings_providers.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.model_tasks import (
    EmbeddingConfigurationError,
    EmbeddingExecutionError,
    EmbeddingService,
    EmbeddingUnsupportedTargetError,
    TaskModelError,
)
from core.model_tasks.embeddings_providers import (
    EmbeddingUsage,
    ProviderEmbeddingResponse,
)
from core.providers.errors import ProviderAuthError

OPENROUTER_TARGET = "openrouter/google/gemini-embedding-2::api-key"


class _ModelTasks:
    """Stand-in for ``TaskModelService`` with one ``text_embedding`` binding."""

    def __init__(self, target: str | None, options: dict[str, Any] | None = None) -> None:
        self._target = target
        self._options = options or {}

    def binding_for(self, task_type: str) -> Any:
        if self._target is None:
            raise TaskModelError("No task model configured for text_embedding")
        return SimpleNamespace(task_type=task_type, target=self._target, options=self._options)

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: Any) -> dict[str, Any]:
        return {"dimensions": None, **self._options}


class _FakeProviderClient:
    """Records every ``embed`` call and returns or raises the configured result."""

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        embed_exception: Exception | None = None,
        model_id: str | None = None,
        usage: EmbeddingUsage | None = None,
    ) -> None:
        self._vectors = vectors if vectors is not None else [[0.1]]
        self._embed_exception = embed_exception
        self._model_id = model_id
        self._usage = usage or EmbeddingUsage(requests=1)
        self.embed_calls: list[tuple[list[str], dict[str, Any], str | None]] = []

    async def embed(
        self, inputs: list[str], options: dict[str, Any], purpose: str | None = None
    ) -> ProviderEmbeddingResponse:
        self.embed_calls.append((list(inputs), dict(options), purpose))
        if self._embed_exception is not None:
            raise self._embed_exception
        return ProviderEmbeddingResponse(
            vectors=tuple(list(vector) for vector in self._vectors),
            model_id=self._model_id,
            usage=self._usage,
        )


def _service(target: str | None = OPENROUTER_TARGET, **options: Any) -> EmbeddingService:
    return EmbeddingService(_ModelTasks(target, options), MagicMock(name="runtime"))


@pytest.fixture
def provider_factory() -> Iterator[MagicMock]:
    with patch("core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime") as factory:
        factory.return_value = _FakeProviderClient()
        yield factory


@pytest.mark.parametrize(
    ("texts", "purpose"),
    [
        pytest.param([], None, id="empty-list"),
        pytest.param("not-a-list", None, id="not-a-list"),
        pytest.param(["ok", 42, "also-ok"], None, id="non-string-element"),
        pytest.param(["query"], "classification", id="unknown-purpose"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_embed_request_fails_before_provider_execution(
    provider_factory: MagicMock, texts: Any, purpose: Any
) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        await _service().embed(texts, purpose=purpose)

    assert provider_factory.return_value.embed_calls == []


@pytest.mark.parametrize(
    ("target", "error_type"),
    [
        pytest.param(None, EmbeddingConfigurationError, id="no-binding"),
        pytest.param("not-a-valid-target", EmbeddingConfigurationError, id="malformed-target"),
        pytest.param("local/embedding", EmbeddingUnsupportedTargetError, id="local-target"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_binding_fails_embedding_and_space_resolution(
    provider_factory: MagicMock, target: str | None, error_type: type[Exception]
) -> None:
    service = _service(target)

    with pytest.raises(error_type):
        await service.embed(["alpha"])
    with pytest.raises(error_type):
        service.resolve_space()
    assert provider_factory.return_value.embed_calls == []


@pytest.mark.parametrize(
    ("response_model_id", "actual_model_id"),
    [
        pytest.param(None, "google/gemini-embedding-2", id="configured-model"),
        pytest.param(
            "google/gemini-embedding-2-202607",
            "google/gemini-embedding-2-202607",
            id="provider-reported-model",
        ),
    ],
)
@pytest.mark.asyncio
async def test_embed_returns_ordered_vectors_with_the_resolved_identity(
    provider_factory: MagicMock, response_model_id: str | None, actual_model_id: str
) -> None:
    usage = EmbeddingUsage(requests=1, token_reports=1, input_tokens=9, total_tokens=9)
    client = _FakeProviderClient(
        vectors=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], model_id=response_model_id, usage=usage
    )
    provider_factory.return_value = client
    service = _service(dimensions=3)

    result = await service.embed(["alpha", "beta"], purpose="query")

    assert result.vectors == ([0.1, 0.2, 0.3], [0.4, 0.5, 0.6])
    assert (result.provider_id, result.model_id, result.dimension) == (
        "openrouter",
        "google/gemini-embedding-2",
        3,
    )
    assert result.actual_model_id == actual_model_id
    assert result.usage == usage
    assert result.space_fingerprint == service.resolve_space().fingerprint
    # Inputs travel verbatim, with the binding's effective options and the purpose.
    assert client.embed_calls == [(["alpha", "beta"], {"dimensions": 3}, "query")]
    _runtime, target_ref = provider_factory.call_args.args
    assert (
        target_ref.provider_id,
        target_ref.model_id,
        target_ref.connection_id,
        target_ref.local_connection_id,
    ) == ("openrouter", "google/gemini-embedding-2", "openrouter:api-key", "api-key")


@pytest.mark.parametrize(
    ("client", "message", "log_level", "logs_traceback"),
    [
        pytest.param(
            _FakeProviderClient(embed_exception=ProviderAuthError("Unauthorized")),
            "Unauthorized",
            logging.WARNING,
            False,
            id="expected-provider-failure",
        ),
        pytest.param(
            _FakeProviderClient(embed_exception=RuntimeError("kaboom")),
            "kaboom",
            logging.ERROR,
            True,
            id="unexpected-failure",
        ),
        pytest.param(_FakeProviderClient(vectors=[]), "no vectors", None, False, id="no-vectors"),
    ],
)
@pytest.mark.asyncio
async def test_failed_provider_execution_is_an_execution_error(
    provider_factory: MagicMock,
    caplog: pytest.LogCaptureFixture,
    client: _FakeProviderClient,
    message: str,
    log_level: int | None,
    logs_traceback: bool,
) -> None:
    provider_factory.return_value = client

    with (
        caplog.at_level(logging.WARNING, logger="vbot.embeddings"),
        pytest.raises(EmbeddingExecutionError, match=message),
    ):
        await _service().embed(["alpha"])

    logged = [
        (record.levelno, record.exc_info is not None)
        for record in caplog.records
        if record.getMessage().startswith("Embedding request")
    ]
    assert logged == ([] if log_level is None else [(log_level, logs_traceback)])


def test_space_fingerprint_covers_target_and_effective_options() -> None:
    def space(target: str, **options: Any) -> Any:
        return _service(target, **options).resolve_space()

    api_key = "openrouter/google/gemini-embedding-2::api-key:work"
    baseline = space(api_key, dimensions=768, extra_options={"user": "recall"})

    assert (baseline.provider_id, baseline.model_id) == ("openrouter", "google/gemini-embedding-2")
    assert space(api_key, extra_options={"user": "recall"}, dimensions=768) == baseline
    assert (
        space(
            "openrouter/google/gemini-embedding-2::oauth:work",
            dimensions=768,
            extra_options={"user": "recall"},
        ).fingerprint
        != baseline.fingerprint
    )
    assert (
        space(api_key, dimensions=256, extra_options={"user": "recall"}).fingerprint
        != baseline.fingerprint
    )
