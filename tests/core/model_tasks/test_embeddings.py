"""Tests for the provider-neutral ``EmbeddingService``.

The Provider client and the local embedding engine are replaced by recording
fakes; their own behavior is covered in ``test_embeddings_providers.py`` and
``test_embeddings_local.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.model_tasks import (
    EmbeddingConfigurationError,
    EmbeddingExecutionError,
    EmbeddingInputTooLongError,
    EmbeddingService,
    EmbeddingUnsupportedTargetError,
    TaskModelError,
)
from core.model_tasks.embedding_profiles import (
    EMBEDDING_PROFILE_CONTRACT_VERSION,
    embedding_profile,
)
from core.model_tasks.embeddings_local import (
    LocalEmbeddingError,
    LocalEmbeddingInputTooLongError,
    LocalEmbeddingModel,
    LocalEmbeddingOutput,
    LocalEmbeddingUnavailableError,
    builtin_local_embedding_models,
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
        self, inputs: list[str], options: dict[str, Any], input_type: str | None = None
    ) -> ProviderEmbeddingResponse:
        self.embed_calls.append((list(inputs), dict(options), input_type))
        if self._embed_exception is not None:
            raise self._embed_exception
        return ProviderEmbeddingResponse(
            vectors=tuple(list(vector) for vector in self._vectors),
            model_id=self._model_id,
            usage=self._usage,
        )


class _LocalEngine:
    """Stand-in for the local embedding engine; a text's vector is ``[len(text), 1.0]``."""

    def __init__(self, *, installed: bool = True, error: Exception | None = None) -> None:
        self.models = {model.id: model for model in builtin_local_embedding_models()}
        self.installed = installed
        self.error = error
        self.calls: list[tuple[str, list[str], bool, dict[str, Any]]] = []

    def model(self, local_id: str) -> LocalEmbeddingModel:
        try:
            return self.models[local_id]
        except KeyError:
            raise LocalEmbeddingUnavailableError("Unknown local embedding model") from None

    def check_available(self, local_id: str) -> None:
        if not self.installed:
            raise LocalEmbeddingUnavailableError("Install it in Settings, then retry.")

    async def embed(
        self, local_id: str, texts: Sequence[str], *, query: bool, options: Mapping[str, Any]
    ) -> LocalEmbeddingOutput:
        self.calls.append((local_id, list(texts), query, dict(options)))
        if self.error is not None:
            raise self.error
        return LocalEmbeddingOutput(
            vectors=tuple([float(len(text)), 1.0] for text in texts),
            tokens=tuple(len(text) for text in texts),
        )

    def document_batch_size(self, local_id: str) -> int:
        return self.model(local_id).document_batch_size


def _service(
    target: str | None = OPENROUTER_TARGET,
    *,
    local: _LocalEngine | None = None,
    recorder: Any = None,
    **options: Any,
) -> EmbeddingService:
    return EmbeddingService(
        _ModelTasks(target, options),
        MagicMock(name="runtime"),
        usage_recorder=recorder,
        local_executor=cast(Any, local),
    )


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
    assert service.document_batch_size() is None
    # Inputs travel with the binding's effective options and the family's query mode.
    assert client.embed_calls == [(["alpha", "beta"], {"dimensions": 3}, "search_query")]
    _runtime, target_ref = provider_factory.call_args.args
    assert (
        target_ref.provider_id,
        target_ref.model_id,
        target_ref.connection_id,
        target_ref.local_connection_id,
    ) == ("openrouter", "google/gemini-embedding-2", "openrouter:api-key", "api-key")


_QWEN_QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
)


@pytest.mark.parametrize(
    ("model_id", "purpose", "sent_inputs", "input_type"),
    [
        pytest.param(
            "google/gemini-embedding-001",
            "document",
            ["alpha"],
            "search_document",
            id="input-type-family",
        ),
        pytest.param(
            "qwen/qwen3-embedding-8b",
            "query",
            [f"{_QWEN_QUERY_PREFIX}alpha"],
            None,
            id="query-instruction-family",
        ),
        pytest.param(
            "qwen/qwen3-embedding-8b", "document", ["alpha"], None, id="plain-document-side"
        ),
        pytest.param(
            "intfloat/multilingual-e5-large",
            "document",
            ["passage: alpha"],
            None,
            id="document-prefix-family",
        ),
        pytest.param(
            "openai/text-embedding-3-small", "query", ["alpha"], None, id="symmetric-family"
        ),
        pytest.param("example/unknown-embed", "query", ["alpha"], None, id="unknown-model"),
        pytest.param("intfloat/multilingual-e5-large", None, ["alpha"], None, id="no-purpose"),
    ],
)
@pytest.mark.asyncio
async def test_embed_applies_the_model_family_purpose_handling(
    provider_factory: MagicMock,
    model_id: str,
    purpose: Any,
    sent_inputs: list[str],
    input_type: str | None,
) -> None:
    texts = ["alpha"]

    await _service(f"openrouter/{model_id}::api-key").embed(texts, purpose=purpose)

    assert provider_factory.return_value.embed_calls == [
        (sent_inputs, {"dimensions": None}, input_type)
    ]
    # Prefixes exist only on the wire; the caller's text is never rewritten.
    assert texts == ["alpha"]


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


