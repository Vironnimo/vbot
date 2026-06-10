"""ProviderAdapter abstract base class.

Defines the interface that all provider adapters must implement.
Adapters translate between vBot's request format and the provider's
wire protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.debug import DebugContext, ProviderDebugRecorder

from core.models.models import Model

JsonObject = dict[str, Any]
ModelLookup = Callable[[str], "Model | None"]


class ProviderAdapter(ABC):
    """Abstract base class for provider adapters.

    Every adapter must implement ``send()`` for non-streaming requests
    and ``stream()`` for streaming (SSE) requests.  The exact request
    and response types are intentionally kept as plain dicts so that
    the adapter layer can stabilise independently of the chat-layer
    data types introduced in Phase 2.

    ``stream()`` yields normalized, provider-agnostic delta dicts rather
    than raw provider SSE chunks.  Supported delta shapes are:

    - ``{"type": "content_delta", "text": " token"}``
    - ``{"type": "reasoning_delta", "text": " thinking"}``
    - ``{"type": "tool_call_delta", "id": "...", "name_delta": "...", "arguments_delta": "..."}``
    - ``{"type": "reasoning_meta", "reasoning_meta": {...}}``
    - ``{"type": "usage", "input_tokens": 1, "output_tokens": 1}``
      (optional ``cache_read_tokens`` / ``cache_write_tokens`` ints when the
      provider reports prompt-cache usage; ``input_tokens`` is always the
      total prompt including cached tokens)
    - ``{"type": "finish", "reason": "stop" | "tool_calls"}``

    ``reasoning_meta`` is internal to the adapter/chat boundary and must
    remain opaque to callers outside the chat core.

    **Debug hooks:**

    When debug mode is enabled, the runtime passes a
    ``ProviderDebugRecorder`` into the adapter constructor; the adapter
    builds its HTTP client through ``_http_shared.build_async_client``,
    which wires all wire-capture into a single transport. Adapters carry
    no capture logic of their own. The chat loop calls
    ``set_debug_context()`` before each ``send()`` / ``stream()`` call;
    the base implementation forwards the context to the recorder, which
    the capture transport reads per request.
    """

    # Class-level default so the optional debug hook resolves even on
    # subclasses (and test doubles) that do not call ``super().__init__()``.
    _debug_recorder: ProviderDebugRecorder | None = None

    def __init__(
        self,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
    ) -> None:
        """Store the model lookup contract and optional debug recorder."""
        self._model_lookup = model_lookup
        self._debug_recorder = debug_recorder

    # ------------------------------------------------------------------
    # Debug hooks
    # ------------------------------------------------------------------

    def set_debug_context(self, ctx: DebugContext) -> None:
        """Forward the per-request debug context to the recorder.

        Called by the chat loop before each ``send()`` or ``stream()``
        call. The context is **never** part of ``**kwargs`` and must not
        leak into provider payloads. No-op when debug mode is off.

        Args:
            ctx: Immutable debug context with run / agent / session /
                provider / model identifiers and iteration number.
        """
        if self._debug_recorder is not None:
            self._debug_recorder.set_context(ctx)

    @abstractmethod
    async def aclose(self) -> None:
        """Close the HTTP client and release resources.

        Subclasses that hold an ``httpx.AsyncClient`` should await
        its ``aclose()`` method.  Callers should use the async context
        manager interface (``async with``) or call this explicitly
        when the adapter is no longer needed.
        """

    @abstractmethod
    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict:
        """Send a non-streaming chat request.

        Args:
            messages: Conversation messages in provider wire format.
            model_id: Exact model identifier sent to the provider API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Returns:
            Parsed response dict from the provider.
        """

    @abstractmethod
    def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]:
        """Send a streaming chat request.

        Args:
            messages: Conversation messages in provider wire format.
            model_id: Exact model identifier sent to the provider API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Yields:
            Normalized provider-agnostic streaming delta dicts.  Adapters
            must hide raw SSE event formats and provider-specific chunk
            structure from callers.
        """

    def normalize_response(self, response: JsonObject) -> JsonObject:
        """Normalize a provider response into canonical assistant-message fields.

        Concrete adapters own provider-specific response parsing.  The default
        raises so subclasses can add this capability without making the legacy
        ABC constructor contract stricter during Phase 2.
        """
        raise NotImplementedError("normalize_response must be implemented by provider adapters")
