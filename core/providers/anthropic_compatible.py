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
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, ClassVar, Self, override

import httpx

from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    enforce_request_body_limit,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._messages_constants import (
    ANTHROPIC_ERROR_STOP_REASONS,
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_REASONING_PARAMETER_NAMES,
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
    _merge_anthropic_system_parts,
    _to_anthropic_messages,
    apply_anthropic_cache_usage,
    apply_anthropic_reasoning_usage,
    extract_anthropic_usage,
    messages_returned_reasoning,
)
from core.providers._tool_calls import WIRE_TOOL_CALL_ID_PROFILES
from core.providers._wire_learning import (
    execute_learning_from_rejections,
    stream_learning_from_rejections,
)
from core.providers.adapter import (
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
    remove_reasoning_kwargs,
)
from core.providers.reasoning_dialects import (
    dialect_request_fields,
    render_reasoning,
)
from core.providers.token_getter import OAuthRequestRecovery, StaticTokenGetter, TokenGetter
from core.providers.wire_profile import Protocol, WireProfile
from core.utils.retry import retry_async
from core.utils.tokens import estimate_structured_tokens

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "ANTHROPIC_ERROR_STOP_REASONS",
    "ANTHROPIC_OVERLOADED_STATUS",
    "ANTHROPIC_REASONING_PARAMETER_NAMES",
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
    "TEXT_BLOCK_TYPE",
    "THINKING_BLOCK_TYPE",
    "TOOL_USE_BLOCK_TYPE",
    "apply_anthropic_cache_usage",
    "apply_anthropic_reasoning_usage",
    "extract_anthropic_usage",
]