def test_space_fingerprint_covers_target_options_and_family_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    # The Model family's purpose handling and its profile contract version
    # shape every request, so changing either starts a new space.
    profile = embedding_profile("google/gemini-embedding-2")
    with monkeypatch.context() as patched:
        patched.setattr(
            "core.model_tasks.embeddings.embedding_profile",
            lambda _model_id: replace(profile, query="retrieval_query"),
        )
        changed_handling = space(api_key, dimensions=768, extra_options={"user": "recall"})
    with monkeypatch.context() as patched:
        patched.setattr(
            "core.model_tasks.embedding_profiles.EMBEDDING_PROFILE_CONTRACT_VERSION",
            EMBEDDING_PROFILE_CONTRACT_VERSION + 1,
        )
        changed_version = space(api_key, dimensions=768, extra_options={"user": "recall"})
    assert baseline.fingerprint not in {changed_handling.fingerprint, changed_version.fingerprint}


_HARRIER_QUERY = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
    "Query: alpha"
)


@pytest.mark.parametrize(
    ("target", "purpose", "sent", "query"),
    [
        pytest.param("local/harrier-0.6b", "query", _HARRIER_QUERY, True, id="query-prefix"),
        pytest.param("local/harrier-0.6b", "document", "alpha", False, id="plain-document"),
        pytest.param("local/granite-embedding-r2", "query", "alpha", True, id="symmetric"),
    ],
)
@pytest.mark.asyncio
async def test_local_target_embeds_on_this_machine_at_no_cost(
    provider_factory: MagicMock, target: str, purpose: Any, sent: str, query: bool
) -> None:
    local = _LocalEngine()
    recorder = AsyncMock()
    recorder.start.return_value = "call-1"
    service = _service(target, local=local, recorder=recorder, threads=3)

    result = await service.embed(["alpha"], purpose=purpose)

    local_id = target.removeprefix("local/")
    # The engine embeds exactly the profile-shaped text, with the binding's options.
    assert local.calls == [(local_id, [sent], query, {"dimensions": None, "threads": 3})]
    assert provider_factory.return_value.embed_calls == []
    assert (result.provider_id, result.model_id, result.dimension) == ("local", local_id, 2)
    assert result.vectors == ([float(len(sent)), 1.0],)
    assert result.space_fingerprint == service.resolve_space().fingerprint
    assert result.usage.input_tokens == len(sent) and result.usage.cost == 0.0
    assert service.resolve_space().local
    assert service.document_batch_size() == 8
    # Every local call is accounted under its target with its tokens and zero cost.
    assert recorder.start.await_args.kwargs["model"] == target
    assert recorder.start.await_args.kwargs["kind"] == "text_embedding"
    recorder.update.assert_awaited_once_with(
        "call-1", {"input_tokens": len(sent), "output_tokens": 0, "reported_cost_usd": 0.0}
    )
    assert recorder.finish.await_args.kwargs["status"] == "completed"


@pytest.mark.parametrize(
    ("target", "local", "error_type", "accounted"),
    [
        pytest.param(
            "local/granite-embedding-r2",
            _LocalEngine(installed=False),
            EmbeddingConfigurationError,
            False,
            id="not-installed",
        ),
        pytest.param(
            "local/granite-embedding-r2",
            _LocalEngine(error=LocalEmbeddingInputTooLongError(0, 3000, 2048)),
            EmbeddingInputTooLongError,
            True,
            id="input-too-long",
        ),
        pytest.param(
            "local/granite-embedding-r2",
            _LocalEngine(error=LocalEmbeddingError("The local embedding model failed")),
            EmbeddingExecutionError,
            True,
            id="engine-failure",
        ),
        pytest.param(
            "local/unknown-model",
            _LocalEngine(),
            EmbeddingUnsupportedTargetError,
            False,
            id="unknown-model",
        ),
    ],
)
@pytest.mark.asyncio
async def test_failed_local_execution_is_an_embedding_error(
    target: str, local: _LocalEngine, error_type: type[Exception], accounted: bool
) -> None:
    recorder = AsyncMock()
    recorder.start.return_value = "call-1"

    with pytest.raises(error_type):
        await _service(target, local=local, recorder=recorder).embed(["alpha"])

    assert recorder.start.await_count == int(accounted)
    if accounted:
        assert recorder.finish.await_args.kwargs["status"] == "failed"


def test_local_space_fingerprint_covers_the_pinned_model_and_profile_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def space(
        target: str = "local/harrier-0.6b", local: _LocalEngine | None = None, **options: Any
    ) -> Any:
        return _service(target, local=local or _LocalEngine(), **options).resolve_space()

    baseline = space(threads=2)

    assert (baseline.provider_id, baseline.model_id, baseline.local) == (
        "local",
        "harrier-0.6b",
        True,
    )
    # Runtime options such as the thread count never change the vectors.
    assert space(threads=7) == baseline
    assert space("local/granite-embedding-r2").fingerprint != baseline.fingerprint
    moved = _LocalEngine()
    moved.models["harrier-0.6b"] = replace(moved.models["harrier-0.6b"], revision="0" * 40)
    assert space(local=moved).fingerprint != baseline.fingerprint
    profile = embedding_profile("harrier-0.6b")
    with monkeypatch.context() as patched:
        patched.setattr(
            "core.model_tasks.embeddings.embedding_profile",
            lambda _model_id: replace(profile, query="Query: "),
        )
        assert space().fingerprint != baseline.fingerprint
