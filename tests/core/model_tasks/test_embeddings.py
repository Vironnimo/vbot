"""Tests for the provider-neutral ``EmbeddingService``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.model_tasks import (
    EmbeddingConfigurationError,
    EmbeddingExecutionError,
    EmbeddingService,
    EmbeddingUnsupportedTargetError,
    TaskModelError,
)

# ---------------------------------------------------------------------------
# Configuration: no binding / malformed binding / unsupported target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_without_configured_binding_raises_configuration_error() -> None:
    """A missing ``text_embedding`` binding is an expected configuration error."""

    service = EmbeddingService(_MissingModelTasks(), _RuntimeStub())

    with pytest.raises(EmbeddingConfigurationError, match="configured"):
        await service.embed(["a", "b"])


@pytest.mark.asyncio
async def test_embed_with_local_target_raises_unsupported_target_error() -> None:
    """A local embedding target is out of scope for this iteration."""

    service = EmbeddingService(
        _BindingModelTasks(target="local/whisper", options={}),
        _RuntimeStub(),
    )

    with pytest.raises(EmbeddingUnsupportedTargetError, match="local"):
        await service.embed(["a", "b"])


@pytest.mark.asyncio
async def test_embed_with_malformed_target_raises_configuration_error() -> None:
    """A target that does not parse (e.g. wrong shape) is a configuration error."""

    service = EmbeddingService(
        _BindingModelTasks(target="not-a-valid-target", options={}),
        _RuntimeStub(),
    )

    with pytest.raises(EmbeddingConfigurationError):
        await service.embed(["a"])


@pytest.mark.asyncio
async def test_embed_with_empty_input_raises_configuration_error() -> None:
    """An empty input list is a configuration error — there is nothing to embed."""

    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with pytest.raises(EmbeddingConfigurationError, match="non-empty"):
        await service.embed([])


@pytest.mark.asyncio
async def test_embed_with_non_list_input_raises_configuration_error() -> None:
    """A non-list input is rejected as a configuration error."""

    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with pytest.raises(EmbeddingConfigurationError, match="non-empty"):
        await service.embed("not-a-list")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_embed_with_non_string_element_raises_configuration_error() -> None:
    """A non-string element in the input list is rejected as a configuration error."""

    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    mixed_inputs: list[Any] = ["ok", 42, "also-ok"]
    with pytest.raises(EmbeddingConfigurationError, match="not a string"):
        await service.embed(mixed_inputs)


# ---------------------------------------------------------------------------
# Happy path: vectors returned in input order, model id surfaced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_returns_vectors_in_input_order_and_resolves_model_id() -> None:
    """A successful embed returns vectors in input order, with the
    resolved ``(provider_id, model_id)`` for the recall store to pin.
    """

    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with patch(
        "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
        return_value=_FakeProviderClient(vectors=vectors),
    ) as factory:
        result = await service.embed(["alpha", "beta"])

    assert result.vectors == ([0.1, 0.2, 0.3], [0.4, 0.5, 0.6])
    assert result.provider_id == "openrouter"
    assert result.model_id == "google/gemini-embedding-2"
    assert result.dimension == 3
    assert result.resolved_model_id == ("openrouter", "google/gemini-embedding-2")

    # The factory was called with the parsed target reference and the
    # runtime (the service does not pass the binding or options into
    # the factory — those are forwarded into the embed call instead).
    factory.assert_called_once()
    runtime_arg, target_ref = factory.call_args.args
    assert target_ref.provider_id == "openrouter"
    assert target_ref.model_id == "google/gemini-embedding-2"
    assert target_ref.connection_id == "openrouter:api-key"
    assert target_ref.local_connection_id == "api-key"


@pytest.mark.asyncio
async def test_embed_merges_schema_defaults_with_stored_options() -> None:
    """Stored options are merged over backend schema defaults before the
    wire call. The recall store passes the schema default for
    ``dimensions``; the user can override it from settings."""

    service = EmbeddingService(
        _BindingModelTasks(
            target="openrouter/google/gemini-embedding-2::api-key",
            options={"dimensions": 2},
        ),
        _RuntimeStub(),
    )

    fake_client = _FakeProviderClient(vectors=[[0.1, 0.2]])
    with patch(
        "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
        return_value=fake_client,
    ):
        await service.embed(["alpha"])

    # The fake client records every (inputs, options) pair it was
    # asked to embed. The stored options dict is merged over the
    # schema default; the resulting ``{"dimensions": 2}`` reaches the
    # provider client.
    assert fake_client.embed_calls == [(["alpha"], {"dimensions": 2})]


@pytest.mark.asyncio
async def test_embed_forwards_input_batch_verbatim() -> None:
    """The service forwards the input list verbatim to the provider client.

    The recall store relies on input order — the wire layer sorts
    vectors by ``index`` defensively, but the contract here is that
    the service does not reorder inputs.
    """

    fake_client = _FakeProviderClient(vectors=[[0.1], [0.2], [0.3]])
    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with patch(
        "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
        return_value=fake_client,
    ):
        await service.embed(["x", "y", "z"])

    assert fake_client.embed_calls[0][0] == ["x", "y", "z"]


# ---------------------------------------------------------------------------
# Error mapping: provider failures become EmbeddingExecutionError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_maps_provider_auth_error_to_execution_error() -> None:
    """A :class:`ProviderError` (auth, rate limit, …) is wrapped as
    :class:`EmbeddingExecutionError` by the service.
    """

    from core.providers.errors import ProviderAuthError

    fake_client = _FakeProviderClient(embed_exception=ProviderAuthError("Unauthorized"))
    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with (
        patch(
            "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
            return_value=fake_client,
        ),
        pytest.raises(EmbeddingExecutionError, match="Unauthorized"),
    ):
        await service.embed(["alpha"])


@pytest.mark.asyncio
async def test_embed_maps_unexpected_exception_to_execution_error() -> None:
    """A non-:class:`VBotError` exception is also wrapped as
    :class:`EmbeddingExecutionError` so callers see one error type.
    """

    fake_client = _FakeProviderClient(embed_exception=RuntimeError("kaboom"))
    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with (
        patch(
            "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
            return_value=fake_client,
        ),
        pytest.raises(EmbeddingExecutionError, match="kaboom"),
    ):
        await service.embed(["alpha"])


@pytest.mark.asyncio
async def test_embed_raises_execution_error_when_no_vectors_returned() -> None:
    """A successful HTTP call that yields zero vectors surfaces as
    :class:`EmbeddingExecutionError` — the recall store would silently
    record a corrupt batch otherwise.
    """

    fake_client = _FakeProviderClient(vectors=[])
    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    with (
        patch(
            "core.model_tasks.embeddings.ProviderEmbeddingClient.from_runtime",
            return_value=fake_client,
        ),
        pytest.raises(EmbeddingExecutionError, match="no vectors"),
    ):
        await service.embed(["alpha"])


# ---------------------------------------------------------------------------
# resolve_model_id: identity pinning for the recall store
# ---------------------------------------------------------------------------


def test_resolve_model_id_returns_provider_and_model_id() -> None:
    """``resolve_model_id`` returns the ``(provider_id, model_id)`` tuple
    for the configured binding without executing a request.
    """

    service = EmbeddingService(
        _BindingModelTasks(target="openrouter/google/gemini-embedding-2::api-key", options={}),
        _RuntimeStub(),
    )

    assert service.resolve_model_id() == ("openrouter", "google/gemini-embedding-2")


def test_resolve_model_id_without_binding_raises_configuration_error() -> None:
    """``resolve_model_id`` raises the same configuration error as
    :meth:`embed` when the binding is missing.
    """

    service = EmbeddingService(_MissingModelTasks(), _RuntimeStub())

    with pytest.raises(EmbeddingConfigurationError, match="configured"):
        service.resolve_model_id()


def test_resolve_model_id_with_local_target_raises_unsupported_target_error() -> None:
    """A local target is rejected at ``resolve_model_id`` time too —
    the recall store uses the result to pin identity, and a local
    engine is not a real provider binding for this iteration.
    """

    service = EmbeddingService(
        _BindingModelTasks(target="local/embedding", options={}),
        _RuntimeStub(),
    )

    with pytest.raises(EmbeddingUnsupportedTargetError, match="local"):
        service.resolve_model_id()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _MissingModelTasks:
    """Stand-in for ``TaskModelService`` with no configured binding."""

    def binding_for(self, _task_type: str) -> Any:
        raise TaskModelError("No task model configured for text_embedding")

    def options_with_defaults(self, _binding: Any) -> dict[str, Any]:
        return {}


class _BindingModelTasks:
    """Stand-in for ``TaskModelService`` with a fixed binding payload."""

    def __init__(self, *, target: str, options: dict[str, Any]) -> None:
        self._target = target
        self._options = options

    def binding_for(self, task_type: str) -> Any:
        return SimpleNamespace(task_type=task_type, target=self._target, options=self._options)

    def options_with_defaults(self, _binding: Any) -> dict[str, Any]:
        # The embedding option schema currently has a single field
        # (``dimensions``) that defaults to ``None``. The wire layer
        # drops ``None`` before sending, so the stored options dict
        # arrives here with ``None`` for the unset case and the
        # user's integer when overridden.
        return {"dimensions": self._options.get("dimensions")}


class _RuntimeStub:
    """Minimal runtime double.

    The service does not call any methods on the runtime directly —
    it forwards the instance to ``ProviderEmbeddingClient.from_runtime``,
    which we patch out in the tests below. The attributes here are
    only what ``from_runtime`` would read; the stub keeps the type
    surface minimal because the production code path is mocked.
    """

    providers: Any = None
    provider_credentials: Any = None


class _FakeProviderClient:
    """Stand-in for ``ProviderEmbeddingClient``.

    Records every ``embed`` call and returns the configured vectors
    (or raises the configured exception). The service uses this in
    place of the real client so the test does not need respx mocks
    and can exercise the error mapping paths directly.
    """

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        embed_exception: Exception | None = None,
    ) -> None:
        self._vectors = list(vectors or [])
        self._embed_exception = embed_exception
        self.embed_calls: list[tuple[list[str], dict[str, Any]]] = []

    async def embed(self, inputs: list[str], options: dict[str, Any]) -> list[list[float]]:
        self.embed_calls.append((list(inputs), dict(options)))
        if self._embed_exception is not None:
            raise self._embed_exception
        return [list(vector) for vector in self._vectors]
