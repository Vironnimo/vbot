"""Anthropic provider adapter.

Handles the ``/messages`` endpoint format used by the Anthropic Messages API.
Owns the full wire protocol: message format, authentication, streaming,
and error classification.

Key differences from the OpenAI-compatible adapter:

- Endpoint: ``/messages`` (not ``/chat/completions``)
- System messages are extracted into a top-level ``system`` field
- Auth: ``x-api-key`` header (no ``Bearer`` prefix)
- Required header: ``anthropic-version``
- Content blocks instead of flat ``content`` strings
- Thinking/reasoning via ``thinking`` and ``output_config`` parameters
- Streaming uses ``event:`` + ``data:`` SSE lines (not ``data:`` only)
- Stream ends on ``message_stop`` event (not ``[DONE]``)
- Anthropic-specific error format and status code 529 (overloaded)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    decode_response_json,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ModelLookup, ProviderAdapter
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    BUDGET_FLOOR_TOKENS,
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningIntent,
    ReasoningReplayPolicy,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    remove_reasoning_kwargs,
    resolve_reasoning_intent,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.anthropic")

# ---------------------------------------------------------------------------
# Anthropic-specific constants
# ---------------------------------------------------------------------------

# Status code 529 is Anthropic-specific: server overloaded.
_HTTP_OVERLOADED = 529

# SSE / API constants
MESSAGES_ENDPOINT = "/messages"
ANTHROPIC_VERSION = "2023-06-01"
# The active-effort floor snapped against when a Claude has no feed ladder (the
# bare-stub catalog today). It spans every active effort, so a selected effort
# passes through unchanged — keeping the effort path byte-identical.
ANTHROPIC_EFFORT_FLOOR = ("minimal", "low", "medium", "high", "xhigh", "max")
# An effort above ``minimal`` carries an ``output_config.effort``; ``minimal``
# rides on adaptive thinking alone.
ANTHROPIC_MINIMAL_EFFORT = "minimal"
ANTHROPIC_REASONING_PARAMETER_NAMES = {
    "thinking",
    "thinking_budget",
    "output_config",
    "reasoning_effort",
    "reasoning",
    "include_reasoning",
}
TEXT_BLOCK_TYPE = "text"
TOOL_USE_BLOCK_TYPE = "tool_use"
THINKING_BLOCK_TYPE = "thinking"
REDACTED_THINKING_BLOCK_TYPE = "redacted_thinking"
REASONING_META_CONTENT_BLOCKS = "content_blocks"

# Prompt caching. A ``cache_control`` marker on a content block tells Anthropic
# to cache the request prefix up to that block (cache reads cost ~0.1x input,
# writes ~1.25x). Caching is a prefix match, so any earlier byte change
# invalidates it, and at most four markers are allowed per request. A marker on
# the last system block caches tools + system together (Anthropic renders tools,
# then system, then messages); the rest roll across the most recent message
# boundaries so the growing conversation tail stays cached as a run progresses.
# Markers on prefixes below the model's cacheable minimum are silent no-ops, so
# no size gate is needed.
CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
CACHE_BREAKPOINT_LIMIT = 4
MAX_HISTORY_CACHE_BREAKPOINTS = 3
# Reasoning blocks are replayed verbatim for round-tripping; the cache breakpoint
# rides on a stabler block (text / tool_use / tool_result) instead.
CACHE_UNMARKABLE_BLOCK_TYPES = frozenset({THINKING_BLOCK_TYPE, REDACTED_THINKING_BLOCK_TYPE})
ANTHROPIC_TOOL_STOP_REASONS = {"tool_use"}
ANTHROPIC_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "pause_turn",
    "refusal",
    "stop_sequence",
}

# Discovery: the Messages catalog lives at ``/models`` and pages at up to 1000
# entries; one page covers the full Claude lineup with room to spare.
MODELS_DISCOVERY_PAGE_SIZE = "1000"
# Sampling parameters Anthropic removed on the adaptive-only generation
# (Opus 4.7+, Fable 5) — these 400 there but are accepted on 4.6 and earlier.
ANTHROPIC_SAMPLING_PARAMETER_NAMES = ("temperature", "top_p", "top_k")
# Provider-scoped per-model metadata: whether the model accepts sampling params.
# Derived at discovery from the live caps (see ``normalize_catalog_entry``) and
# read by ``_model_supports_temperature`` to drop sampling for models that reject it.
ANTHROPIC_METADATA_KEY = "anthropic"
SUPPORTS_TEMPERATURE_METADATA_FIELD = "supports_temperature"
# Stable effort-ladder order for projecting the catalog's ``effort`` capability.
ANTHROPIC_EFFORT_LEVEL_ORDER = ("low", "medium", "high", "xhigh", "max")


class AnthropicAdapter(ProviderAdapter):
    """Adapter for the Anthropic Messages API.

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
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site but is not used by the Anthropic adapter.
        del connection_mode
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

    async def __aenter__(self) -> AnthropicAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # History shaping policy
    # ------------------------------------------------------------------

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay persisted thinking blocks across runs, per Anthropic guidance.

        Anthropic expects thinking blocks passed back unchanged for the whole
        same-model conversation, not just the active tool loop; stripping them
        risks signature/ordering 400s and breaks provider-side prompt caching.
        Cross-model entries are stripped by the chat layer's same-model gate.
        """
        del model_id
        return REASONING_REPLAY_FULL_HISTORY

    # ------------------------------------------------------------------
    # Wire media capability
    # ------------------------------------------------------------------

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """The Anthropic Messages wire carries images plus native ``application/pdf``.

        PDFs ride as a base64 ``document`` block (verified shape); other office
        formats are out of scope until their wire is probed.
        """
        del model_id
        return IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"}

    # ------------------------------------------------------------------
    # Catalog discovery
    # ------------------------------------------------------------------

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        credential_value: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Add the required ``anthropic-version`` header for ``/models`` discovery.

        The auth header (``x-api-key``) is already supplied by the discovery
        pipeline from the connection config; the Messages API additionally
        requires a version header on every request, listing included.
        """

        del credential_value
        return {**headers, "anthropic-version": ANTHROPIC_VERSION}

    @classmethod
    def discovery_params(cls) -> dict[str, str]:
        """Page the ``/models`` listing large enough to return the full lineup."""

        return {"limit": MODELS_DISCOVERY_PAGE_SIZE}

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one Anthropic ``/models`` entry into a vBot :class:`Model`.

        The live ``/models`` endpoint is authoritative and rich: it carries the
        context window, output limit, input modalities, structured-output and
        reasoning support, and the per-model ``effort`` ladder. Provider request
        defaults are not model facts and are ignored here.
        """

        del defaults
        model_id = _read_catalog_string(raw, "id")
        name = _read_catalog_string(raw, "display_name") or model_id
        caps = _catalog_mapping(raw, "capabilities")
        thinking = _catalog_mapping(caps, "thinking")
        thinking_types = _catalog_mapping(thinking, "types")

        # ``thinking.supported`` is a direct boolean; the per-type and other
        # capability nodes are ``{supported: bool}`` objects (``_catalog_supported``).
        reasoning_supported = thinking.get("supported") is True
        adaptive = _catalog_supported(thinking_types, "adaptive")
        enabled = _catalog_supported(thinking_types, "enabled")
        effort_levels = tuple(
            level
            for level in ANTHROPIC_EFFORT_LEVEL_ORDER
            if _catalog_supported(_catalog_mapping(caps, "effort"), level)
        )
        control, levels = _anthropic_reasoning_control(
            reasoning_supported, adaptive=adaptive, enabled=enabled, effort_levels=effort_levels
        )

        image = _catalog_supported(caps, "image_input")
        pdf = _catalog_supported(caps, "pdf_input")
        input_modalities = ("text", *(("image",) if image else ()), *(("pdf",) if pdf else ()))

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=image,
                # The catalog has no per-model tool flag; every Claude model
                # supports tool use, so this is a safe provider-wide constant.
                tools=True,
                json_mode=_catalog_supported(caps, "structured_outputs"),
                reasoning=ReasoningCapabilities(
                    supported=reasoning_supported,
                    control=control,
                    levels=levels,
                ),
                input_modalities=input_modalities,
                output_modalities=("text",),
            ),
            context_window=_read_catalog_int(raw, "max_input_tokens"),
            max_output_tokens=_read_catalog_int(raw, "max_tokens"),
            metadata={
                ANTHROPIC_METADATA_KEY: {
                    SUPPORTS_TEMPERATURE_METADATA_FIELD: _anthropic_supports_temperature(
                        reasoning_supported, adaptive=adaptive, enabled=enabled
                    )
                }
            },
        )

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    async def _build_headers(self) -> dict[str, str]:
        """Build request headers for the Anthropic API.

        Includes the auth header from provider config, the required
        ``anthropic-version`` header, and any ``extra_headers``.
        """
        token = await self._token_getter()
        headers: dict[str, str] = {
            self._auth_config.header: f"{self._auth_config.prefix}{token}",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize an Anthropic response to canonical assistant fields.

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
        usage = _extract_anthropic_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the Anthropic Messages API request payload.

        Extracts system-role messages into the ``system`` field (required by
        the Anthropic API — system messages must not appear in the messages
        array) and assembles model, messages, defaults, and overrides.
        """
        # ``None``-valued caller kwargs mean "not specified" — drop them so they
        # do not clobber provider defaults below. Falsy-but-non-None values
        # (e.g. ``temperature=0.0``) must survive.
        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        system_parts: list[str | list[dict[str, Any]]] = []
        conversation_messages: list[dict[str, Any]] = []

        for message in messages:
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
        _apply_anthropic_tools(payload, request_kwargs)
        reasoning_supported = self._model_reasoning_supported(model_id)
        self._apply_reasoning(
            payload,
            request_kwargs,
            model_id,
            reasoning_supported=reasoning_supported,
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
        # Cache stable prefixes last, after every other payload mutation, so the
        # markers land on the final system/messages that go on the wire.
        _apply_prompt_caching(payload)
        return payload

    def _model_reasoning_supported(self, model_id: str) -> bool | None:
        return model_reasoning_supported(self._model_lookup, model_id)

    def _model_supports_temperature(self, model_id: str) -> bool:
        """Whether ``model_id`` accepts sampling parameters (temperature/top_p/top_k).

        Reads the discovery-derived ``metadata.anthropic.supports_temperature``
        flag through the injected catalog lookup. Defaults to ``True`` (don't
        drop) when there is no lookup, no model, or no flag — the conservative
        "unknown means leave it alone" stance; runtime always wires the lookup.
        """

        if self._model_lookup is None:
            return True
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return True
        provider_metadata = model.metadata.get(ANTHROPIC_METADATA_KEY)
        if isinstance(provider_metadata, Mapping):
            value = provider_metadata.get(SUPPORTS_TEMPERATURE_METADATA_FIELD)
            if isinstance(value, bool):
                return value
        return True

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
        *,
        reasoning_supported: bool | None,
    ) -> None:
        """Resolve the shared reasoning intent and render it onto the payload.

        A catalog-known non-reasoning model strips every Anthropic thinking
        control and sends nothing. Otherwise the provider-neutral intent
        (:func:`resolve_reasoning_intent`) is rendered into Anthropic's
        ``thinking``/``output_config`` shape — including native ``budget_tokens``
        for a ``budget`` Claude.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        if reasoning_supported is False:
            remove_reasoning_kwargs(request_kwargs, *ANTHROPIC_REASONING_PARAMETER_NAMES)
            return
        max_tokens = self._resolve_max_tokens(request_kwargs)
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
            payload["thinking"] = {"type": "disabled"}

    def _resolve_max_tokens(self, request_kwargs: dict[str, Any]) -> int | None:
        """Return the effective ``max_tokens`` (caller kwarg, else provider default).

        Used to keep a thinking ``budget_tokens`` strictly under the output
        allowance — caller value wins over the provider default, mirroring payload
        assembly precedence.
        """

        value = request_kwargs.get("max_tokens")
        if value is None and self._config.defaults:
            value = self._config.defaults.get("max_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

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

        Retries on retryable errors (429, 502, 503, 529) via
        ``retry_async``.  Fails immediately on auth errors (401/403).

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

        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            payload = self._build_payload(messages, model_id, **kwargs)
            try:
                response = await self._client.post(
                    MESSAGES_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            detail = self._build_error_detail(response.status_code, response.text)
            classify_http_status(
                response.status_code,
                extra_retryable={_HTTP_OVERLOADED},
                detail=detail,
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "Anthropic provider"))

        return await retry_async(_do_request)

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

        Retries the initial connection on retryable errors (429, 502, 503,
        529).  Once the stream is established, yields parsed SSE data
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
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True

        async def _connect_stream() -> httpx.Response:
            # Rebuild headers per attempt: an OAuth token may refresh during a
            # retry backoff, and the getter must be re-consulted each time.
            headers = await self._build_headers()
            request = self._client.build_request(
                "POST",
                MESSAGES_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            # If the status indicates an error, read and close the response
            # before classifying — this frees the connection for retry.
            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                detail = self._build_error_detail(response.status_code, error_body)
                classify_http_status(
                    response.status_code,
                    extra_retryable={_HTTP_OVERLOADED},
                    detail=detail,
                    response_headers=response.headers,
                )
                # classify_http_status always raises for >= 400; this is unreachable
                # but satisfies type checkers.
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        response = await retry_async(_connect_stream)

        content_blocks_by_index: dict[int, dict[str, Any]] = {}
        reasoning_meta_blocks: list[dict[str, Any]] = []
        _usage_from_start: dict[str, Any] | None = None
        seen_message_stop = False

        try:
            async for data in iter_sse_data(response):
                if not data.strip():
                    continue
                parsed = parse_sse_json_data(data, context="Anthropic provider")
                if not isinstance(parsed, dict):
                    continue
                for normalized_delta in _normalize_anthropic_stream_event(
                    parsed,
                    content_blocks_by_index,
                    reasoning_meta_blocks,
                ):
                    yield normalized_delta
                event_type = parsed.get("type")
                # Accumulate input-side usage from message_start for later usage delta.
                if event_type == "message_start":
                    message = parsed.get("message", {})
                    if isinstance(message, dict):
                        usage = message.get("usage", {})
                        if isinstance(usage, dict):
                            input_tokens = usage.get("input_tokens")
                            if isinstance(input_tokens, int):
                                _usage_from_start = {"input_tokens": input_tokens}
                                apply_anthropic_cache_usage(_usage_from_start, usage)
                # Yield the usage delta once output tokens arrive. If message_start
                # carried no usable input count, degrade to input_tokens=0 rather
                # than dropping the run's token accounting entirely.
                elif event_type == "message_delta":
                    delta_usage = parsed.get("usage", {})
                    if isinstance(delta_usage, dict):
                        output_tokens = delta_usage.get("output_tokens")
                        if isinstance(output_tokens, int):
                            yield {
                                "type": "usage",
                                **(_usage_from_start or {"input_tokens": 0}),
                                "output_tokens": output_tokens,
                            }
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


def _anthropic_reasoning_control(
    supported: bool,
    *,
    adaptive: bool,
    enabled: bool,
    effort_levels: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...]]:
    """Map the live thinking capabilities to a vBot reasoning ``(control, levels)``.

    The render the adapter performs decides the mapping (not the lab's marketing):

    * ``adaptive`` supported → ``levels``: the effort path renders adaptive
      thinking + ``output_config.effort`` against the catalog's effort ladder.
    * else ``enabled`` (native budget thinking, no adaptive) → ``budget``: the
      budget path renders native ``thinking: {type: enabled, budget_tokens}``.
      This covers the older models and the hybrids that expose an effort ladder
      but not adaptive thinking (where sending adaptive would 400).
    * supported but neither knob known → no control (effort snaps against the
      adapter floor).
    """

    if not supported:
        return None, ()
    if adaptive:
        return REASONING_CONTROL_LEVELS, effort_levels
    if enabled:
        return REASONING_CONTROL_BUDGET, ()
    return None, ()


def _anthropic_supports_temperature(supported: bool, *, adaptive: bool, enabled: bool) -> bool:
    """Whether the model accepts sampling parameters, from its thinking caps.

    Anthropic removed sampling (temperature/top_p/top_k) on the adaptive-only
    generation (Opus 4.7+, Fable 5) — exactly the models that expose adaptive
    thinking but no native ``enabled`` thinking. Every model that still offers
    ``enabled`` thinking (4.6 and earlier) keeps sampling. Verified live on
    2026-06-22 against ``/v1/messages``; the live ``/models`` caps expose the
    distinction as ``thinking.types.enabled.supported``.
    """

    return not (supported and adaptive and not enabled)


def _read_catalog_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _read_catalog_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _catalog_mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, Mapping) else {}


def _catalog_supported(raw: Mapping[str, Any], key: str) -> bool:
    """Read a capability node's ``supported`` flag (catalog shape ``{key: {supported: bool}}``)."""

    node = raw.get(key)
    return isinstance(node, Mapping) and node.get("supported") is True


def _normalize_anthropic_stream_event(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
    reasoning_meta_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "error":
        error_info = event.get("error", {})
        message = (error_info.get("message") if isinstance(error_info, dict) else None) or str(
            event
        )
        raise ProviderError(f"Provider stream error: {message}", retryable=False)
    if event_type == "content_block_start":
        return _normalize_anthropic_content_block_start(event, content_blocks_by_index)
    if event_type == "content_block_delta":
        return _normalize_anthropic_content_block_delta(event, content_blocks_by_index)
    if event_type == "content_block_stop":
        return _normalize_anthropic_content_block_stop(
            event,
            content_blocks_by_index,
            reasoning_meta_blocks,
        )
    if event_type == "message_delta":
        return _normalize_anthropic_message_delta(event, content_blocks_by_index)
    return []


def _normalize_anthropic_content_block_start(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    content_block = event.get("content_block", {})
    if index is None or not isinstance(content_block, dict):
        return []

    block_type = content_block.get("type")
    block_state: dict[str, Any] = {"type": block_type}
    if block_type == TOOL_USE_BLOCK_TYPE:
        tool_call_id = content_block.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = f"tool_call_{index}"
        name = content_block.get("name")
        block_state["id"] = tool_call_id
        block_state["name"] = name if isinstance(name, str) else ""
        content_blocks_by_index[index] = block_state
        if block_state["name"]:
            return [
                {
                    "type": "tool_call_delta",
                    "id": tool_call_id,
                    "name_delta": block_state["name"],
                    "arguments_delta": "",
                }
            ]
        return []

    if _is_supported_reasoning_block(content_block):
        block_state["block"] = dict(content_block)

    content_blocks_by_index[index] = block_state
    return []


def _normalize_anthropic_content_block_delta(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    delta = event.get("delta", {})
    if index is None or not isinstance(delta, dict):
        return []

    block_state = content_blocks_by_index.get(index, {})
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        text = delta.get("text")
        return [{"type": "content_delta", "text": text}] if isinstance(text, str) and text else []
    if delta_type == "thinking_delta":
        return _normalize_anthropic_thinking_delta(delta, block_state)
    if delta_type == "signature_delta":
        _apply_anthropic_signature_delta(delta, block_state)
        return []
    if delta_type == "input_json_delta":
        return _normalize_anthropic_tool_input_delta(delta, block_state)
    return []


def _normalize_anthropic_content_block_stop(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
    reasoning_meta_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    if index is None:
        return []
    block_state = content_blocks_by_index.get(index, {})
    block = block_state.get("block")
    if not isinstance(block, dict) or not _is_supported_reasoning_block(block):
        return []

    reasoning_meta_blocks.append(dict(block))
    return [
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                REASONING_META_CONTENT_BLOCKS: [
                    dict(meta_block) for meta_block in reasoning_meta_blocks
                ]
            },
        }
    ]


def _normalize_anthropic_message_delta(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    delta = event.get("delta", {})
    if not isinstance(delta, dict):
        return []
    stop_reason = delta.get("stop_reason")
    if stop_reason is None:
        return []
    return [
        {
            "type": "finish",
            "reason": _normalize_anthropic_stop_reason(
                stop_reason,
                has_tool_calls=_has_anthropic_stream_tool_calls(content_blocks_by_index),
            ),
        }
    ]


def _normalize_anthropic_thinking_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> list[dict[str, Any]]:
    thinking = delta.get("thinking")
    if not isinstance(thinking, str) or not thinking:
        return []
    block = block_state.get("block")
    if isinstance(block, dict):
        block["thinking"] = f"{block.get('thinking', '')}{thinking}"
    return [{"type": "reasoning_delta", "text": thinking}]


def _apply_anthropic_signature_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> None:
    signature = delta.get("signature")
    block = block_state.get("block")
    if isinstance(signature, str) and signature and isinstance(block, dict):
        block["signature"] = signature


def _normalize_anthropic_tool_input_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> list[dict[str, Any]]:
    if block_state.get("type") != TOOL_USE_BLOCK_TYPE:
        return []
    arguments_delta = delta.get("partial_json")
    if not isinstance(arguments_delta, str):
        arguments_delta = delta.get("input_delta")
    if not isinstance(arguments_delta, str) or not arguments_delta:
        return []
    tool_call_id = block_state.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return []
    return [
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "name_delta": "",
            "arguments_delta": arguments_delta,
        }
    ]


def _normalize_anthropic_stop_reason(stop_reason: Any, *, has_tool_calls: bool) -> str:
    if stop_reason in ANTHROPIC_TOOL_STOP_REASONS:
        return "tool_calls"
    if stop_reason in ANTHROPIC_STOP_REASONS:
        return "stop"
    return "tool_calls" if has_tool_calls else "stop"


def _anthropic_stream_index(event: dict[str, Any]) -> int | None:
    index = event.get("index")
    return index if isinstance(index, int) else None


def _has_anthropic_stream_tool_calls(
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> bool:
    return any(
        block.get("type") == TOOL_USE_BLOCK_TYPE for block in content_blocks_by_index.values()
    )


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
    *,
    include_thinking_blocks: bool = True,
) -> list[dict[str, Any]]:
    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "tool":
            pending_tool_results.append(_to_anthropic_tool_result_block(message))
            continue

        if pending_tool_results:
            anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))
            pending_tool_results = []
        anthropic_message = _to_anthropic_message(
            message,
            include_thinking_blocks=include_thinking_blocks,
        )
        if anthropic_message is not None:
            anthropic_messages.append(anthropic_message)

    if pending_tool_results:
        anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))

    return anthropic_messages


