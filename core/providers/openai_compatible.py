"""OpenAI-compatible provider adapter.

Handles the ``/chat/completions`` endpoint format used by OpenAI, Groq,
Together, and other providers that follow the OpenAI API convention.
Differences in base URL, auth headers, and default parameters are expressed
through ``ProviderConfig``. Providers that are mostly OpenAI-compatible but need
provider-specific behavior can subclass this adapter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import (
    build_async_client,
    build_streaming_request,
    classify_http_status,
    decode_response_json,
    iter_sse_events,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    TERMINAL_OUTCOME_CONTENT_FILTERED,
    TERMINAL_OUTCOME_ERROR,
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    ModelLookup,
    ProviderAdapter,
    TerminalOutcome,
    normalize_tool_call_candidates,
    project_tool_result_content_fallbacks,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import (
    AuthConfig,
    ProviderConfig,
    resolve_context_window,
    resolve_request_output_limit,
)
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    normalize_thinking_effort,
    reasoning_token_count,
    remove_reasoning_kwargs,
    resolve_reasoning_intent,
    warn_effort_swallowed,
    warn_rejected_effort,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.providers.tool_schema import ToolSchemaProfile, render_tool_definitions
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.tokens import estimate_request_input_tokens

_LOGGER = get_logger("providers.openai_compatible")

# ---------------------------------------------------------------------------
# SSE parsing constants
# ---------------------------------------------------------------------------

SSE_DONE_MARKER = "[DONE]"
CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
OPENAI_REASONING_EFFORTS = {"low", "medium", "high"}
OPENAI_REASONING_EFFORTS_WITH_NONE = {"none", *OPENAI_REASONING_EFFORTS}
OPENAI_NONE_REASONING_PROVIDER_IDS = {"openai"}
OPENAI_REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_text", "thinking")
OPENAI_REASONING_META_KEYS = ("encrypted_content", "reasoning_details")
_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY = "openai_reasoning_details"
# Tracks the last Provider id seen per raw wire index plus the virtual slot it
# was redirected to, so an endpoint that reuses one index for every parallel
# Tool Call (observed on Ollama-compatible servers, distinguished only by id)
# does not collapse the batch into a single accumulator slot.
_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY = "openai_tool_call_index_ids"
# The provider-scoped metadata field naming WHICH wire field carries the
# response reasoning (Phase 5, projected from models.dev ``interleaved``):
# ``{field: "reasoning_content"}`` → visible text, ``{field: "reasoning_details"}``
# → opaque meta. It is read GRACEFULLY: when present it makes the named field the
# preferred source; when absent ``normalize_response`` falls back to today's
# hardcoded default-key scan, so it works whether or not catalogs carry the field.
REASONING_RESPONSE_FIELD_METADATA_KEY = "reasoning_response_field"
OPENAI_TOOL_FINISH_REASONS = {"tool_calls", "function_call"}
OPENAI_ERROR_FINISH_REASONS = {
    "cancelled",
    "error",
    "failed",
    "network_error",
    "server_error",
}
DEFAULT_MAX_OUTPUT_TOKENS = 8192
CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "contextWindow")
MAX_OUTPUT_TOKEN_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "maxOutputTokens",
    "maxCompletionTokens",
)
JSON_MODE_PARAMETER_NAMES = {"response_format", "structured_outputs", "json_mode"}
REASONING_PARAMETER_NAMES = {"reasoning", "include_reasoning", "reasoning_effort"}
# Any of these on a request means the caller set the output allowance
# explicitly, so the model-ceiling default must not override it.
OUTPUT_LIMIT_PARAMETER_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")


class OpenAICompatibleAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible API providers.

    Uses the ``/chat/completions`` endpoint with the standard OpenAI request
    and response format.  Provider-specific differences (base URL, auth header,
    extra headers, default parameters) come from ``ProviderConfig`` — no
    subclassing required.

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
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        self._connection_mode = connection_mode
        super().__init__(model_lookup=model_lookup, debug_recorder=debug_recorder)
        self._client = build_async_client(
            base_url=base_url or config.base_url,
            debug_recorder=debug_recorder,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Wire media capability
    # ------------------------------------------------------------------

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Images plus the OpenAI ``input_audio`` format set (WAV/MP3).

        This is exactly what the shared ``/chat/completions`` content
        translator encodes today, so generic OpenAI-compatible providers
        (OpenRouter, MiniMax, OpenCode-Go, Mistral) inherit the correct set.
        ``application/pdf`` is deliberately *not* declared here: the base wire
        is unverified for documents, so only concrete, verified adapters opt in.
        """
        del model_id
        return IMAGE_WIRE_MEDIA_TYPES | frozenset(_OPENAI_INPUT_AUDIO_FORMATS)

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize a standard OpenAI-compatible ``/models`` entry."""

        architecture = _read_optional_mapping(raw, "architecture")
        top_provider = _read_optional_mapping(raw, "top_provider")
        supported_parameters = _read_optional_string_set(raw, "supported_parameters")
        input_modalities = _read_first_optional_string_tuple(
            (architecture, raw),
            ("input_modalities", "inputModalities", "modalities"),
        )
        output_modalities = _read_first_optional_string_tuple(
            (architecture, raw),
            ("output_modalities", "outputModalities"),
        )

        model_id = _read_non_empty_string(raw, "id")
        name = _read_optional_non_empty_string(raw, "name") or model_id

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=_has_image_modality(raw, architecture),
                tools=_supports_tools_by_default(raw, top_provider, architecture),
                json_mode=_supports_json_mode(
                    raw,
                    top_provider,
                    architecture,
                    supported_parameters,
                ),
                reasoning=ReasoningCapabilities(
                    supported=_supports_reasoning(
                        raw,
                        top_provider,
                        architecture,
                        supported_parameters,
                    ),
                ),
                input_modalities=input_modalities,
                output_modalities=output_modalities or ("text",),
                supported_parameters=tuple(supported_parameters),
            ),
            # A window-less endpoint leaves this ``None`` (honest "unknown") —
            # never a fake ``0`` masquerading as a discovered fact. The read-side
            # default chain (``resolve_context_window``) fills the gap at use time.
            context_window=_read_first_optional_int(raw, CONTEXT_WINDOW_KEYS)
            or _read_first_optional_int(architecture, CONTEXT_WINDOW_KEYS),
            max_output_tokens=_read_first_optional_int(top_provider, MAX_OUTPUT_TOKEN_KEYS)
            or _read_first_optional_int(raw, MAX_OUTPUT_TOKEN_KEYS)
            or _read_first_optional_int(architecture, MAX_OUTPUT_TOKEN_KEYS),
        )

    async def _build_headers(self) -> dict[str, str]:
        """Build request headers; keyless connections contribute no auth header."""
        token = await self._token_getter()
        headers: dict[str, str] = {}
        if self._auth_config.header and token:
            headers[self._auth_config.header] = f"{self._auth_config.prefix}{token}"
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    async def _build_request_headers(
        self,
        messages: list[dict[str, Any]],
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        """Build headers that may depend on one concrete request payload."""

        del messages, payload
        return await self._build_headers()

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize an OpenAI-compatible response to canonical assistant fields.

        When the model's catalog metadata names a reasoning response field
        (``metadata.<provider>.reasoning_response_field``), that field is the
        PREFERRED source for the reasoning; otherwise the hardcoded default-key
        scan applies, so this works whether or not catalogs carry the projected
        field (Phase 5, graceful).
        """
        message = _first_choice_message(response)
        content = message.get("content")
        preferred_field = self._reasoning_response_field(model_id)
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": content if isinstance(content, str) or content is None else str(content),
            "reasoning": _extract_openai_reasoning(message, preferred_field=preferred_field),
            "reasoning_meta": _extract_openai_reasoning_meta(
                message, preferred_field=preferred_field
            ),
            "tool_calls": _extract_openai_tool_calls(message),
        }
        normalized["terminal_outcome"] = _extract_openai_terminal_outcome(
            response,
            has_tool_calls=bool(normalized["tool_calls"]),
        )
        usage = _extract_openai_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def _reasoning_response_field(self, model_id: str | None) -> str | None:
        """Resolve the data-driven reasoning response field for ``model_id``.

        Reads ``metadata.<provider>.reasoning_response_field`` from the injected
        catalog, where ``<provider>`` is this adapter's id with hyphens
        normalized to underscores (matching the provider-scoped metadata key
        convention, e.g. ``opencode_go``). Returns ``None`` — falling back to the
        hardcoded default-key scan — when there is no lookup, no model, or no
        such metadata field.
        """

        if model_id is None or self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        provider_metadata = model.metadata.get(self._config.id.replace("-", "_"))
        if not isinstance(provider_metadata, Mapping):
            return None
        field_name = provider_metadata.get(REASONING_RESPONSE_FIELD_METADATA_KEY)
        return field_name if isinstance(field_name, str) and field_name else None

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Convert an internal assistant message to its wire representation.

        Subclasses may override this to inject provider-specific fields
        (e.g. ``reasoning_content`` for DeepSeek-compatible endpoints).
        """
        return _to_openai_assistant_message(message)

    def _format_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Convert one internal message to its wire representation."""
        if message.get("role") == "assistant":
            return self._format_assistant_message(message)
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            return {
                "role": "user",
                "content": [self._format_user_content_part(part) for part in message["content"]],
            }
        return _to_openai_message(message)

    def _format_user_content_part(self, part: Any) -> dict[str, Any]:
        """Encode one canonical user content part for this provider's wire."""
        return _to_openai_user_content_part(part)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the request payload with model, messages, defaults, and overrides."""
        # ``None``-valued caller kwargs mean "not specified" — drop them so they
        # do not clobber provider defaults below. Falsy-but-non-None values
        # (e.g. ``temperature=0.0``) must survive.
        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        self._apply_model_output_limit(request_kwargs, model_id, messages)
        projected_messages = project_tool_result_content_fallbacks(messages)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [self._format_message(message) for message in projected_messages],
        }
        _apply_openai_tools(
            payload,
            request_kwargs,
            profile=("explicit_non_strict" if self._config.id == "openai" else "omit_strict"),
        )
        self._apply_reasoning(payload, request_kwargs, model_id)
        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                if key == "max_tokens" and any(
                    alias in request_kwargs
                    for alias in ("max_completion_tokens", "max_output_tokens")
                ):
                    continue
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        return payload

    def _apply_model_output_limit(
        self,
        request_kwargs: dict[str, Any],
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Resolve the output allowance and clamp it to remaining context.

        The provider-config ``max_tokens`` default is a flat fallback (commonly
        8192) applied to every model regardless of its real ceiling, so a model
        whose output ceiling is higher gets truncated — reasoning models most of
        all, since their thinking trace counts toward the same allowance. When
        the caller set no explicit output limit and the catalog knows this
        model's ``max_output_tokens``, inject it as ``max_tokens`` so it rides in
        ``request_kwargs`` and wins over the flat config default (applied last in
        :meth:`_build_payload`). The selected allowance is then capped against
        the current messages, Tool definitions, and effective context window so
        a ceiling equal to the whole context cannot produce an invalid request.
        Mirrors the Anthropic adapter's context-aware ``max_tokens``.
        """

        explicit_values: list[int] = []
        for key in OUTPUT_LIMIT_PARAMETER_NAMES:
            explicit_value = _positive_int(request_kwargs.get(key))
            if explicit_value is not None:
                explicit_values.append(explicit_value)
        explicit_limit = min(explicit_values) if explicit_values else None
        for key in OUTPUT_LIMIT_PARAMETER_NAMES:
            if key in request_kwargs and _positive_int(request_kwargs[key]) is None:
                request_kwargs.pop(key)

        tools = request_kwargs.get("tools")
        tool_definitions = tools if isinstance(tools, list) else None
        estimated_input, _ = estimate_request_input_tokens(messages, tool_definitions)
        resolved = resolve_request_output_limit(
            explicit_limit=explicit_limit,
            model_output_limit=self._model_max_output_tokens(model_id),
            provider_default=(
                self._config.defaults.get("max_tokens") if self._config.defaults else None
            ),
            effective_context_window=resolve_context_window(
                self._model_context_window(model_id), self._config
            ),
            estimated_input_tokens=estimated_input,
        )
        if resolved is None:
            return
        if explicit_values:
            for key in OUTPUT_LIMIT_PARAMETER_NAMES:
                if _positive_int(request_kwargs.get(key)) is not None:
                    request_kwargs[key] = min(int(request_kwargs[key]), resolved)
            return
        request_kwargs["max_tokens"] = resolved

    def _model_max_output_tokens(self, model_id: str) -> int | None:
        """The model's catalog output ceiling, or ``None`` when it is unknown.

        Strips a ``::variant`` connection suffix and tolerates a missing lookup
        or model. A gateway whose ids need richer resolution (flat vendor-prefixed
        namespaces) overrides this.
        """

        if self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        ceiling = model.max_output_tokens
        if isinstance(ceiling, int) and not isinstance(ceiling, bool) and ceiling > 0:
            return ceiling
        return None

    def _model_context_window(self, model_id: str) -> int | None:
        """The Model's catalog context window, or ``None`` when unknown."""

        if self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        context_window = model.context_window
        if (
            isinstance(context_window, int)
            and not isinstance(context_window, bool)
            and context_window > 0
        ):
            return context_window
        return None

    def _model_reasoning_supported(self, model_id: str) -> bool | None:
        return model_reasoning_supported(self._model_lookup, model_id)

    def _supported_reasoning_efforts(self, model_id: str) -> set[str] | tuple[str, ...]:
        """Return the effort ladder to snap against for one model.

        The effective per-model ladder from the DB
        (``capabilities.reasoning.levels``) wins when present, so snapping
        follows what this provider actually supports for this model. The
        hardcoded adapter constant is only the floor for a model with no feed
        ladder (e.g. opencode-go, whose ladder is clobbered upstream — Phase 5).
        """
        ladder = model_reasoning_levels(self._model_lookup, model_id)
        if ladder is not None:
            return ladder
        return self._reasoning_efforts_floor()

    def _reasoning_efforts_floor(self) -> set[str]:
        if self._config.id in OPENAI_NONE_REASONING_PROVIDER_IDS:
            return OPENAI_REASONING_EFFORTS_WITH_NONE
        return OPENAI_REASONING_EFFORTS

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
    ) -> None:
        """Resolve the shared reasoning intent and render it onto the payload.

        Consumes ``thinking_effort``/``reasoning_effort`` from ``request_kwargs``,
        resolves the provider-neutral intent via :func:`resolve_reasoning_intent`,
        and renders it (see :meth:`_render_reasoning`). A catalog-known
        non-reasoning model strips the raw reasoning controls and sends nothing,
        exactly as before.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        reasoning_effort = request_kwargs.pop("reasoning_effort", "")
        reasoning_supported = self._model_reasoning_supported(model_id)
        if reasoning_supported is False:
            remove_reasoning_kwargs(request_kwargs, *REASONING_PARAMETER_NAMES)
            return
        intent = resolve_reasoning_intent(
            supported=reasoning_supported,
            control=model_reasoning_control(self._model_lookup, model_id),
            levels=tuple(self._supported_reasoning_efforts(model_id)),
            effort=thinking_effort or reasoning_effort,
            budget_max=model_reasoning_budget_max(self._model_lookup, model_id),
            # The generic ``/chat/completions`` wire has no native token budget,
            # so a budget intent degrades to an effort and the budget is never
            # materialized — passing ``max_tokens`` would not change the output.
            max_tokens=None,
        )
        self._render_reasoning(payload, intent, reasoning_supported=reasoning_supported)

    def _render_reasoning(
        self,
        payload: dict[str, Any],
        intent: ReasoningIntent,
        *,
        reasoning_supported: bool | None,
    ) -> None:
        """Render a reasoning intent onto the generic OpenAI-compatible wire.

        The base wire only speaks ``reasoning_effort``: an ``effort`` intent maps
        straight through; ``budget``/``on`` have no native field and degrade to
        the snapped effort; ``off`` sends ``reasoning_effort: "none"`` only for an
        OpenAI-style provider that proves it supports the ``none`` value
        (``effort_level == "none"`` and reasoning confirmed), otherwise nothing;
        ``default`` leaves the provider default untouched.
        """

        if intent.kind == REASONING_INTENT_EFFORT:
            payload["reasoning_effort"] = intent.effort_level
        elif intent.kind in (REASONING_INTENT_BUDGET, REASONING_INTENT_ON):
            if intent.effort_level is not None:
                payload["reasoning_effort"] = intent.effort_level
        elif (
            intent.kind == REASONING_INTENT_OFF
            and reasoning_supported is True
            and intent.effort_level == "none"
        ):
            payload["reasoning_effort"] = "none"

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
            detail=detail,
            response_headers=response_headers,
        )

    def _wrap_transport_error(self, exc: httpx.TransportError) -> Exception:
        """Wrap a transport failure, allowing concrete gateways to add context."""

        return wrap_network_error(exc)

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
        """Send a non-streaming chat completion request.

        Retries on retryable errors (429, 502, 503) via ``retry_async``.
        Fails immediately on auth errors (401/403).

        Args:
            messages: Conversation messages in OpenAI format.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Returns:
            Parsed response dict from the provider.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            NetworkError: Connection errors (retried, then raised).
            ProviderTimeoutError: Timeout errors (retried, then raised).
            ProviderError: Other HTTP errors.
        """

        # Capture the agent-selected effort before ``_build_payload`` consumes the
        # reasoning kwargs, so the observability signals below can name it.
        selected_effort = _selected_thinking_effort(kwargs)

        async def _do_request() -> dict[str, Any]:
            payload = self._build_payload(messages, model_id, **kwargs)
            headers = await self._build_request_headers(messages, payload)
            try:
                response = await self._client.post(
                    CHAT_COMPLETIONS_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc

            reason = response.text
            detail = (
                f"{response.status_code} {reason}".strip() if reason else str(response.status_code)
            )
            # Surface a rejected reasoning effort (strict providers return 400)
            # before classifying — classification and retry policy are unchanged.
            warn_rejected_effort(
                status_code=response.status_code,
                detail=detail,
                model_id=model_id,
                selected_effort=selected_effort,
                provider_logger=_LOGGER,
            )
            self._classify_http_status(
                response.status_code,
                detail=detail,
                response_headers=response.headers,
            )
            parsed = dict(decode_response_json(response, "OpenAI-compatible provider"))
            # A non-``none`` effort that comes back with 0 reasoning tokens was
            # effectively swallowed by the provider — surface it.
            warn_effort_swallowed(
                selected_effort=selected_effort,
                usage=parsed.get("usage"),
                model_id=model_id,
                provider_logger=_LOGGER,
            )
            return parsed

        return await retry_async(_do_request)

    # ------------------------------------------------------------------
    # stream() — SSE streaming
    # ------------------------------------------------------------------

    def _prepare_stream_payload(self, payload: dict[str, Any]) -> None:
        """Apply the generic OpenAI streaming request extensions.

        Concrete compatible providers can override this when their documented
        wire supports SSE but not OpenAI's optional ``stream_options`` field.
        """

        payload["stream"] = True
        _merge_stream_usage_options(payload)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming request and yield normalized provider-agnostic deltas.

        Retries the initial connection on retryable errors (429, 502, 503).
        Once the stream is established, parses OpenAI-compatible SSE chunks
        into ``content_delta``, ``reasoning_delta``, ``reasoning_meta``,
        ``tool_call_delta``, and ``finish`` dictionaries until the ``[DONE]``
        marker is received.

        Args:
            messages: Conversation messages in OpenAI format.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Yields:
            Normalized delta dictionaries consumed by the chat layer.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            NetworkError: Connection and mid-stream read errors.
            ProviderTimeoutError: Timeout errors (initial connection retried;
                mid-stream timeouts raised).
            ProviderError: Other HTTP errors and in-band stream/provider
                error payloads.
        """
        payload = self._build_payload(messages, model_id, **kwargs)
        self._prepare_stream_payload(payload)

        async def _connect_stream() -> httpx.Response:
            # Rebuild headers per attempt: an OAuth token may refresh during a
            # retry backoff, and the getter must be re-consulted each time.
            headers = await self._build_request_headers(messages, payload)
            request = build_streaming_request(
                self._client,
                "POST",
                CHAT_COMPLETIONS_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc

            # If the status indicates an error, read and close the response
            # before classifying — this frees the connection for retry.
            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                detail = (
                    f"{response.status_code} {error_body}".strip()
                    if error_body
                    else str(response.status_code)
                )
                self._classify_http_status(
                    response.status_code,
                    detail=detail,
                    response_headers=response.headers,
                )
                # classify_http_status always raises for >= 400; this is unreachable
                # but satisfies type checkers.
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        response = await retry_async(_connect_stream)

        tool_call_slots: set[int] = set()
        normalization_state: dict[str, Any] = {}
        seen_done_marker = False

        try:
            async for event in iter_sse_events(response):
                if event.comment is not None:
                    yield {"type": "heartbeat"}
                    continue
                data = event.data
                if data is None:
                    continue
                if data.strip() == SSE_DONE_MARKER:
                    seen_done_marker = True
                    break
                raw_chunk = parse_sse_json_data(
                    data,
                    context="OpenAI-compatible provider",
                )
                if not isinstance(raw_chunk, dict):
                    raise ProviderError(
                        "OpenAI-compatible provider sent non-object JSON in stream",
                        retryable=False,
                    )
                for normalized_delta in self._normalize_stream_chunk(
                    raw_chunk,
                    tool_call_slots,
                    normalization_state,
                ):
                    yield normalized_delta
            if not seen_done_marker:
                raise NetworkError("Stream ended without [DONE] marker")
        except httpx.TimeoutException as exc:
            raise self._wrap_transport_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_slots: set[int],
        normalization_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return _normalize_openai_stream_chunk(
            raw_chunk,
            tool_call_slots,
            normalization_state=normalization_state,
        )


def _normalize_openai_stream_chunk(
    chunk: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    error = chunk.get("error")
    if isinstance(error, dict):
        message = error.get("message") or str(error)
        raise ProviderError(f"Provider stream error: {message}", retryable=False)

    normalized_deltas: list[dict[str, Any]] = []
    for choice in _stream_choices(chunk):
        delta = choice.get("delta", {})
        if isinstance(delta, dict):
            normalized_deltas.extend(
                _normalize_openai_message_delta(
                    delta,
                    tool_call_slots,
                    normalization_state=normalization_state,
                )
            )

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            normalized_deltas.append(
                {
                    "type": "finish",
                    "reason": _normalize_openai_finish_reason(
                        finish_reason,
                        has_tool_calls=bool(tool_call_slots),
                    ),
                }
            )

    # OpenAI streaming includes usage only in the final chunk when
    # stream_options.include_usage is set. Extract token usage when present.
    usage_delta = _extract_stream_usage(chunk)
    if usage_delta is not None:
        normalized_deltas.append(usage_delta)

    return normalized_deltas


def _stream_choices(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    choices = chunk.get("choices", [])
    if not isinstance(choices, list):
        return []
    return [choice for choice in choices if isinstance(choice, dict)]


def _normalize_openai_message_delta(
    delta: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized_deltas: list[dict[str, Any]] = []
    content = delta.get("content")
    if isinstance(content, str) and content:
        normalized_deltas.append({"type": "content_delta", "text": content})

    reasoning = _extract_openai_reasoning(delta)
    if reasoning:
        normalized_deltas.append({"type": "reasoning_delta", "text": reasoning})

    reasoning_meta = _extract_openai_reasoning_meta(delta)
    if reasoning_meta:
        reasoning_meta = _accumulate_openai_stream_reasoning_details(
            reasoning_meta,
            normalization_state,
        )
        normalized_deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})

    normalized_deltas.extend(
        _normalize_openai_tool_call_deltas(
            delta,
            tool_call_slots,
            normalization_state=normalization_state,
        )
    )
    return normalized_deltas


def _normalize_openai_tool_call_deltas(
    delta: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_tool_calls = delta.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    normalized_deltas: list[dict[str, Any]] = []
    for position, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, dict):
            continue
        tool_call_index = _openai_tool_call_index(raw_tool_call, position)
        provider_id = raw_tool_call.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            provider_id = None
        tool_call_index = _redirect_reused_tool_call_index(
            tool_call_index,
            provider_id,
            tool_call_slots,
            normalization_state,
        )
        tool_call_slots.add(tool_call_index)
        function = raw_tool_call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        name_delta = function.get("name")
        arguments_delta = function.get("arguments")
        if not isinstance(name_delta, str):
            name_delta = ""
        if not isinstance(arguments_delta, str):
            arguments_delta = ""
        if provider_id is None and not name_delta and not arguments_delta:
            continue
        normalized_delta: dict[str, Any] = {
            "type": "tool_call_delta",
            "slot": tool_call_index,
            "name_delta": name_delta,
            "arguments_delta": arguments_delta,
        }
        if provider_id is not None:
            normalized_delta["id"] = provider_id
        normalized_deltas.append(normalized_delta)
    return normalized_deltas


def _openai_tool_call_index(raw_tool_call: dict[str, Any], position: int) -> int:
    index = raw_tool_call.get("index")
    return index if isinstance(index, int) else position


def _redirect_reused_tool_call_index(
    raw_index: int,
    provider_id: str | None,
    tool_call_slots: set[int],
    normalization_state: dict[str, Any] | None,
) -> int:
    """Return the accumulator slot for a raw wire Tool-call index.

    Ollama-compatible endpoints may reuse one index (typically ``0``) for every
    Tool Call in a parallel batch, distinguishing the calls only by ``id``. A
    same-index delta carrying a *different* id therefore starts a new call and
    is redirected to a fresh virtual slot instead of being merged into the
    previous call's fragments. An id-less fragment keeps the tracked virtual
    slot of its raw index. Requires the per-stream ``normalization_state``
    mapping; without it the raw index wins.
    """

    if normalization_state is None:
        return raw_index
    tracked = normalization_state.get(_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY)
    if not isinstance(tracked, dict):
        tracked = {}
        normalization_state[_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY] = tracked
    entry = tracked.get(raw_index)
    if isinstance(entry, list) and len(entry) == 2:
        last_id, virtual_index = entry
        if (
            isinstance(last_id, str)
            and isinstance(virtual_index, int)
            and not isinstance(virtual_index, bool)
        ):
            if provider_id is None or provider_id == last_id:
                return virtual_index
            fresh_index = max(tool_call_slots, default=raw_index) + 1
            tracked[raw_index] = [provider_id, fresh_index]
            return fresh_index
    if provider_id is None:
        return raw_index
    tracked[raw_index] = [provider_id, raw_index]
    return raw_index


def _normalize_openai_finish_reason(
    finish_reason: Any,
    *,
    has_tool_calls: bool,
) -> TerminalOutcome:
    if finish_reason in OPENAI_TOOL_FINISH_REASONS:
        return TERMINAL_OUTCOME_TOOL_CALLS
    if finish_reason == "stop":
        return TERMINAL_OUTCOME_TOOL_CALLS if has_tool_calls else TERMINAL_OUTCOME_STOP
    if finish_reason == "length":
        return TERMINAL_OUTCOME_OUTPUT_TRUNCATED
    if finish_reason == "content_filter":
        return TERMINAL_OUTCOME_CONTENT_FILTERED
    if finish_reason in OPENAI_ERROR_FINISH_REASONS:
        return TERMINAL_OUTCOME_ERROR
    return TERMINAL_OUTCOME_UNKNOWN


def _extract_openai_terminal_outcome(
    response: dict[str, Any],
    *,
    has_tool_calls: bool,
) -> TerminalOutcome:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return TERMINAL_OUTCOME_UNKNOWN
    return _normalize_openai_finish_reason(
        choices[0].get("finish_reason"),
        has_tool_calls=has_tool_calls,
    )


def _to_openai_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "assistant":
        return _to_openai_assistant_message(message)
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message["tool_call_id"],
            "content": message["content"],
        }
    if role == "user":
        return {
            "role": "user",
            "content": _to_openai_user_content(message.get("content", "")),
        }

    return {
        "role": role,
        "content": message.get("content", ""),
    }


# OpenAI `input_audio` parts accept exactly these formats; the chat layer's
# native-audio gate mirrors this mapping.
_OPENAI_INPUT_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
}