class AnthropicCompatibleAdapter(ProviderAdapter):
    """Adapter for Anthropic Messages-compatible APIs.

    Uses the ``/messages`` endpoint with Anthropic's own request and response
    format.  Provider-specific differences (base URL, auth header, extra
    headers, default parameters) come from ``ProviderConfig``; tool-call ids,
    output limits, sampling parameters, reasoning, replay, media and prompt
    caching come from the Model's wire profile.

    Args:
        config: Immutable provider configuration.
        token_getter: Async callable that returns the current auth token.
        api_version: ``anthropic-version`` header value, when the endpoint needs one.
        extra_retryable_statuses: Provider-specific retryable HTTP statuses.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("messages",)

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
        extra_retryable_statuses: frozenset[int] = frozenset(),
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        self._api_version = api_version
        self._extra_retryable_statuses: set[int] = set(extra_retryable_statuses)
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site but is not used by the Messages wire.
        del connection_mode
        super().__init__(model_lookup=model_lookup, debug_recorder=debug_recorder)
        self._owns_client = client is None
        self._client = client or build_async_client(
            base_url=base_url or config.base_url,
            debug_recorder=debug_recorder,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @override
    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

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

    @override
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
        usage = extract_anthropic_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the Messages wire without counting duplicate readable thinking."""
        profile = self.wire_profile(model_id)
        wire = _normalize_tool_call_ids(profile, [dict(message) for message in messages])
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
                include_thinking_blocks=not (
                    profile.replay.strip_when_off and profile.reasoning.supported is False
                ),
            )
        }
        if system is not None:
            payload["system"] = system
        if tools:
            _apply_anthropic_tools(
                payload, {"tools": list(tools)}, profile=profile.request.tool_schema
            )
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
        array) and assembles model, messages, defaults, and overrides, shaped
        by the Model's wire profile.

        Raises:
            ProviderError: (not retryable, nothing sent) when the wire profile
                does not admit the Model or refuses a request parameter.
        """
        self._refuse_unadmitted_model(model_id)
        profile = self.wire_profile(model_id)
        rules = profile.request
        wire_messages = _normalize_tool_call_ids(profile, messages)
        # ``None``-valued caller kwargs mean "not specified" — drop them so they
        # do not clobber provider defaults below. Falsy-but-non-None values
        # (e.g. ``temperature=0.0``) must survive.
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
        _apply_anthropic_tools(payload, request_kwargs, profile=rules.tool_schema)
        reasoning_supported = profile.reasoning.supported
        # Resolve the output allowance once: it both bounds any thinking budget
        # and is the ``max_tokens`` that goes on the wire (set after overrides
        # below so it wins over the provider-default fallback).
        resolved_max_tokens = self._resolve_max_tokens(
            request_kwargs,
            model_id,
            wire_messages,
            tools=kwargs.get("tools"),
        )
        unrendered = set(payload)
        self._apply_reasoning(
            payload,
            request_kwargs,
            model_id,
            reasoning_supported=reasoning_supported,
            max_tokens=resolved_max_tokens,
        )
        rendered = set(payload) - unrendered
        provider_label = self._config.name
        # Shape caller values first so a dropped caller value never
        # displaces a field the reasoning dialect rendered.
        rules.shape_parameters(
            request_kwargs, reasoning_active=False, provider_label=provider_label
        )

        # Replayed thinking blocks must not be sent when the outgoing request
        # explicitly disables thinking or the model cannot reason; with the
        # thinking parameter merely absent they are kept (Anthropic guidance:
        # omitting blocks is the risk, the server drops unusable ones).
        thinking = request_kwargs.get("thinking", payload.get("thinking"))
        strip_thinking = profile.replay.strip_when_off and (
            reasoning_supported is False or _thinking_type(thinking) == "disabled"
        )
        payload["messages"] = _to_anthropic_messages(
            conversation_messages,
            include_thinking_blocks=not strip_thinking,
        )

        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        # Pin the resolved output allowance so the model's own ceiling wins
        # over the provider-default fallback (Anthropic requires a positive
        # ``max_tokens`` and rejects one above the model's output ceiling).
        if resolved_max_tokens is not None:
            payload["max_tokens"] = resolved_max_tokens
        rules.apply_body(payload)
        # Sampling parameters are typically not sent while thinking is active.
        rules.shape_parameters(
            payload,
            reasoning_active=_thinking_type(payload.get("thinking")) in _ACTIVE_THINKING,
            protected=rendered - set(request_kwargs),
            provider_label=provider_label,
        )
        # Cache stable prefixes last, after every other payload mutation, so the
        # markers land on the final system/messages that go on the wire.
        if rules.prompt_cache == "anthropic_breakpoints":
            _apply_prompt_caching(payload)
        return payload

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
        """Plan the Agent's reasoning effort and render it onto the payload.

        Consumes ``thinking_effort`` from ``request_kwargs``;
        :meth:`ReasoningWire.plan` decides and the profile's reasoning dialect
        spells the decision. A Model known not to reason has every raw
        reasoning control (including the dialect's own fields) stripped and
        gets nothing. ``max_tokens`` (the resolved output allowance) bounds any
        thinking budget so it stays strictly under the output cap.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        wire = self.wire_profile(model_id).reasoning
        if reasoning_supported is False:
            remove_reasoning_kwargs(
                request_kwargs,
                *ANTHROPIC_REASONING_PARAMETER_NAMES,
                *dialect_request_fields(wire.dialect),
            )
            return
        render_reasoning(
            wire,
            wire.plan(thinking_effort, output_allowance=max_tokens),
            payload,
            output_allowance=max_tokens,
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
        3. The wire profile's ``output_limit_default``, then the provider-config
           ``max_tokens`` default, as the fallback only when the ceiling is
           unknown (no lookup / offline refresh).

        The wire profile's ``output_limit_cap`` bounds the result.
        """

        explicit = request_kwargs.get("max_tokens")
        if not _is_positive_int(explicit):
            request_kwargs.pop("max_tokens", None)
            explicit = None
        tool_definitions = tools if isinstance(tools, list) else None
        estimated_input = resolve_request_input_budget(
            model_id,
            lambda: self.estimate_request_input_tokens(
                messages, model_id=model_id, tools=tool_definitions
            ),
        )
        rules = self.wire_profile(model_id).request
        default = rules.output_limit_default or (
            self._config.defaults.get("max_tokens") if self._config.defaults else None
        )
        resolved = resolve_request_output_limit(
            explicit_limit=explicit,
            model_output_limit=self._model_max_output_tokens(model_id),
            provider_default=default,
            effective_context_window=resolve_context_window(
                self._model_context_window(model_id), self._config
            ),
            estimated_input_tokens=estimated_input,
        )
        if resolved is not None and rules.output_limit_cap is not None:
            resolved = min(resolved, rules.output_limit_cap)
        return resolved

    def _model_max_output_tokens(self, model_id: str) -> int | None:
        """The model's catalog output ceiling, or ``None`` when it is unknown.

        Strips a ``::variant`` suffix and tolerates a missing lookup or model.
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
        except json.JSONDecodeError, AttributeError:
            if response_body:
                detail = f"{status_code}: {response_body}"
        return detail

    # ------------------------------------------------------------------
    # send() — non-streaming
    # ------------------------------------------------------------------

    @override
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

        request_headers = self._stable_request_headers(model_id, kwargs)
        payload = self._build_payload(messages, model_id, **kwargs)
        await enforce_request_body_limit(payload, self.request_body_limit(model_id))

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

        response = await execute_learning_from_rejections(
            lambda: auth_recovery.run(lambda: retry_async(_do_request)),
            payload,
            rebuild=lambda: self._build_payload(messages, model_id, **kwargs),
            wire=self.wire,
            model_id=model_id,
            provider_label=self._config.id,
        )
        if messages_returned_reasoning(response):
            self.wire.observe_reasoning_returned(model_id)
        return response

    # ------------------------------------------------------------------
    # stream() — SSE streaming
    # ------------------------------------------------------------------

    @override
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
        request_headers = self._stable_request_headers(model_id, kwargs)

        def build_stream_payload() -> dict[str, Any]:
            built = self._build_payload(messages, model_id, **kwargs)
            built["stream"] = True
            return built

        payload = build_stream_payload()
        await enforce_request_body_limit(payload, self.request_body_limit(model_id))
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

        async def _stream_payload() -> AsyncGenerator[dict[str, Any]]:
            response = await connect_streaming_with_retry(
                self._client,
                MESSAGES_ENDPOINT,
                payload,
                build_headers=_build_headers,
                handle_error_status=_handle_error_status,
                auth_recovery=auth_recovery,
            )
            async with aclosing(self._stream_response_deltas(response)) as deltas:
                async for delta in deltas:
                    yield delta

        async with aclosing(
            stream_learning_from_rejections(
                _stream_payload,
                payload,
                rebuild=build_stream_payload,
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def _stream_response_deltas(
        self, response: httpx.Response
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield the normalized deltas of an established SSE stream, then close it."""

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


_ACTIVE_THINKING = frozenset({"adaptive", "enabled"})


def _thinking_type(thinking: Any) -> str | None:
    """The ``type`` of a Messages ``thinking`` value, or ``None`` when absent."""

    if isinstance(thinking, dict):
        kind = thinking.get("type")
        return kind if isinstance(kind, str) else None
    return None


def _normalize_tool_call_ids(
    profile: WireProfile, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    id_profile = WIRE_TOOL_CALL_ID_PROFILES.get(profile.request.tool_call_ids)
    if id_profile is None:
        return messages
    return normalize_tool_call_ids(messages, id_profile)
