"""Reusable Anthropic Messages-compatible provider adapter.

Owns the ``/messages`` wire mechanics: message format, configurable
authentication/version headers, streaming, and error classification.

Key differences from the OpenAI-compatible adapter:

- Endpoint: ``/messages`` (not ``/chat/completions``)
- System messages are extracted into a top-level ``system`` field
- Auth and version headers are supplied by the concrete Provider
- Content blocks instead of flat ``content`` strings
- Thinking/reasoning via ``thinking`` and ``output_config`` parameters
- Streaming uses ``event:`` + ``data:`` SSE lines (not ``data:`` only)
- Stream ends on ``message_stop`` event (not ``[DONE]``)
- Messages-compatible structured errors and configurable retry statuses"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    execute_with_sampling_fallback,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._messages_constants import (
    ANTHROPIC_EFFORT_FLOOR,
    ANTHROPIC_ERROR_STOP_REASONS,
    ANTHROPIC_METADATA_KEY,
    ANTHROPIC_MINIMAL_EFFORT,
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_REASONING_PARAMETER_NAMES,
    ANTHROPIC_SAMPLING_PARAMETER_NAMES,
    ANTHROPIC_STOP_REASONS,
    ANTHROPIC_TOOL_STOP_REASONS,
    ANTHROPIC_VERSION,
    CACHE_BREAKPOINT_LIMIT,
    CACHE_CONTROL_EPHEMERAL,
    CACHE_UNMARKABLE_BLOCK_TYPES,
    MAX_HISTORY_CACHE_BREAKPOINTS,
    MESSAGES_ENDPOINT,
    REASONING_META_CONTENT_BLOCKS,
    REDACTED_THINKING_BLOCK_TYPE,
    REQUIRES_ADAPTIVE_THINKING_METADATA_KEY,
    TEXT_BLOCK_TYPE,
    THINKING_BLOCK_TYPE,
    TOOL_USE_BLOCK_TYPE,
)
from core.providers._messages_stream import (
    AnthropicMessagesStreamDecoder,
)
from core.providers._messages_wire import (
    _apply_anthropic_tools,
    _apply_prompt_caching,
    _extract_anthropic_reasoning,
    _extract_anthropic_reasoning_meta,
    _extract_anthropic_text,
    _extract_anthropic_tool_calls,
    _extract_anthropic_usage,
    _merge_anthropic_system_parts,
    _to_anthropic_messages,
    apply_anthropic_cache_usage,
    apply_anthropic_reasoning_usage,
)
from core.providers.adapter import (
    ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE,
    IMAGE_WIRE_MEDIA_TYPES,
    ModelLookup,
    ProviderAdapter,
    normalize_tool_call_ids,
    resolve_request_input_budget,
)
from core.providers.errors import NetworkError
from core.providers.providers import (
    AuthConfig,
    ProviderConfig,
    resolve_context_window,
    resolve_request_output_limit,
)
from core.providers.reasoning import (
    BUDGET_FLOOR_TOKENS,
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    REASONING_REPLAY_FIDELITY_META_ONLY,
    ReasoningIntent,
    ReasoningReplayFidelity,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    remove_reasoning_kwargs,
    resolve_reasoning_intent,
)
from core.providers.token_getter import OAuthRequestRecovery, StaticTokenGetter, TokenGetter
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.tokens import estimate_structured_tokens

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "ANTHROPIC_EFFORT_FLOOR",
    "ANTHROPIC_ERROR_STOP_REASONS",
    "ANTHROPIC_METADATA_KEY",
    "ANTHROPIC_MINIMAL_EFFORT",
    "ANTHROPIC_OVERLOADED_STATUS",
    "ANTHROPIC_REASONING_PARAMETER_NAMES",
    "ANTHROPIC_SAMPLING_PARAMETER_NAMES",
    "ANTHROPIC_STOP_REASONS",
    "ANTHROPIC_TOOL_STOP_REASONS",
    "ANTHROPIC_VERSION",
    "AnthropicCompatibleAdapter",
    "AnthropicMessagesStreamDecoder",
    "CACHE_BREAKPOINT_LIMIT",
    "CACHE_CONTROL_EPHEMERAL",
    "CACHE_UNMARKABLE_BLOCK_TYPES",
    "MAX_HISTORY_CACHE_BREAKPOINTS",
    "MESSAGES_ENDPOINT",
    "REASONING_META_CONTENT_BLOCKS",
    "REDACTED_THINKING_BLOCK_TYPE",
    "REQUIRES_ADAPTIVE_THINKING_METADATA_KEY",
    "TEXT_BLOCK_TYPE",
    "THINKING_BLOCK_TYPE",
    "TOOL_USE_BLOCK_TYPE",
    "apply_anthropic_cache_usage",
    "apply_anthropic_reasoning_usage",
]

_LOGGER = get_logger("providers.anthropic_compatible")


class AnthropicCompatibleAdapter(ProviderAdapter):
    """Adapter for Anthropic Messages-compatible APIs.

    Uses the ``/messages`` endpoint with Anthropic's own request and response
    format.  Provider-specific differences (base URL, auth header, extra
    headers, default parameters) come from ``ProviderConfig``.

    Args:
        config: Immutable provider configuration.
        token_getter: Async callable that returns the current auth token.
    """

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_version: str | None = None,
        wire_media_types: frozenset[str] = IMAGE_WIRE_MEDIA_TYPES,
        prompt_caching: bool = False,
        extra_retryable_statuses: frozenset[int] = frozenset(),
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        self._api_version = api_version
        self._wire_media_types = wire_media_types
        self._prompt_caching = prompt_caching
        self._extra_retryable_statuses: set[int] = set(extra_retryable_statuses)
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site but is not used by the Messages wire.
        del connection_mode
        super().__init__(
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            reasoning_replay_default=config.reasoning_replay,
        )
        self._owns_client = client is None
        self._client = client or build_async_client(
            base_url=base_url or config.base_url,
            debug_recorder=debug_recorder,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AnthropicCompatibleAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Wire media capability
    # ------------------------------------------------------------------

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Return media types verified for this compatible endpoint profile."""
        del model_id
        return self._wire_media_types

    def reasoning_replay_fidelity(self, model_id: str) -> ReasoningReplayFidelity:
        """The Messages wire round-trips signed thinking blocks only.

        Assistant serialization goes through ``_to_anthropic_assistant_content``,
        which replays ``reasoning_meta.content_blocks`` verbatim and never emits
        a readable text field; this declaration documents that native shape.
        """
        del model_id
        return REASONING_REPLAY_FIDELITY_META_ONLY

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    async def _build_headers(self) -> dict[str, str]:
        """Build configured auth, optional version, and extra headers."""
        token = await self._token_getter()
        headers: dict[str, str] = {
            self._auth_config.header: f"{self._auth_config.prefix}{token}",
        }
        if self._api_version is not None:
            headers["anthropic-version"] = self._api_version
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize a Messages response to canonical assistant fields.

        ``model_id`` is accepted for interface parity with the data-driven
        reasoning-response-field path (Phase 5) but unused — Anthropic's wire
        reasoning shape is fixed (``thinking`` blocks).
        """
        del model_id
        content_blocks = response.get("content", [])
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": _extract_anthropic_text(content_blocks),
            "reasoning": _extract_anthropic_reasoning(content_blocks),
            "reasoning_meta": _extract_anthropic_reasoning_meta(content_blocks),
            "tool_calls": _extract_anthropic_tool_calls(content_blocks),
        }
        normalized["terminal_outcome"] = AnthropicMessagesStreamDecoder._normalize_stop_reason(
            response.get("stop_reason"),
            has_tool_calls=bool(normalized["tool_calls"]),
        )
        usage = _extract_anthropic_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the Messages wire without counting duplicate readable thinking."""
        wire = normalize_tool_call_ids(
            [dict(message) for message in messages], ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE
        )
        system = _merge_anthropic_system_parts(
            [
                message["content"]
                for message in wire
                if message.get("role") == "system"
                and isinstance(message.get("content"), (str, list))
            ]
        )
        payload: dict[str, Any] = {
            "messages": _to_anthropic_messages(
                [message for message in wire if message.get("role") != "system"],
                include_thinking_blocks=self._model_reasoning_supported(model_id) is not False,
            )
        }
        if system is not None:
            payload["system"] = system
        if tools:
            _apply_anthropic_tools(payload, {"tools": list(tools)})
        # Separate the growing history from stable System Prompt/Tools so the
        # shared per-item count cache also benefits the Messages wire.
        history_tokens = estimate_structured_tokens(payload.pop("messages"), model_id=model_id)[0]
        return history_tokens + estimate_structured_tokens(payload, model_id=model_id)[0]

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the Anthropic Messages-compatible request payload.

        Extracts system-role messages into the ``system`` field (required by
        the Anthropic API — system messages must not appear in the messages
        array) and assembles model, messages, defaults, and overrides.
        """
        # ``None``-valued caller kwargs mean "not specified" — drop them so they
        # do not clobber provider defaults below. Falsy-but-non-None values
        # (e.g. ``temperature=0.0``) must survive.
        wire_messages = normalize_tool_call_ids(
            messages,
            ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE,
        )
        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        system_parts: list[str | list[dict[str, Any]]] = []
        conversation_messages: list[dict[str, Any]] = []

        for message in wire_messages:
            role = message.get("role")
            if role == "system":
                # Anthropic requires system messages in a separate top-level
                # field, not in the messages array.
                content = message.get("content")
                if isinstance(content, (str, list)):
                    system_parts.append(content)
            else:
                conversation_messages.append(message)

        payload: dict[str, Any] = {"model": model_id}
        system_content = _merge_anthropic_system_parts(system_parts)
        if system_content is not None:
            payload["system"] = system_content
        _apply_anthropic_tools(
            payload,
            request_kwargs,
        )
        reasoning_supported = self._model_reasoning_supported(model_id)
        # Resolve the output allowance once: it both bounds any thinking budget
        # and is the ``max_tokens`` that goes on the wire (set after overrides
        # below so it wins over the provider-default fallback).
        resolved_max_tokens = self._resolve_max_tokens(
            request_kwargs,
            model_id,
            wire_messages,
            tools=kwargs.get("tools"),
        )
        self._apply_reasoning(
            payload,
            request_kwargs,
            model_id,
            reasoning_supported=reasoning_supported,
            max_tokens=resolved_max_tokens,
        )

        # Sampling parameters must never reach the wire in two cases: when
        # thinking is active (Anthropic rejects a sampling temperature alongside
        # thinking), or when the model is from the adaptive-only generation
        # (Opus 4.7+, Fable 5) that removed sampling entirely. Both drop the
        # caller value and skip the provider default below.
        supports_sampling = self._model_supports_temperature(model_id)
        thinking_active = _anthropic_thinking_active(payload, request_kwargs)
        drop_sampling = thinking_active or not supports_sampling
        if drop_sampling:
            for sampling_key in ANTHROPIC_SAMPLING_PARAMETER_NAMES:
                request_kwargs.pop(sampling_key, None)

        # Replayed thinking blocks must not be sent when the outgoing request
        # explicitly disables thinking or the model cannot reason; with the
        # thinking parameter merely absent they are kept (Anthropic guidance:
        # omitting blocks is the risk, the server drops unusable ones).
        payload["messages"] = _to_anthropic_messages(
            conversation_messages,
            include_thinking_blocks=not _anthropic_thinking_disabled(
                payload,
                request_kwargs,
                reasoning_supported=reasoning_supported,
            ),
        )

        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                if drop_sampling and key in ANTHROPIC_SAMPLING_PARAMETER_NAMES:
                    continue
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        # Pin the resolved output allowance last so the model's own ceiling wins
        # over the provider-default fallback (Anthropic requires a positive
        # ``max_tokens`` and rejects one above the model's output ceiling).
        if resolved_max_tokens is not None:
            payload["max_tokens"] = resolved_max_tokens
        # Cache stable prefixes last, after every other payload mutation, so the
        # markers land on the final system/messages that go on the wire.
        if self._prompt_caching:
            _apply_prompt_caching(payload)
        return payload

    def _model_reasoning_supported(self, model_id: str) -> bool | None:
        return model_reasoning_supported(self._model_lookup, model_id)

    def _model_supports_temperature(self, model_id: str) -> bool:
        """Compatible endpoints accept sampling unless a concrete provider says otherwise."""

        del model_id
        return True

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        """Classify one response status, allowing concrete gateways to refine it."""

        classify_http_status(
            status_code,
            idempotent=False,
            extra_retryable=self._extra_retryable_statuses,
            detail=detail,
            response_headers=response_headers,
        )

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
        *,
        reasoning_supported: bool | None,
        max_tokens: int | None,
    ) -> None:
        """Resolve the shared reasoning intent and render it onto the payload.

        A catalog-known non-reasoning model strips every Anthropic thinking
        control and sends nothing. Otherwise the provider-neutral intent
        (:func:`resolve_reasoning_intent`) is rendered into Anthropic's
        ``thinking``/``output_config`` shape — including native ``budget_tokens``
        for a ``budget`` Claude. ``max_tokens`` (the resolved output allowance)
        bounds any thinking budget so it stays strictly under the output cap.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        if reasoning_supported is False:
            remove_reasoning_kwargs(request_kwargs, *ANTHROPIC_REASONING_PARAMETER_NAMES)
            return
        intent = resolve_reasoning_intent(
            supported=reasoning_supported,
            control=model_reasoning_control(self._model_lookup, model_id),
            levels=model_reasoning_levels(self._model_lookup, model_id) or ANTHROPIC_EFFORT_FLOOR,
            effort=thinking_effort,
            budget_max=model_reasoning_budget_max(self._model_lookup, model_id),
            max_tokens=max_tokens,
        )
        self._render_reasoning(payload, intent, model_id=model_id, max_tokens=max_tokens)

    def _render_reasoning(
        self,
        payload: dict[str, Any],
        intent: ReasoningIntent,
        *,
        model_id: str,
        max_tokens: int | None,
    ) -> None:
        """Render a reasoning intent onto Anthropic's ``thinking`` shape.

        * ``effort`` → adaptive thinking (summarized) plus ``output_config.effort``
          for efforts above ``minimal``.
        * ``budget`` → native ``thinking: {type: enabled, budget_tokens}``.
        * ``on`` → enabled with a floor budget; skipped with a warning when even
          the floor cannot fit under ``max_tokens`` (D3).
        * ``off`` → ``thinking: {type: disabled}``.
        * ``default`` → leave the provider default untouched (omit ``thinking``).
        """

        if intent.kind == REASONING_INTENT_EFFORT:
            payload["thinking"] = {"type": "adaptive", "display": "summarized"}
            if intent.effort_level != ANTHROPIC_MINIMAL_EFFORT:
                payload["output_config"] = {"effort": intent.effort_level}
        elif intent.kind == REASONING_INTENT_BUDGET:
            payload["thinking"] = {"type": "enabled", "budget_tokens": intent.budget_tokens}
        elif intent.kind == REASONING_INTENT_ON:
            budget = _anthropic_floor_budget(max_tokens)
            if budget is None:
                _LOGGER.warning(
                    "Skipping reasoning for %s: floor budget (%d) does not fit max_tokens (%s)",
                    model_id,
                    BUDGET_FLOOR_TOKENS,
                    max_tokens,
                )
                return
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif intent.kind == REASONING_INTENT_OFF:
            if self._model_requires_adaptive_thinking(model_id):
                return
            payload["thinking"] = {"type": "disabled"}

    def _model_requires_adaptive_thinking(self, model_id: str) -> bool:
        if self._model_lookup is None:
            return False
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return False
        metadata = model.metadata.get(ANTHROPIC_METADATA_KEY)
        return (
            isinstance(metadata, Mapping)
            and metadata.get(REQUIRES_ADAPTIVE_THINKING_METADATA_KEY) is True
        )

    def _resolve_max_tokens(
        self,
        request_kwargs: dict[str, Any],
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
    ) -> int | None:
        """Return the output ``max_tokens`` for the request.

        Anthropic requires a positive ``max_tokens`` and rejects one above the
        model's output ceiling. Precedence:

        1. An explicit positive caller value (a non-positive one is ignored — it
           would 400 at the wire, so it is treated as unspecified).
        2. The model's catalog output ceiling — the default, so each model uses
           its own full allowance instead of a flat provider cap. This is what
           keeps a thinking budget from starving the answer: a shared 8K-style
           cap would let a mid/high effort budget consume the whole allowance.
        3. The provider-config ``max_tokens`` default, as the fallback only when
           the ceiling is unknown (no lookup / offline refresh).
        """

        explicit = request_kwargs.get("max_tokens")
        if not _is_positive_int(explicit):
            request_kwargs.pop("max_tokens", None)
            explicit = None
        tool_definitions = tools if isinstance(tools, list) else None
        estimated_input = self.estimate_request_input_tokens(
            messages, model_id=model_id, tools=tool_definitions
        )
        estimated_input = resolve_request_input_budget(model_id, estimated_input)
        default = self._config.defaults.get("max_tokens") if self._config.defaults else None
        return resolve_request_output_limit(
            explicit_limit=explicit,
            model_output_limit=self._model_max_output_tokens(model_id),
            provider_default=default,
            effective_context_window=resolve_context_window(
                self._model_context_window(model_id), self._config
            ),
            estimated_input_tokens=estimated_input,
        )

    def _model_max_output_tokens(self, model_id: str) -> int | None:
        """The model's catalog output ceiling, or ``None`` when it is unknown.

        Mirrors :meth:`_model_supports_temperature`'s lookup: it strips a
        ``::variant`` suffix and tolerates a missing lookup or model.
        """

        if self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        return model.max_output_tokens

    def _model_context_window(self, model_id: str) -> int | None:
        """The Model's catalog context window, or ``None`` when unknown."""

        if self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        context_window = model.context_window
        if _is_positive_int(context_window):
            return context_window
        return None

    # ------------------------------------------------------------------
    # Error detail helper (Anthropic-specific)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        """Build an error detail string from an Anthropic error response.

        Parses the Anthropic error response format for richer error messages.
        The Anthropic API returns errors as::

            {"type": "error", "error": {"type": "...", "message": "..."}}

        Args:
            status_code: HTTP response status code.
            response_body: Response body text for context.

        Returns:
            A human-readable detail string combining the status code with
            any structured error information available.
        """
        detail = str(status_code)
        try:
            error_data = json.loads(response_body) if response_body else {}
            error_info = error_data.get("error", {})
            error_type = error_info.get("type", "")
            error_message = error_info.get("message", "")
            if error_type and error_message:
                detail = f"{status_code} ({error_type}): {error_message}"
            elif error_message:
                detail = f"{status_code}: {error_message}"
        except (json.JSONDecodeError, AttributeError):
            if response_body:
                detail = f"{status_code}: {response_body}"
        return detail

    # ------------------------------------------------------------------
    # send() — non-streaming
    # ------------------------------------------------------------------

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a non-streaming request to the Anthropic Messages API.

        Retries on shared transient statuses plus concrete Provider additions
        via ``retry_async``. Fails immediately on auth errors (401/403).

        Args:
            messages: Conversation messages.  System-role messages are
                automatically extracted into the ``system`` field.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (thinking, output_config, …).

        Returns:
            Parsed response dict from the provider.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            NetworkError: Connection errors (retried, then raised).
            ProviderTimeoutError: Timeout errors (retried, then raised).
            ProviderError: Other HTTP errors.
        """

        request_headers = self._request_headers_from_kwargs(kwargs)
        # Built before the retry loop so the sampling fallback below can strip a
        # rejected parameter from the exact payload — provider ``defaults`` would
        # otherwise refill the key on a rebuild.
        payload = self._build_payload(messages, model_id, **kwargs)

        auth_recovery = OAuthRequestRecovery(self._token_getter, self._auth_config)

        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            headers.update(request_headers)
            try:
                response = await self._client.post(
                    MESSAGES_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            auth_recovery.record_response(
                response.status_code, headers, response.text if response.status_code >= 400 else ""
            )
            detail = self._build_error_detail(response.status_code, response.text)
            self._classify_http_status(
                response.status_code,
                detail=detail,
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, f"{self._config.name} provider"))

        return await execute_with_sampling_fallback(
            lambda: auth_recovery.run(lambda: retry_async(_do_request)),
            payload,
            logger=_LOGGER,
            provider_label=self._config.id,
        )

    # ------------------------------------------------------------------
    # stream() — SSE streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming request to the Anthropic Messages API and yield
        normalized provider-agnostic deltas.

        Anthropic uses ``event:`` and ``data:`` lines in its SSE stream.
        The stream ends on a ``message_stop`` event.  Provider-specific
        stream events are translated into ``content_delta``,
        ``reasoning_delta``, ``tool_call_delta``, ``reasoning_meta``, and
        ``finish`` dictionaries before being yielded.

        Retries the initial connection on shared transient statuses plus
        concrete Provider additions. Once established, yields parsed SSE data
        chunks as dicts until ``message_stop`` is received.

        Args:
            messages: Conversation messages.  System-role messages are
                automatically extracted into the ``system`` field.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (thinking, output_config, …).

        Yields:
            Normalized delta dicts from the SSE event stream.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            NetworkError: Connection and mid-stream read errors.
            ProviderTimeoutError: Timeout errors (initial connection retried;
                mid-stream timeouts raised).
            ProviderError: Other HTTP errors and in-band stream/provider
                error payloads.
        """
        request_headers = self._request_headers_from_kwargs(kwargs)
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True
        auth_recovery = OAuthRequestRecovery(self._token_getter, self._auth_config)

        async def _build_headers() -> dict[str, str]:
            headers = await self._build_headers()
            headers.update(request_headers)
            return headers

        def _handle_error_status(
            status_code: int,
            error_body: str,
            response_headers: httpx.Headers,
        ) -> None:
            self._classify_http_status(
                status_code,
                detail=self._build_error_detail(status_code, error_body),
                response_headers=response_headers,
            )

        response = await execute_with_sampling_fallback(
            lambda: connect_streaming_with_retry(
                self._client,
                MESSAGES_ENDPOINT,
                payload,
                build_headers=_build_headers,
                handle_error_status=_handle_error_status,
                auth_recovery=auth_recovery,
            ),
            payload,
            logger=_LOGGER,
            provider_label=self._config.id,
        )

        stream_decoder = AnthropicMessagesStreamDecoder()
        seen_message_stop = False

        try:
            async for data in iter_sse_data(response):
                if not data.strip():
                    continue
                parsed = parse_sse_json_data(data, context=f"{self._config.name} provider")
                if not isinstance(parsed, dict):
                    continue
                for normalized_delta in stream_decoder.normalize(parsed):
                    yield normalized_delta
                if parsed.get("type") == "message_stop":
                    seen_message_stop = True
                    break
            if not seen_message_stop:
                raise NetworkError("Stream ended without message_stop event")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()


def _is_positive_int(value: Any) -> bool:
    """True for a real positive integer (``bool`` and non-integers excluded)."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _anthropic_floor_budget(max_tokens: int | None) -> int | None:
    """Return the floor thinking budget, or ``None`` when it cannot fit ``max_tokens``.

    Anthropic counts ``budget_tokens`` against the output allowance, so the floor
    budget must stay strictly under a positive ``max_tokens``; when it cannot, no
    valid budget can be sent (D3 skip).
    """

    if max_tokens is not None and max_tokens <= BUDGET_FLOOR_TOKENS:
        return None
    return BUDGET_FLOOR_TOKENS


def _anthropic_thinking_active(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
) -> bool:
    """Return True when the outgoing request activates thinking.

    A raw ``thinking`` caller kwarg wins over the value derived from
    ``thinking_effort`` because ``request_kwargs`` is applied onto the
    payload last.
    """
    thinking = request_kwargs.get("thinking", payload.get("thinking"))
    return isinstance(thinking, dict) and thinking.get("type") in {"adaptive", "enabled"}


def _anthropic_thinking_disabled(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    *,
    reasoning_supported: bool | None,
) -> bool:
    """Return True when the outgoing request explicitly rules out thinking.

    Only an explicit ``thinking: {type: disabled}`` or a catalog-known
    non-reasoning model counts — an absent thinking parameter does not.
    """
    if reasoning_supported is False:
        return True
    thinking = request_kwargs.get("thinking", payload.get("thinking"))
    return isinstance(thinking, dict) and thinking.get("type") == "disabled"