def _to_openai_user_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    return [_to_openai_user_content_part(part) for part in content]


def _to_openai_user_content_part(part: Any) -> dict[str, Any]:
    if not isinstance(part, dict):
        return {"type": "text", "text": "" if part is None else str(part)}

    part_type = part.get("type")
    if part_type == "media":
        base64_data = part.get("base64")
        media_type = part.get("media_type")
        if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
            raise ProviderError(
                "media content block requires string base64 and media_type fields",
                retryable=False,
            )
        if media_type.startswith("image/"):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
            }
        audio_format = _OPENAI_INPUT_AUDIO_FORMATS.get(media_type)
        if audio_format is not None:
            return {
                "type": "input_audio",
                "input_audio": {"data": base64_data, "format": audio_format},
            }
        raise ProviderError(
            f"unsupported media type for OpenAI-compatible wire: {media_type}",
            retryable=False,
        )

    if part_type == "document":
        return _to_openai_file_part(part)

    if part_type == "text":
        text = part.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}

    return dict(part)


def _to_openai_file_part(part: dict[str, Any]) -> dict[str, Any]:
    """Translate a canonical document block into an OpenAI Chat Completions ``file`` part.

    Wire shape verified against the OpenAI Chat Completions file-input API: the
    bytes ride as a ``data:<mime>;base64,...`` URL under ``file_data`` with the
    original ``filename``. Declaring which adapters' wires actually carry
    documents stays in ``wire_media_support`` — this is encoding only.
    """
    base64_data = part.get("base64")
    media_type = part.get("media_type")
    filename = part.get("filename")
    if (
        not isinstance(base64_data, str)
        or not isinstance(media_type, str)
        or not media_type
        or not isinstance(filename, str)
        or not filename
    ):
        raise ProviderError(
            "document content block requires string base64, media_type, and filename fields",
            retryable=False,
        )
    return {
        "type": "file",
        "file": {
            "filename": filename,
            "file_data": f"data:{media_type};base64,{base64_data}",
        },
    }