def _merge_anthropic_system_parts(
    parts: list[str | list[dict[str, Any]]],
) -> str | list[dict[str, Any]] | None:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if all(isinstance(part, str) for part in parts):
        return "\n\n".join(part for part in parts if isinstance(part, str))

    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        blocks.extend(
            dict(block) if isinstance(block, dict) else _text_block(block) for block in part
        )
    return blocks


def _to_anthropic_message(
    message: dict[str, Any],
    *,
    include_thinking_blocks: bool = True,
) -> dict[str, Any] | None:
    role = message.get("role")
    if role == "tool":
        return _to_anthropic_tool_result_message([_to_anthropic_tool_result_block(message)])
    if role == "assistant":
        content_blocks = _to_anthropic_assistant_content(
            message,
            include_thinking_blocks=include_thinking_blocks,
        )
        # The wire rejects empty content arrays — a replayed reasoning-only
        # turn whose thinking blocks were stripped has nothing left to send.
        if not content_blocks:
            return None
        return {
            "role": "assistant",
            "content": content_blocks,
        }
    if role == "user":
        return {
            "role": "user",
            "content": _to_anthropic_user_content(message.get("content", "")),
        }
    return {
        "role": role,
        "content": _to_anthropic_text_content(message.get("content", "")),
    }


def _to_anthropic_tool_result_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": blocks}


