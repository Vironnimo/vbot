"""ProviderAdapter abstract base class.

Defines the interface that all provider adapters must implement.
Adapters translate between vBot's request format and the provider's
wire protocol.
"""

from __future__ import annotations

import copy
import hashlib
import json
import string
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

if TYPE_CHECKING:
    from core.debug import DebugContext, ProviderDebugRecorder

from core.models.models import Model
from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN, ReasoningReplayPolicy

JsonObject = dict[str, Any]
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

# Concrete image media types every current chat wire can carry as native input.
# Lives in the providers domain (the wire-protocol layer), not the chat layer:
# it is the common building block adapters compose into their wire-media set.
IMAGE_WIRE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

_ALPHANUMERIC_TOOL_CALL_ID_CHARACTERS = frozenset(string.ascii_letters + string.digits)
_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_-")
_TOOL_CALL_ID_HASH_LENGTH = 12
_RESPONSES_OUTPUT_META_KEY = "response_output"
_TOOL_RESULT_ENVELOPE_KEYS = frozenset({"ok", "error", "data", "artifacts"})
TOOL_RESULT_CONTENT_BLOCKS_FIELD = "tool_result_content"


def canonical_tool_result_is_error(message: Mapping[str, Any]) -> bool:
    """Return whether a canonical Tool message carries a failure envelope.

    Provider Adapters use this request-only projection to add native error
    signals without changing the serialized Tool Result content or teaching
    Chat about Provider wire fields. Legacy/non-envelope Tool content remains
    ordinary content and therefore receives no native failure flag.
    """

    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(result, Mapping)
        and frozenset(result) == _TOOL_RESULT_ENVELOPE_KEYS
        and result.get("ok") is False
        and result.get("data") is None
        and isinstance(result.get("error"), Mapping)
        and isinstance(result.get("artifacts"), list)
    )


def tool_result_content_blocks(message: Mapping[str, Any]) -> list[JsonObject]:
    """Return the Run-local rich content attached to one Tool Result.

    Persisted Tool messages keep their stable JSON-string envelope. Chat adds
    this request-only field after resolving attachment references, so Provider
    adapters can render native multimodal Tool Results without putting base64
    data into Session history.
    """

    value = message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD)
    if not isinstance(value, list):
        return []
    return [dict(block) for block in value if isinstance(block, Mapping)]


def project_tool_result_content_fallbacks(
    messages: list[JsonObject],
) -> list[JsonObject]:
    """Project rich Tool Results onto text-only Tool wires.

    Supplemental text remains part of its correlated Tool Result. Media blocks
    are emitted in one request-only user message after the complete consecutive
    Tool Result batch, preserving Provider tool-cycle ordering without writing
    a synthetic user message to the canonical Session.
    """

    projected: list[JsonObject] = []
    pending_media: list[JsonObject] = []

    def flush_media() -> None:
        if pending_media:
            projected.append({"role": "user", "content": list(pending_media)})
            pending_media.clear()

    for message in messages:
        if message.get("role") != "tool":
            flush_media()
            projected.append(dict(message))
            continue

        projected_message = dict(message)
        projected_message.pop(TOOL_RESULT_CONTENT_BLOCKS_FIELD, None)
        supplemental_text: list[str] = []
        for block in tool_result_content_blocks(message):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    supplemental_text.append(text)
            elif block_type in {"media", "document"}:
                pending_media.append(block)
        if supplemental_text:
            content = projected_message.get("content")
            base_text = content if isinstance(content, str) else ""
            projected_message["content"] = "\n\n".join(
                part for part in (base_text, *supplemental_text) if part
            )
        projected.append(projected_message)

    flush_media()
    return projected


@dataclass(frozen=True)
class ToolCallIdProfile:
    """Verified target-wire constraints for request Tool-call identifiers."""

    name: str
    allowed_characters: frozenset[str]
    max_length: int
    exact_length: int | None = None
    trim_trailing_underscores: bool = False
    rewrite_responses_output_items: bool = False


ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="anthropic_messages",
    allowed_characters=_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS,
    max_length=64,
)
MISTRAL_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="mistral",
    allowed_characters=_ALPHANUMERIC_TOOL_CALL_ID_CHARACTERS,
    max_length=9,
    exact_length=9,
)
RESPONSES_TOOL_CALL_ID_PROFILE = ToolCallIdProfile(
    name="responses",
    allowed_characters=_DASH_UNDERSCORE_TOOL_CALL_ID_CHARACTERS,
    max_length=64,
    trim_trailing_underscores=True,
    rewrite_responses_output_items=True,
)


def normalize_tool_call_ids(
    messages: list[JsonObject],
    profile: ToolCallIdProfile,
) -> list[JsonObject]:
    """Return a target-wire copy with paired Tool call/result IDs normalized.

    Canonical Session messages are Provider-origin evidence and must stay
    immutable. This transform therefore deep-copies the complete request view,
    allocates collision-free IDs across the outgoing request, and scopes result
    correlation to the immediately preceding Assistant Tool batch. Reusing one
    original ID in a later batch creates a fresh wire ID rather than aliasing
    two logical calls.
    """

    request_messages = copy.deepcopy(messages)
    reserved_valid_ids = _reserved_valid_tool_call_ids(request_messages, profile)
    used_wire_ids: set[str] = set()
    active_result_ids: dict[str, list[str]] = {}

    for message_index, message in enumerate(request_messages):
        role = message.get("role")
        if role == "assistant":
            active_result_ids = _normalize_assistant_tool_call_ids(
                message,
                profile,
                used_wire_ids=used_wire_ids,
                reserved_valid_ids=reserved_valid_ids,
                message_index=message_index,
            )
            continue
        if role == "tool":
            original_id = message.get("tool_call_id")
            if isinstance(original_id, str):
                mapped_ids = active_result_ids.get(original_id)
                if mapped_ids:
                    message["tool_call_id"] = mapped_ids.pop(0)
            continue
        active_result_ids = {}

    return request_messages