def _to_openai_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    openai_message: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content"),
    }
    if message.get("tool_calls") is not None:
        openai_message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": json.dumps(tool_call.get("arguments", {}), separators=(",", ":")),
                },
            }
            for tool_call in message["tool_calls"]
        ]
    if openai_message.get("content") is None and "tool_calls" not in openai_message:
        openai_message["content"] = ""
    _apply_openai_reasoning_meta(openai_message, message.get("reasoning_meta"))
    return openai_message


def _apply_openai_tools(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    profile: ToolSchemaProfile,
) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    rendered = render_tool_definitions(
        tools,
        profile=profile,
    )
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
                **({"strict": tool["strict"]} if isinstance(tool.get("strict"), bool) else {}),
            },
        }
        for tool in rendered
    ]


def _selected_thinking_effort(kwargs: Mapping[str, Any]) -> str:
    """Return the agent-selected reasoning effort from request kwargs.

    Mirrors ``_apply_openai_reasoning``'s precedence (``thinking_effort`` wins
    over a raw ``reasoning_effort``) but does not mutate kwargs, so it can read
    the selection before the payload builder consumes it for the observability
    signals. Returns the canonical effort, or an empty string when none was set.
    """
    thinking_effort = kwargs.get("thinking_effort") or ""
    reasoning_effort = kwargs.get("reasoning_effort") or ""
    return normalize_thinking_effort(thinking_effort or reasoning_effort)