def _to_anthropic_tool_result_block(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message["tool_call_id"],
        "content": message["content"],
    }


def _to_anthropic_user_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return _to_anthropic_text_content(content)

    return [_to_anthropic_user_content_block(block) for block in content]


def _to_anthropic_user_content_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"type": "text", "text": str(block)}

    block_type = block.get("type")
    if block_type == "media":
        base64_data = block.get("base64")
        media_type = block.get("media_type")
        if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
            raise ProviderError(
                "media content block requires string base64 and media_type fields",
                retryable=False,
            )
        if not media_type.startswith("image/"):
            raise ProviderError(
                f"Anthropic adapter supports only image media blocks; received {media_type}",
                retryable=False,
            )
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_data,
            },
        }
    if block_type == "document":
        return _to_anthropic_document_block(block)
    if block_type == "text":
        text = block.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}

    return dict(block)


def _to_anthropic_document_block(block: dict[str, Any]) -> dict[str, Any]:
    """Translate a canonical document block into an Anthropic ``document`` block.

    Wire shape verified against the Anthropic Messages API (base64 source).
    """
    base64_data = block.get("base64")
    media_type = block.get("media_type")
    if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
        raise ProviderError(
            "document content block requires string base64 and media_type fields",
            retryable=False,
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64_data,
        },
    }