def _reserved_valid_tool_call_ids(
    messages: list[JsonObject],
    profile: ToolCallIdProfile,
) -> set[str]:
    reserved: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and _tool_call_id_is_valid(tool_call_id, profile):
                reserved.add(tool_call_id)
    return reserved


def _normalize_assistant_tool_call_ids(
    message: JsonObject,
    profile: ToolCallIdProfile,
    *,
    used_wire_ids: set[str],
    reserved_valid_ids: set[str],
    message_index: int,
) -> dict[str, list[str]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return {}

    result_ids: dict[str, list[str]] = {}
    response_item_ids: dict[str, list[str]] = {}
    for tool_call_index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue
        original_id = tool_call.get("id")
        if not isinstance(original_id, str) or not original_id:
            continue
        wire_id = _allocate_tool_call_id(
            original_id,
            profile,
            used_wire_ids=used_wire_ids,
            reserved_valid_ids=reserved_valid_ids,
            occurrence=f"{message_index}:{tool_call_index}",
        )
        tool_call["id"] = wire_id
        result_ids.setdefault(original_id, []).append(wire_id)
        response_item_ids.setdefault(original_id, []).append(wire_id)

    if profile.rewrite_responses_output_items:
        _rewrite_responses_output_tool_call_ids(message, response_item_ids)
    return result_ids


def _allocate_tool_call_id(
    original_id: str,
    profile: ToolCallIdProfile,
    *,
    used_wire_ids: set[str],
    reserved_valid_ids: set[str],
    occurrence: str,
) -> str:
    if _tool_call_id_is_valid(original_id, profile) and original_id not in used_wire_ids:
        used_wire_ids.add(original_id)
        return original_id

    attempt = 0
    while True:
        seed = original_id if attempt == 0 else f"{original_id}\0{occurrence}\0{attempt}"
        candidate = _derive_tool_call_id(original_id, seed, profile)
        if candidate not in used_wire_ids and candidate not in reserved_valid_ids:
            used_wire_ids.add(candidate)
            return candidate
        attempt += 1


def _tool_call_id_is_valid(tool_call_id: str, profile: ToolCallIdProfile) -> bool:
    if not tool_call_id or any(
        character not in profile.allowed_characters for character in tool_call_id
    ):
        return False
    if len(tool_call_id) > profile.max_length:
        return False
    if profile.exact_length is not None and len(tool_call_id) != profile.exact_length:
        return False
    return not (profile.trim_trailing_underscores and tool_call_id.endswith("_"))


def _derive_tool_call_id(
    original_id: str,
    seed: str,
    profile: ToolCallIdProfile,
) -> str:
    digest = hashlib.sha256(f"{profile.name}\0{seed}".encode()).hexdigest()
    if profile.exact_length is not None:
        return digest[: profile.exact_length]

    sanitized = "".join(
        character for character in original_id if character in profile.allowed_characters
    )
    if profile.trim_trailing_underscores:
        sanitized = sanitized.rstrip("_")
    digest_part = digest[: min(_TOOL_CALL_ID_HASH_LENGTH, profile.max_length)]
    if len(digest_part) == profile.max_length:
        return digest_part
    separator = "_" if "_" in profile.allowed_characters else ""
    prefix_length = profile.max_length - len(separator) - len(digest_part)
    prefix = sanitized[:prefix_length]
    if profile.trim_trailing_underscores:
        prefix = prefix.rstrip("_")
    if not prefix:
        return digest_part
    return f"{prefix}{separator}{digest_part}"


def _rewrite_responses_output_tool_call_ids(
    message: JsonObject,
    mapped_ids: dict[str, list[str]],
) -> None:
    reasoning_meta = message.get("reasoning_meta")
    if not isinstance(reasoning_meta, dict):
        return
    response_output = reasoning_meta.get(_RESPONSES_OUTPUT_META_KEY)
    if not isinstance(response_output, list):
        return

    remaining_ids = {original_id: list(wire_ids) for original_id, wire_ids in mapped_ids.items()}
    for item in response_output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        original_id = item.get("call_id")
        if not isinstance(original_id, str) or not original_id:
            original_id = item.get("id")
        if not isinstance(original_id, str):
            continue
        wire_ids = remaining_ids.get(original_id)
        if not wire_ids:
            continue
        wire_id = wire_ids.pop(0)
        if wire_id == original_id:
            continue
        item["call_id"] = wire_id
        # A Responses output item id is Provider-owned pairing state, separate
        # from ``call_id``. Replaying it after changing the call identity can
        # falsely pair a foreign function item with opaque reasoning.
        item.pop("id", None)


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
      including cached tokens)
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

    # ------------------------------------------------------------------
    # Per-request conversation context
    # ------------------------------------------------------------------

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> JsonObject:
        """Return extra per-request kwargs derived from the conversation identity.

        The chat layer calls this once per provider request and merges the
        result into the ``send()``/``stream()`` kwargs, letting an adapter turn
        the stable ``(agent_id, session_id)`` pair into a provider-specific
        routing hint (e.g. the OpenAI Codex prompt-cache scope headers) without
        the chat layer knowing any provider specifics.  The ABC default adds
        nothing, so only adapters that override this ever receive the extra
        kwargs — the chat call for every other provider is byte-for-byte
        unchanged, and no wire that would reject an unknown field is touched.
        """
        del agent_id, session_id, project_id
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
