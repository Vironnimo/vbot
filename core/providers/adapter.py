"""ProviderAdapter abstract base class.

Defines the interface that all provider adapters must implement.
Adapters translate between vBot's request format and the provider's
wire protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from core.models.models import Model
from core.providers._tool_calls import (
    ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE,
    INVALID_TOOL_CALL_NAME,
    MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS,
    MISTRAL_TOOL_CALL_ID_PROFILE,
    RESPONSES_TOOL_CALL_ID_PROFILE,
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    JsonObject,
    ToolCallIdProfile,
    canonical_tool_result_is_error,
    normalize_tool_call_candidate,
    normalize_tool_call_candidates,
    normalize_tool_call_ids,
    project_tool_result_content_fallbacks,
    tool_result_content_blocks,
)
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_FIDELITY,
    DEFAULT_REASONING_REPLAY_POLICY,
    REASONING_REPLAY_POLICIES,
    ReasoningIntent,
    ReasoningReplayFidelity,
    ReasoningReplayPolicy,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    resolve_reasoning_intent,
)

if TYPE_CHECKING:
    from core.debug import DebugContext, ProviderDebugRecorder
    from core.providers.providers import ConnectionConfig, ProviderConfig

__all__ = [
    "ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE",
    "IMAGE_WIRE_MEDIA_TYPES",
    "INVALID_TOOL_CALL_NAME",
    "JsonObject",
    "MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS",
    "MISTRAL_TOOL_CALL_ID_PROFILE",
    "ModelLookup",
    "ProviderAdapter",
    "RESPONSES_TOOL_CALL_ID_PROFILE",
    "TERMINAL_OUTCOMES",
    "TERMINAL_OUTCOME_CONTENT_FILTERED",
    "TERMINAL_OUTCOME_ERROR",
    "TERMINAL_OUTCOME_OUTPUT_TRUNCATED",
    "TERMINAL_OUTCOME_STOP",
    "TERMINAL_OUTCOME_TOOL_CALLS",
    "TERMINAL_OUTCOME_UNKNOWN",
    "TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD",
    "TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD",
    "TOOL_CALL_REJECTION_FIELD",
    "TOOL_RESULT_CONTENT_BLOCKS_FIELD",
    "TerminalOutcome",
    "ToolCallIdProfile",
    "canonical_tool_result_is_error",
    "estimate_wire_request_input_tokens",
    "normalize_tool_call_candidate",
    "normalize_tool_call_candidates",
    "normalize_tool_call_ids",
    "project_tool_result_content_fallbacks",
    "request_input_budget",
    "resolve_request_input_budget",
    "terminal_outcome_from_response",
    "tool_result_content_blocks",
]

ModelLookup = Callable[[str], "Model | None"]

TerminalOutcome = Literal[
    "stop",
    "tool_calls",
    "output_truncated",
    "content_filtered",
    "error",
    "unknown",
]

TERMINAL_OUTCOME_STOP: Final[TerminalOutcome] = "stop"

TERMINAL_OUTCOME_TOOL_CALLS: Final[TerminalOutcome] = "tool_calls"

TERMINAL_OUTCOME_OUTPUT_TRUNCATED: Final[TerminalOutcome] = "output_truncated"

TERMINAL_OUTCOME_CONTENT_FILTERED: Final[TerminalOutcome] = "content_filtered"

TERMINAL_OUTCOME_ERROR: Final[TerminalOutcome] = "error"

TERMINAL_OUTCOME_UNKNOWN: Final[TerminalOutcome] = "unknown"

TERMINAL_OUTCOMES: Final[frozenset[TerminalOutcome]] = frozenset(
    {
        TERMINAL_OUTCOME_STOP,
        TERMINAL_OUTCOME_TOOL_CALLS,
        TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
        TERMINAL_OUTCOME_CONTENT_FILTERED,
        TERMINAL_OUTCOME_ERROR,
        TERMINAL_OUTCOME_UNKNOWN,
    }
)

IMAGE_WIRE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

_REQUEST_INPUT_BUDGET: ContextVar[tuple[str, int] | None] = ContextVar(
    "provider_request_input_budget", default=None
)


@contextmanager
def request_input_budget(model_id: str, tokens: int) -> Iterator[None]:
    """Scope Chat's complete Context projection to one send, including inner wire routing.

    This local budget never enters request kwargs or a Provider payload. The
    Adapter still adds its independent output-capacity uncertainty reserve.
    """
    token = _REQUEST_INPUT_BUDGET.set((model_id, max(0, tokens)))
    try:
        yield
    finally:
        _REQUEST_INPUT_BUDGET.reset(token)


def resolve_request_input_budget(model_id: str, local_estimate: int) -> int:
    budget = _REQUEST_INPUT_BUDGET.get()
    return budget[1] if budget is not None and budget[0] == model_id else local_estimate


def estimate_wire_request_input_tokens(
    adapter: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Estimate one wire request, retaining the generic fallback for test doubles.

    Production Adapters inherit :meth:`ProviderAdapter.estimate_request_input_tokens`.
    The fallback keeps lightweight duck-typed Adapters used by integration
    seams on the established Chat Completions estimate until they implement the
    richer Adapter contract.
    """

    estimator = getattr(adapter, "estimate_request_input_tokens", None)
    if callable(estimator):
        estimated = estimator(messages, model_id=model_id, tools=tools)
        if isinstance(estimated, int) and not isinstance(estimated, bool) and estimated >= 0:
            return estimated

    from core.utils.tokens import estimate_request_input_tokens

    estimated, _ = estimate_request_input_tokens(messages, tools, model_id=model_id)
    return estimated


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
      or ``{"type": "tool_call_delta", "slot": 0, "id": "...", ...}``, where
      ``id`` may be omitted until a later fragment for index-based wires
    - ``{"type": "heartbeat"}`` (transport liveness only; not Model progress)
    - ``{"type": "reasoning_meta", "reasoning_meta": {...}}``
    - ``{"type": "usage", "input_tokens": 1, "output_tokens": 1}``
      (optional ``cache_read_tokens`` / ``cache_write_tokens`` ints when the
      provider reports prompt-cache usage, plus optional ``reasoning_tokens``
      as a subset of output; ``input_tokens`` is always the total prompt
      including cached tokens; either primary token counter may be omitted when
      the upstream Provider reports only the other one, and Chat estimates only
      the missing field)
    - ``{"type": "finish", "reason": "stop" | "tool_calls" |
      "output_truncated" | "content_filtered" | "error" | "unknown"}``

    ``reasoning_meta`` is internal to the adapter/chat boundary and must
    remain opaque to callers outside the chat core.

    **Debug hooks:**

    When debug mode is enabled, the runtime passes a
    ``ProviderDebugRecorder`` into the adapter constructor; the adapter
    builds its HTTP client through ``_http_shared.build_async_client``,
    which wires HTTP capture into a single transport. A stateful non-HTTP
    transport may feed one canonical exchange directly to the same recorder;
    OpenAI Subscription WebSocket streaming is the sanctioned implementation.
    The chat loop calls
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
        reasoning_replay_default: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY,
    ) -> None:
        """Store model policy lookup, Provider replay default, and debug recorder."""
        self._model_lookup = model_lookup
        self._debug_recorder = debug_recorder
        self._reasoning_replay_default = reasoning_replay_default

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
        history-wide reasoning strips on top of it. The effective precedence is
        Model Override, then Provider Override, then the system
        ``full_history`` default. ``model_id`` is part of the contract because
        one Provider can explicitly narrow an individual older Model without
        reducing every other Model on the same Adapter.
        """
        model_lookup = getattr(self, "_model_lookup", None)
        if model_lookup is not None:
            model = model_lookup(model_id.split("::", 1)[0])
            if model is not None and model.reasoning_replay in REASONING_REPLAY_POLICIES:
                return cast(ReasoningReplayPolicy, model.reasoning_replay)
        return getattr(self, "_reasoning_replay_default", DEFAULT_REASONING_REPLAY_POLICY)

    def reasoning_replay_fidelity(self, model_id: str) -> ReasoningReplayFidelity:
        """Return which class of reasoning state this wire carries back.

        The base serializers consult this once per Assistant message build and
        emit exactly one class: opaque meta when the declaration allows or
        prefers it and the turn captured meta, otherwise the readable text —
        never both. Adapters must not re-implement class filtering on top of
        the declaration. ``model_id`` is part of the contract for parity with
        ``reasoning_replay_policy`` because one adapter can route models to
        different wires. The default ``meta_preferred`` matches OpenRouter's
        documented contract (``reasoning_details`` supersede plaintext) and
        degrades safely for raw-string-only wires.
        """
        del model_id
        return DEFAULT_REASONING_REPLAY_FIDELITY

    # ------------------------------------------------------------------
    # Reasoning render description
    # ------------------------------------------------------------------

    @classmethod
    def describe_reasoning_render(
        cls,
        *,
        model_lookup: ModelLookup | None,
        model_id: str,
        effort: str | None,
        provider_config: ProviderConfig | None = None,
    ) -> ReasoningIntent:
        """Return the provider-neutral intent this wire renders for (model, effort).

        ``/status`` asks this instead of re-deriving the report from the
        declared Model control, so the reported line matches what a request
        with the selected effort would actually carry. The default resolves
        the shared intent against the Model's declared control and ladder —
        the semantics of a wire whose render follows the declaration (binary
        thinking toggles, native token budgets). Wires whose render deviates
        from the declaration override this; the generic OpenAI-compatible wire
        is the main case (it sends the snapped effort level even for an
        ``on_off``-declared Model and has no native budget field).

        ``provider_config`` is only consulted by adapters whose floor ladder
        depends on the Provider identity; the default render ignores it.
        """

        del provider_config
        return resolve_reasoning_intent(
            supported=model_reasoning_supported(model_lookup, model_id),
            control=model_reasoning_control(model_lookup, model_id),
            levels=model_reasoning_levels(model_lookup, model_id) or (),
            effort=effort,
            budget_max=model_reasoning_budget_max(model_lookup, model_id),
            max_tokens=None,
        )

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

    # ------------------------------------------------------------------
    # Request-context estimation
    # ------------------------------------------------------------------

    def request_body_limit(self, model_id: str) -> int | None:
        """Verified maximum serialized request bytes for this Model's wire, if known.

        Concrete wires enforce this before network I/O and raise
        ProviderRequestTooLargeError with the actual byte count. Chat may then
        retire already delivered images and submit a smaller request. Unknown
        limits stay absent; this is independent of tokens and harness image caps.
        """
        del model_id
        return None

    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the context consumed by this Adapter's rendered request.

        Chat uses this same wire-owned estimate for pre-send Context protection
        and automatic Compaction. The default fits Chat Completions-shaped
        requests; Adapters that render a different wire representation override
        it so all local Context decisions budget the request that is actually
        sent rather than the persisted canonical history.
        """

        from core.utils.tokens import estimate_request_input_tokens

        estimated, _ = estimate_request_input_tokens(messages, tools, model_id=model_id)
        return estimated

    # ------------------------------------------------------------------
    # Catalog normalization policy
    # ------------------------------------------------------------------

    @classmethod
    def finalize_discovered_model(
        cls,
        model: Model,
        connection: ConnectionConfig | None,
    ) -> Model:
        """Apply Connection-specific facts after catalog entry normalization.

        Most Providers expose identical Model facts on every Connection and
        therefore return the normalized Model unchanged. Adapters override
        this only when the listing payload is ambiguous without Connection
        context, such as Ollama's direct Cloud catalog omitting the
        ``remote_host`` marker used by a local Ollama proxy.
        """

        del connection
        return model

    # ------------------------------------------------------------------
    # Per-request conversation context
    # ------------------------------------------------------------------

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> JsonObject:
        """Return extra per-request kwargs derived from the conversation identity.

        The chat layer calls this once per provider request and merges the
        result into the ``send()``/``stream()`` kwargs, letting an adapter turn
        Session identity stays available for stateful transport continuation;
        ``prompt_cache_affinity_id`` is a separate, provider-neutral routing
        lineage inherited only by cache-compatible forks. Concrete adapters map
        either identity onto their own wire without the chat layer knowing any
        provider specifics. The ABC default adds nothing, so no unrelated wire
        receives an unknown field.
        """
        del agent_id, session_id, project_id, prompt_cache_affinity_id
        return {}

    def _request_headers_from_kwargs(
        self,
        request_kwargs: JsonObject,
    ) -> dict[str, str]:
        """Extract concrete-Adapter header context before payload serialization.

        The default leaves caller kwargs untouched. A concrete Adapter may pop
        only its own private context keys and return the corresponding wire
        headers. Compatible base Adapters call this once per request, then
        rebuild auth headers and merge these stable values on every retry.
        """

        del request_kwargs
        return {}

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


def terminal_outcome_from_response(response: JsonObject) -> TerminalOutcome:
    """Return one canonical terminal outcome from normalized assistant fields.

    Production adapters must provide ``terminal_outcome``. The missing-field
    inference keeps older third-party/test adapters source-compatible while
    preserving the historical safe distinction between an ordinary answer and
    a Tool turn. An explicit unrecognized value is never inferred from content:
    it fails closed as ``unknown``.
    """

    raw_outcome = response.get("terminal_outcome")
    if raw_outcome is None:
        return TERMINAL_OUTCOME_TOOL_CALLS if response.get("tool_calls") else TERMINAL_OUTCOME_STOP
    if isinstance(raw_outcome, str) and raw_outcome in TERMINAL_OUTCOMES:
        return cast(TerminalOutcome, raw_outcome)
    return TERMINAL_OUTCOME_UNKNOWN