def _to_anthropic_text_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return content
    return [_text_block(content)]


def _text_block(content: Any) -> dict[str, Any]:
    return {"type": "text", "text": "" if content is None else str(content)}


def _to_anthropic_assistant_content(
    message: dict[str, Any],
    *,
    include_thinking_blocks: bool = True,
) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    if include_thinking_blocks:
        content_blocks.extend(_reasoning_blocks_from_meta(message.get("reasoning_meta")))

    content = message.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        content_blocks.extend(content)

    for tool_call in message.get("tool_calls") or []:
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tool_call["id"],
                "name": tool_call["name"],
                "input": tool_call.get("arguments", {}),
            }
        )
    return content_blocks


def _reasoning_blocks_from_meta(reasoning_meta: Any) -> list[dict[str, Any]]:
    if not isinstance(reasoning_meta, dict):
        return []
    blocks = reasoning_meta.get(REASONING_META_CONTENT_BLOCKS)
    if not isinstance(blocks, list):
        return []
    return [dict(block) for block in blocks if _is_supported_reasoning_block(block)]


def _is_supported_reasoning_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return block.get("type") in (THINKING_BLOCK_TYPE, REDACTED_THINKING_BLOCK_TYPE)


def _apply_anthropic_tools(payload: dict[str, Any], kwargs: dict[str, Any]) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    payload["tools"] = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        for tool in tools
    ]


