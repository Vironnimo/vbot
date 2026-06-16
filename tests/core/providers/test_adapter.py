"""Tests for the ProviderAdapter abstract base class.

Verifies that the ABC enforces its contract: direct instantiation is
forbidden, concrete subclasses must implement both ``send()`` and
``stream()``, and streaming adapters expose normalized deltas rather than
raw provider SSE chunks.
"""

from collections.abc import AsyncIterator
from typing import get_type_hints

import pytest

from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ProviderAdapter
from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN

# ---------------------------------------------------------------------------
# Helper: minimal concrete subclass that satisfies the ABC
# ---------------------------------------------------------------------------


class _StubAdapter(ProviderAdapter):
    """Minimal concrete adapter used by contract tests."""

    async def aclose(self) -> None:
        """No-op close for stub adapter."""

    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict:
        return {"model": model_id, "messages": messages}

    async def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]:
        yield {"type": "content_delta", "text": "hello"}
        yield {
            "type": "tool_call_delta",
            "id": f"call-{model_id}",
            "name_delta": "read_file",
            "arguments_delta": '{"path":',
        }
        yield {"type": "reasoning_meta", "reasoning_meta": {"provider": "opaque"}}
        yield {"type": "finish", "reason": "tool_calls"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProviderAdapterABC:
    """Contract tests for the ProviderAdapter ABC."""

    def test_cannot_instantiate_abc_directly(self) -> None:
        """ProviderAdapter raises TypeError when instantiated directly."""
        with pytest.raises(TypeError, match="abstract method"):
            ProviderAdapter()  # type: ignore[abstract]

    def test_subclass_missing_send_raises_type_error(self) -> None:
        """A subclass that doesn't implement send() raises TypeError."""

        class _MissingSend(ProviderAdapter):
            async def stream(
                self, messages: list[dict], *, model_id: str, **kwargs
            ) -> AsyncIterator[dict]:
                yield {}

        with pytest.raises(TypeError, match="abstract method"):
            _MissingSend()  # type: ignore[abstract]

    def test_subclass_missing_stream_raises_type_error(self) -> None:
        """A subclass that doesn't implement stream() raises TypeError."""

        class _MissingStream(ProviderAdapter):
            async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict:
                return {}

        with pytest.raises(TypeError, match="abstract method"):
            _MissingStream()  # type: ignore[abstract]

    def test_subclass_missing_both_raises_type_error(self) -> None:
        """A subclass that implements neither method raises TypeError."""

        class _MissingBoth(ProviderAdapter):
            pass

        with pytest.raises(TypeError, match="abstract method"):
            _MissingBoth()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self) -> None:
        """A subclass implementing both methods can be instantiated."""
        adapter = _StubAdapter()
        assert isinstance(adapter, ProviderAdapter)

    def test_constructor_stores_model_lookup_callable(self) -> None:
        """ProviderAdapter stores an injected model_lookup callable."""

        def lookup(model_id: str):
            _ = model_id
            return None

        adapter = _StubAdapter(model_lookup=lookup)
        assert adapter._model_lookup is lookup

    def test_reasoning_replay_policy_defaults_to_current_run(self) -> None:
        """Unmigrated adapters keep the historical current-run history shaping."""
        adapter = _StubAdapter()

        assert adapter.reasoning_replay_policy("any-model") == REASONING_REPLAY_CURRENT_RUN

    def test_wire_media_support_defaults_to_empty(self) -> None:
        """The ABC carries nothing by default: a forgotten declaration degrades."""
        adapter = _StubAdapter()

        assert adapter.wire_media_support("any-model") == frozenset()

    def test_image_wire_media_types_are_the_common_image_set(self) -> None:
        """The shared image constant covers exactly the allowlisted image types."""
        assert IMAGE_WIRE_MEDIA_TYPES == frozenset(
            {"image/jpeg", "image/png", "image/gif", "image/webp"}
        )

    def test_default_normalize_response_requires_adapter_implementation(self) -> None:
        """Response normalization is optional for ABC construction but required at use."""
        adapter = _StubAdapter()
        with pytest.raises(NotImplementedError, match="normalize_response"):
            adapter.normalize_response({})

    def test_stream_signature_stays_async_iterator_of_dicts(self) -> None:
        """The public stream method shape remains unchanged for adapter callers."""
        hints = get_type_hints(ProviderAdapter.stream)

        assert hints["messages"] == list[dict]
        assert hints["model_id"] is str
        assert hints["return"] == AsyncIterator[dict]

    @pytest.mark.asyncio
    async def test_stream_contract_yields_normalized_deltas(self) -> None:
        """Streaming chunks use normalized delta shapes, not raw provider SSE data."""
        adapter = _StubAdapter()

        chunks = [
            chunk
            async for chunk in adapter.stream([{"role": "user", "content": "hi"}], model_id="model")
        ]

        assert chunks == [
            {"type": "content_delta", "text": "hello"},
            {
                "type": "tool_call_delta",
                "id": "call-model",
                "name_delta": "read_file",
                "arguments_delta": '{"path":',
            },
            {"type": "reasoning_meta", "reasoning_meta": {"provider": "opaque"}},
            {"type": "finish", "reason": "tool_calls"},
        ]
