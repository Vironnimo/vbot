"""Tests for the ProviderAdapter abstract base class.

Verifies that the ABC enforces its contract: direct instantiation is
forbidden, and concrete subclasses must implement both ``send()`` and
``stream()``.
"""

from collections.abc import AsyncIterator

import pytest

from core.providers.adapter import ProviderAdapter

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
        yield {"chunk": True, "model": model_id}


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

    def test_default_normalize_response_requires_adapter_implementation(self) -> None:
        """Response normalization is optional for ABC construction but required at use."""
        adapter = _StubAdapter()
        with pytest.raises(NotImplementedError, match="normalize_response"):
            adapter.normalize_response({})
