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
from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN, ReasoningReplayPolicy

JsonObject = dict[str, Any]
ModelLookup = Callable[[str], "Model | None"]

# Concrete image media types every current chat wire can carry as native input.
# Lives in the providers domain (the wire-protocol layer), not the chat layer:
# it is the common building block adapters compose into their wire-media set.
IMAGE_WIRE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


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

    # ------------------------------------------------------------------
    # History shaping policy
    # ------------------------------------------------------------------

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Return how persisted assistant reasoning replays for ``model_id``.

        The chat layer queries this once per request build and shapes the
        request history accordingly; adapters must not re-implement
        history-wide reasoning strips on top of it.  ``model_id`` is part of
        the contract because one adapter can route different models to
        different wires.  The default keeps the historical behavior: only the
        active run's assistant turns carry reasoning fields.
        """
        del model_id
        return REASONING_REPLAY_CURRENT_RUN

    # ------------------------------------------------------------------
    # Wire media capability
    # ------------------------------------------------------------------

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Return the concrete media types this adapter's wire carries natively.

        The chat layer intersects this with the model's advertised input
        modalities to decide whether an attachment goes native or is degraded;
        the adapter owns the *format* granularity (e.g. ``"image/png"``,
        ``"audio/wav"``, ``"application/pdf"``) because that is the wire fact.
        ``model_id`` is part of the contract for parity with
        ``reasoning_replay_policy`` and because one adapter can route models to
        different wires; concrete adapters may also branch on their connection
        mode.  The ABC default carries nothing — a forgotten declaration
        degrades the attachment, never crashes the wire.
        """
        del model_id
        return frozenset()

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

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        """Normalize a provider response into canonical assistant-message fields.

        Concrete adapters own provider-specific response parsing.  The default
        raises so subclasses can add this capability without making the legacy
        ABC constructor contract stricter during Phase 2.

        ``model_id`` is optional and keyword-only: the chat layer passes it so an
        adapter can read per-model wire facts (e.g. the data-driven reasoning
        response field) from its ``model_lookup``; callers without it (and the
        compaction summary path) omit it and the adapter falls back to its
        hardcoded default behavior.
        """
        raise NotImplementedError("normalize_response must be implemented by provider adapters")
