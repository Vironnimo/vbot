"""ProviderAdapter abstract base class.

Defines the interface that all provider adapters must implement.
Adapters translate between vBot's request format and the provider's
wire protocol.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

JsonObject = dict[str, Any]


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
    - ``{"type": "finish", "reason": "stop" | "tool_calls"}``

    ``reasoning_meta`` is internal to the adapter/chat boundary and must
    remain opaque to callers outside the chat core.
    """

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