def _merge_stream_usage_options(payload: dict[str, Any]) -> None:
    stream_options = payload.get("stream_options")
    if isinstance(stream_options, dict):
        payload["stream_options"] = {**stream_options, "include_usage": True}
        return
    payload["stream_options"] = {"include_usage": True}


def _first_choice_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices", [])
    if not choices:
        return {}
    message = choices[0].get("message", {})
    return message if isinstance(message, dict) else {}


def _extract_openai_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_tool_calls = message.get("tool_calls")
    if not raw_tool_calls:
        return None
    raw_call_values = raw_tool_calls if isinstance(raw_tool_calls, list) else [raw_tool_calls]
    tool_calls: list[dict[str, Any]] = []
    for position, raw_call_value in enumerate(raw_call_values):
        raw_call = raw_call_value if isinstance(raw_call_value, Mapping) else {}
        function_value = raw_call.get("function")
        function = function_value if isinstance(function_value, Mapping) else {}
        tool_calls.extend(
            normalize_tool_call_candidates(
                tool_call_id=raw_call.get("id"),
                name=function.get("name"),
                arguments=function.get("arguments"),
                fallback_id=f"tool_call_{position}",
            )
        )
    return tool_calls or None


def _extract_openai_reasoning(
    message: dict[str, Any], *, preferred_field: str | None = None
) -> str | None:
    """Return the visible reasoning text from an assistant message.

    When ``preferred_field`` names a visible-text reasoning field present as a
    string on the message, it wins; otherwise the default key scan
    (``OPENAI_REASONING_KEYS``) applies. A ``preferred_field`` that is actually a
    meta field (e.g. ``reasoning_details``) carries no visible text, so it is
    ignored here and surfaces through :func:`_extract_openai_reasoning_meta`.
    """

    if preferred_field is not None and preferred_field not in OPENAI_REASONING_META_KEYS:
        value = message.get(preferred_field)
        if isinstance(value, str) and value:
            return value
    for key in OPENAI_REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_openai_reasoning_meta(
    message: dict[str, Any], *, preferred_field: str | None = None
) -> dict[str, Any] | None:
    """Return the opaque reasoning-meta fields from an assistant message.

    The default meta keys (``OPENAI_REASONING_META_KEYS``) are always collected;
    a ``preferred_field`` that names a meta field not already in that set is also
    collected when present, so a catalog-named meta field is preserved for replay
    even if it is not a hardcoded default.
    """

    meta: dict[str, Any] = {}
    for key in OPENAI_REASONING_META_KEYS:
        if key in message:
            meta[key] = message[key]
    if (
        preferred_field is not None
        and preferred_field not in OPENAI_REASONING_KEYS
        and preferred_field not in meta
        and preferred_field in message
    ):
        meta[preferred_field] = message[preferred_field]
    return meta or None