def _apply_prompt_caching(payload: dict[str, Any]) -> None:
    """Place ``cache_control`` breakpoints so Anthropic caches stable prefixes.

    One marker on the last system block caches tools + system together; up to
    :data:`MAX_HISTORY_CACHE_BREAKPOINTS` markers on the most recent message
    boundaries cache the growing conversation tail (the dominant cost at large
    context), giving the next request several read-points within the 20-block
    cache lookback. The total never exceeds :data:`CACHE_BREAKPOINT_LIMIT`.
    """

    remaining = CACHE_BREAKPOINT_LIMIT
    cached_system = _system_with_cache_control(payload.get("system"))
    if cached_system is not None:
        payload["system"] = cached_system
        remaining -= 1

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    history_budget = min(remaining, MAX_HISTORY_CACHE_BREAKPOINTS)
    marked = 0
    for index in range(len(messages) - 1, -1, -1):
        if marked >= history_budget:
            break
        message = messages[index]
        if not isinstance(message, dict):
            continue
        cached_message = _message_with_cache_control(message)
        if cached_message is not None:
            messages[index] = cached_message
            marked += 1


def _system_with_cache_control(system: Any) -> list[dict[str, Any]] | None:
    """Return the system field in block form with ``cache_control`` on its last
    cacheable block, or ``None`` when there is nothing to cache."""

    if isinstance(system, str):
        if not system.strip():
            return None
        return [_cached_text_block(system)]
    if isinstance(system, list) and system:
        return _blocks_with_cache_control(system)
    return None