def _accumulate_openai_stream_reasoning_details(
    reasoning_meta: dict[str, Any],
    normalization_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Preserve separately streamed reasoning details in Provider order.

    OpenAI-compatible gateways may emit one ``reasoning_details`` array per
    sibling Tool Call. Chat intentionally treats reasoning metadata as opaque
    and therefore shallow-merges normalized metadata deltas. Emit the complete
    accumulated array from this Adapter so a later sibling cannot overwrite an
    earlier detail. Details carrying a stable ``id`` update in place, retaining
    first-seen order while allowing a Provider's cumulative snapshot to refine
    an earlier fragment.
    """

    details = reasoning_meta.get("reasoning_details")
    if normalization_state is None or not isinstance(details, list):
        return reasoning_meta

    accumulated = normalization_state.get(_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY)
    if not isinstance(accumulated, list):
        accumulated = []
        normalization_state[_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY] = accumulated

    for detail in details:
        normalized_detail = dict(detail) if isinstance(detail, Mapping) else detail
        detail_id = detail.get("id") if isinstance(detail, Mapping) else None
        if isinstance(detail_id, str) and detail_id:
            matching_index = next(
                (
                    index
                    for index, existing in enumerate(accumulated)
                    if isinstance(existing, Mapping) and existing.get("id") == detail_id
                ),
                None,
            )
            if matching_index is not None:
                accumulated[matching_index] = normalized_detail
                continue
        accumulated.append(normalized_detail)

    merged = dict(reasoning_meta)
    merged["reasoning_details"] = [
        dict(detail) if isinstance(detail, Mapping) else detail for detail in accumulated
    ]
    return merged


def _apply_openai_reasoning_meta(
    message: dict[str, Any],
    reasoning_meta: Any,
) -> None:
    if not isinstance(reasoning_meta, dict):
        return
    for key in OPENAI_REASONING_META_KEYS:
        if key in reasoning_meta:
            message[key] = reasoning_meta[key]


def _extract_openai_usage(response: dict[str, Any]) -> dict[str, int] | None:
    """Extract token usage from an OpenAI-compatible response.

    Maps ``prompt_tokens`` → ``input_tokens`` and
    ``completion_tokens`` → ``output_tokens``.  Returns ``None`` when
    the response has no usable usage data.
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    has_input = isinstance(prompt_tokens, int)
    has_output = isinstance(completion_tokens, int)
    if not has_input and not has_output:
        return None
    normalized: dict[str, int] = {}
    if isinstance(prompt_tokens, int):
        normalized["input_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        normalized["output_tokens"] = completion_tokens
    cache_read_tokens = _openai_cached_prompt_tokens(usage)
    if cache_read_tokens is not None:
        normalized["cache_read_tokens"] = cache_read_tokens
    cache_write_tokens = _openai_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        normalized["cache_write_tokens"] = cache_write_tokens
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        normalized["reasoning_tokens"] = reasoning_tokens
    return normalized


def _extract_stream_usage(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an OpenAI-compatible streaming chunk.

    Yields a usage delta only when the chunk contains a ``usage`` dict
    with at least one integer primary token counter. Maps ``prompt_tokens``
    → ``input_tokens`` and ``completion_tokens`` → ``output_tokens``;
    an absent counter remains absent for Chat to estimate independently.

    Returns ``None`` when the chunk has no usable usage data, so that
    callers can skip yielding anything.
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    has_input = isinstance(prompt_tokens, int)
    has_output = isinstance(completion_tokens, int)
    if not has_input and not has_output:
        return None
    delta: dict[str, Any] = {"type": "usage"}
    if has_input:
        delta["input_tokens"] = prompt_tokens
    if has_output:
        delta["output_tokens"] = completion_tokens
    cache_read_tokens = _openai_cached_prompt_tokens(usage)
    if cache_read_tokens is not None:
        delta["cache_read_tokens"] = cache_read_tokens
    cache_write_tokens = _openai_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        delta["cache_write_tokens"] = cache_write_tokens
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        delta["reasoning_tokens"] = reasoning_tokens
    return delta


def _openai_cached_prompt_tokens(usage: dict[str, Any]) -> int | None:
    """Read cache-hit tokens from common OpenAI-compatible response shapes.

    Cached tokens are a subset of ``prompt_tokens`` on the OpenAI wire,
    so no input-token adjustment is needed.
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached_tokens = _optional_non_negative_usage_int(details.get("cached_tokens"))
        if cached_tokens is not None:
            return cached_tokens
    for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens"):
        cached_tokens = _optional_non_negative_usage_int(usage.get(key))
        if cached_tokens is not None:
            return cached_tokens
    return None


def _openai_cache_write_tokens(usage: dict[str, Any]) -> int | None:
    """Read cache-creation tokens from common compatible response shapes."""
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for key in ("cache_write_tokens", "cache_creation_tokens"):
            cache_write_tokens = _optional_non_negative_usage_int(details.get(key))
            if cache_write_tokens is not None:
                return cache_write_tokens
    cache_write_tokens = _optional_non_negative_usage_int(usage.get("cache_creation_input_tokens"))
    if cache_write_tokens is not None:
        return cache_write_tokens
    return None


def _optional_non_negative_usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _read_optional_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _read_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Expected '{key}' to be an object")
    return value


def _read_non_empty_string(data: Mapping[str, Any], key: str) -> str:
    value = _read_string(data, key)
    if not value:
        raise ValueError(f"Expected '{key}' to be a non-empty string")
    return value


def _read_optional_non_empty_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _read_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Expected '{key}' to be a string")
    return value


def _read_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of strings")
    return value


def _read_optional_string_set(data: Mapping[str, Any], key: str) -> set[str]:
    value = data.get(key)
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _read_first_optional_string_tuple(
    metadata_sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return tuple(value)
    return ()


def _read_first_optional_int(data: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        parsed_value = _parse_optional_int(value)
        if parsed_value is not None:
            return parsed_value
    return None


def _parse_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _has_image_modality(raw: Mapping[str, Any], architecture: Mapping[str, Any]) -> bool:
    return _modalities_include_image(raw) or _modalities_include_image(architecture)


def _modalities_include_image(data: Mapping[str, Any]) -> bool:
    for key in ("input_modalities", "inputModalities", "modalities"):
        value = data.get(key)
        if isinstance(value, list) and any(_modality_is_image(item) for item in value):
            return True
    return False


def _modality_is_image(value: Any) -> bool:
    if isinstance(value, str):
        return "image" in value.lower()
    if isinstance(value, dict):
        return any(_modality_is_image(item) for item in value.values())
    return False


def _supports_tools_by_default(*metadata_sources: Mapping[str, Any]) -> bool:
    explicit_value = _read_first_optional_bool(
        metadata_sources,
        ("supports_tools", "tools", "tool_calls", "function_calling"),
    )
    return explicit_value is not False


def _supports_json_mode(
    raw: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & JSON_MODE_PARAMETER_NAMES:
        return True
    explicit_value = _read_first_optional_bool(
        (raw, top_provider, architecture),
        (
            "supports_json_mode",
            "json_mode",
            "supports_structured_outputs",
            "structured_outputs",
        ),
    )
    return explicit_value is True


def _supports_reasoning(
    raw: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & REASONING_PARAMETER_NAMES:
        return True
    if _read_reasoning_supported(raw) or _read_reasoning_supported(architecture):
        return True
    explicit_value = _read_first_optional_bool(
        (raw, top_provider, architecture),
        ("supports_reasoning", "reasoning_supported"),
    )
    if explicit_value is True:
        return True
    return _has_non_empty_list(raw, "reasoning_efforts") or _has_non_empty_list(
        raw,
        "reasoningEfforts",
    )


def _read_reasoning_supported(data: Mapping[str, Any]) -> bool:
    reasoning = data.get("reasoning")
    return isinstance(reasoning, dict) and reasoning.get("supported") is True


def _read_first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...], keys: tuple[str, ...]
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
    return None


def _has_non_empty_list(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, list) and len(value) > 0