def _message_with_cache_control(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of ``message`` with ``cache_control`` on its last cacheable
    content block, or ``None`` when the message has nothing to cache."""

    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return None
        marked = dict(message)
        marked["content"] = [_cached_text_block(content)]
        return marked
    if isinstance(content, list) and content:
        blocks = _blocks_with_cache_control(content)
        if blocks is None:
            return None
        marked = dict(message)
        marked["content"] = blocks
        return marked
    return None


def _blocks_with_cache_control(blocks: list[Any]) -> list[dict[str, Any]] | None:
    """Copy ``blocks`` and add ``cache_control`` to the last cacheable block.

    Returns ``None`` when no block can carry the marker (every block is a
    reasoning block, see :data:`CACHE_UNMARKABLE_BLOCK_TYPES`)."""

    copied = [dict(block) if isinstance(block, dict) else block for block in blocks]
    for index in range(len(copied) - 1, -1, -1):
        block = copied[index]
        if isinstance(block, dict) and block.get("type") not in CACHE_UNMARKABLE_BLOCK_TYPES:
            block["cache_control"] = dict(CACHE_CONTROL_EPHEMERAL)
            return copied
    return None


def _cached_text_block(text: str) -> dict[str, Any]:
    return {
        "type": TEXT_BLOCK_TYPE,
        "text": text,
        "cache_control": dict(CACHE_CONTROL_EPHEMERAL),
    }


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


def _extract_anthropic_text(content_blocks: Any) -> str | None:
    text_parts = [
        block["text"] for block in _content_blocks(content_blocks) if block.get("type") == "text"
    ]
    return "".join(text_parts) if text_parts else None


def _extract_anthropic_reasoning(content_blocks: Any) -> str | None:
    reasoning_parts = [
        block["thinking"]
        for block in _content_blocks(content_blocks)
        if block.get("type") == THINKING_BLOCK_TYPE and isinstance(block.get("thinking"), str)
    ]
    return "".join(reasoning_parts) if reasoning_parts else None


def _extract_anthropic_reasoning_meta(content_blocks: Any) -> dict[str, Any] | None:
    reasoning_blocks = [
        dict(block)
        for block in _content_blocks(content_blocks)
        if _is_supported_reasoning_block(block)
    ]
    if not reasoning_blocks:
        return None
    return {REASONING_META_CONTENT_BLOCKS: reasoning_blocks}


def _extract_anthropic_tool_calls(content_blocks: Any) -> list[dict[str, Any]] | None:
    tool_calls = [
        {
            "id": block["id"],
            "name": block["name"],
            "arguments": block.get("input", {}),
        }
        for block in _content_blocks(content_blocks)
        if block.get("type") == "tool_use"
    ]
    return tool_calls or None


def _extract_anthropic_usage(response: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an Anthropic response.

    Returns ``{"input_tokens": N, "output_tokens": N}`` when usage data
    is available, defaulting ``output_tokens`` to ``0`` when only
    ``input_tokens`` is provided.  Returns ``None`` when usage data is
    absent or incomplete.
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        return None
    output_tokens = usage.get("output_tokens")
    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens if output_tokens is not None else 0,
    }
    apply_anthropic_cache_usage(normalized, usage)
    return normalized


def apply_anthropic_cache_usage(normalized: dict[str, Any], usage: dict[str, Any]) -> None:
    """Fold Anthropic cache token counts into canonical usage fields.

    Anthropic reports ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` separately from ``input_tokens``;
    canonical ``input_tokens`` means the total prompt including cached
    tokens, so both counts are added on top.
    """
    cache_read = usage.get("cache_read_input_tokens")
    cache_write = usage.get("cache_creation_input_tokens")
    input_tokens = normalized["input_tokens"]
    if isinstance(cache_read, int) and isinstance(input_tokens, int):
        normalized["cache_read_tokens"] = cache_read
        input_tokens += cache_read
    if isinstance(cache_write, int) and isinstance(input_tokens, int):
        normalized["cache_write_tokens"] = cache_write
        input_tokens += cache_write
    normalized["input_tokens"] = input_tokens


def _content_blocks(content_blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(content_blocks, list):
        return []
    return [block for block in content_blocks if isinstance(block, dict)]
