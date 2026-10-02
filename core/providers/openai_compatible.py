"""OpenAI-compatible provider adapter.

Handles the ``/chat/completions`` endpoint format used by OpenAI, Groq,
Together, and other providers that follow the OpenAI API convention.
Differences in base URL, auth headers, and default parameters are expressed
through ``ProviderConfig``. Providers that are mostly OpenAI-compatible but need
provider-specific behavior can subclass this adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self, override

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._chat_completions_catalog import (
    _has_image_modality,
    _positive_int,
    _read_first_optional_int,
    _read_first_optional_string_tuple,
    _read_non_empty_string,
    _read_optional_mapping,
    _read_optional_non_empty_string,
    _read_optional_string_set,
    _supports_json_mode,
    _supports_reasoning,
    _supports_tools_by_default,
)
from core.providers._chat_completions_constants import (
    _OPENAI_STREAM_REASONING_FIELD_SEEN_STATE_KEY,
    _OPENAI_STREAM_REASONING_FIELDS_STATE_KEY,
    CHAT_COMPLETIONS_ENDPOINT,
    CONTEXT_WINDOW_KEYS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    JSON_MODE_PARAMETER_NAMES,
    MAX_OUTPUT_TOKEN_KEYS,
    OPENAI_ERROR_FINISH_REASONS,
    OPENAI_NATIVE_TRANSPORT_FINISH_REASONS,
    OPENAI_REASONING_EFFORTS,
    OPENAI_REASONING_KEYS,
    OPENAI_REASONING_META_KEYS,
    OPENAI_TOOL_FINISH_REASONS,
    OUTPUT_LIMIT_PARAMETER_NAMES,
    REASONING_PARAMETER_NAMES,
    REASONING_RESPONSE_FIELD_METADATA_KEY,
    SSE_DONE_MARKER,
)
from core.providers._chat_completions_stream import (
    _normalize_openai_stream_chunk,
)
from core.providers._chat_completions_wire import (
    _apply_openai_tools,
    _extract_openai_reasoning,
    _extract_openai_reasoning_meta,
    _extract_openai_terminal_outcome,
    _extract_openai_tool_calls,
    _extract_openai_usage,
    _find_openai_reasoning,
    _first_choice_message,
    _merge_stream_usage_options,
    _openai_response_carries_reasoning,
    _openai_response_message,
    _selected_thinking_effort,
    _to_openai_assistant_message,
    _to_openai_message,
    _to_openai_user_content_part,
)
from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    format_http_error_detail,
    iter_sse_events,
    parse_sse_json_data,
    prepare_json_body,
    wrap_network_error,
)
from core.providers._tool_calls import WIRE_TOOL_CALL_ID_PROFILES, normalize_tool_call_ids
from core.providers._wire_learning import execute_learning_from_rejections
from core.providers._wire_profile_files import thaw_json
from core.providers.adapter import (
    ModelLookup,
    ProviderAdapter,
    project_tool_result_content_fallbacks,
    resolve_request_input_budget,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
    ProviderRequestTooLargeError,
)
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
    REASONING_REPLAY_FIDELITY_META_ONLY,
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
    ReasoningIntent,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_supported,
    remove_reasoning_kwargs,
    resolve_reasoning_intent,
    warn_effort_swallowed,
    warn_rejected_effort,
)
from core.providers.reasoning_dialects import (
    describe_reasoning,
    dialect_request_fields,
    render_reasoning,
)
from core.providers.token_getter import OAuthRequestRecovery, StaticTokenGetter, TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.providers.wire_profile import ReasoningWire
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.tokens import (
    estimate_structured_tokens,
)

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "CHAT_COMPLETIONS_ENDPOINT",
    "CONTEXT_WINDOW_KEYS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "JSON_MODE_PARAMETER_NAMES",
    "MAX_OUTPUT_TOKEN_KEYS",
    "OPENAI_ERROR_FINISH_REASONS",
    "OPENAI_NATIVE_TRANSPORT_FINISH_REASONS",
    "OPENAI_REASONING_EFFORTS",
    "OPENAI_REASONING_KEYS",
    "OPENAI_REASONING_META_KEYS",
    "OPENAI_TOOL_FINISH_REASONS",
    "OUTPUT_LIMIT_PARAMETER_NAMES",
    "OpenAICompatibleAdapter",
    "REASONING_PARAMETER_NAMES",
    "REASONING_RESPONSE_FIELD_METADATA_KEY",
    "SSE_DONE_MARKER",
]

_LOGGER = get_logger("providers.openai_compatible")

_PROFILE_DESCRIBED_PROTOCOLS = frozenset({"chat_completions", "messages"})
"""Protocols whose codecs render reasoning only from the wire profile's plan."""


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

    DESCRIBES_REASONING_FROM_PROFILE: ClassVar[bool] = True
    """Whether Chat Completions (and routed Messages) requests carry exactly the profile's plan.

    True when reasoning reaches the wire only through :meth:`_apply_reasoning`
    (the profile's plan in its dialect), so :meth:`describe_reasoning_render`
    describes that plan. A subclass that still spells reasoning itself (Tool
    toggles, other endpoints chosen per Model) sets it to False and keeps the
    declared-control description until its wire is profile-driven.
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
        super().__init__(
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            reasoning_replay_default=config.reasoning_replay,
        )
        self._client = build_async_client(
            base_url=base_url or config.base_url,
            debug_recorder=debug_recorder,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @override
    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

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

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize an OpenAI-compatible response to canonical assistant fields.

        Readable reasoning is read from the wire profile's
        ``response.reasoning_fields`` in priority order.
        """
        message = _first_choice_message(response)
        content = message.get("content")
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": content if isinstance(content, str) or content is None else str(content),
            "reasoning": _extract_openai_reasoning(
                message, fields=self._reasoning_fields(model_id)
            ),
            "reasoning_meta": _extract_openai_reasoning_meta(message),
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

    def _reasoning_fields(self, model_id: str | None) -> tuple[str, ...]:
        """The readable reasoning fields of ``model_id``'s wire, in priority order."""

        if not model_id:
            return OPENAI_REASONING_KEYS
        return self.wire_profile(model_id).response.reasoning_fields

    def _format_assistant_message(
        self,
        message: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Convert an internal assistant message to its wire representation.

        Serialization carries exactly one reasoning class per the adapter's
        declared :meth:`reasoning_replay_fidelity`: opaque meta when the turn
        captured it and the declaration allows meta, otherwise the readable
        text — never both. The readable text rides in the wire profile's
        ``replay.history_field``; a wire without one never receives readable
        reasoning.
        """
        wire = _to_openai_assistant_message(message)
        fidelity = self.reasoning_replay_fidelity(model_id or "")
        carrier = self.wire_profile(model_id or "").replay.history_field
        if fidelity == REASONING_REPLAY_FIDELITY_META_ONLY or carrier is None:
            # Strict/block-shaped wires never take a top-level readable field.
            return wire

        reasoning = message.get("reasoning")
        readable = isinstance(reasoning, str) and bool(reasoning)
        if fidelity == REASONING_REPLAY_FIDELITY_READABLE_ONLY:
            # Wires with no meta class; stray meta keys are stripped defensively.
            for key in OPENAI_REASONING_META_KEYS:
                wire.pop(key, None)
            if readable:
                wire[carrier] = reasoning
            return wire

        # ``meta_preferred``: meta supersedes duplicated plaintext (OpenRouter's
        # documented contract); readable text stays the lossless fallback only
        # when no meta was captured.
        has_meta = any(key in wire for key in OPENAI_REASONING_META_KEYS)
        if has_meta or not readable:
            return wire
        wire[carrier] = reasoning
        return wire

    def _format_message(
        self,
        message: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Convert one internal message to its wire representation."""
        if message.get("role") == "assistant":
            return self._format_assistant_message(message, model_id=model_id)
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            return {
                "role": "user",
                "content": [self._format_user_content_part(part) for part in message["content"]],
            }
        return _to_openai_message(message)

    def _format_user_content_part(self, part: Any) -> dict[str, Any]:
        """Encode one canonical user content part for this provider's wire."""
        return _to_openai_user_content_part(part)

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Count the rendered Chat request, including only its selected reasoning class."""
        projected = project_tool_result_content_fallbacks([dict(message) for message in messages])
        wire = [self._format_message(message, model_id=model_id) for message in projected]
        tokens, _ = estimate_structured_tokens(wire, model_id=model_id)
        if tools:
            tool_tokens, _ = estimate_structured_tokens(
                render_tool_definitions(
                    list(tools), profile=self.wire_profile(model_id).request.tool_schema
                ),
                model_id=model_id,
            )
            tokens += tool_tokens
        return tokens

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
        self._refuse_unadmitted_model(model_id)
        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        profile = self.wire_profile(model_id)
        rules = profile.request
        id_profile = WIRE_TOOL_CALL_ID_PROFILES.get(rules.tool_call_ids)
        if id_profile is not None:
            messages = normalize_tool_call_ids(messages, id_profile)
        selected_effort = _selected_thinking_effort(request_kwargs)
        self._apply_model_output_limit(request_kwargs, model_id, messages)
        projected_messages = project_tool_result_content_fallbacks(messages)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [
                self._format_message(message, model_id=model_id) for message in projected_messages
            ],
        }
        _apply_openai_tools(payload, request_kwargs, profile=rules.tool_schema)
        unrendered = set(payload)
        self._apply_reasoning(payload, request_kwargs, model_id)
        rendered = set(payload) - unrendered
        if rules.parameters:
            # Shape caller values first so a dropped caller value never
            # displaces a field the reasoning dialect rendered.
            rules.shape_parameters(
                request_kwargs, reasoning_active=False, provider_label=self._config.name
            )
        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                if key == "max_tokens" and any(
                    alias in request_kwargs
                    for alias in ("max_completion_tokens", "max_output_tokens")
                ):
                    continue
                payload.setdefault(key, value)
        for key, value in rules.body_defaults.items():
            payload.setdefault(key, thaw_json(value))
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        for key, value in rules.extra_body.items():
            payload[key] = thaw_json(value)
        if rules.output_limit_collapse and rules.output_limit_field is not None:
            _collapse_output_limit(payload, rules.output_limit_field)
        described = self._describe_reasoning(model_id, selected_effort or None)
        if rules.parameters:
            rules.shape_parameters(
                payload,
                reasoning_active=described.requests_reasoning,
                protected=rendered - set(request_kwargs) - set(rules.extra_body),
                provider_label=self._config.name,
            )
        replay = profile.replay
        if (
            replay.strip_when_off
            and replay.history_field is not None
            and (profile.reasoning.supported is False or described.kind == REASONING_INTENT_OFF)
        ):
            # Reasoning is off for this request: earlier turns' readable
            # reasoning must not reach the wire either.
            for message in payload["messages"]:
                if isinstance(message, dict):
                    message.pop(replay.history_field, None)
        return payload

    def _apply_model_output_limit(
        self,
        request_kwargs: dict[str, Any],
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        estimated_input_tokens: int | Callable[[], int] | None = None,
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

        Subclasses whose wire format differs from the chat-message shape (e.g.
        stateless Responses items) pass a wire-accurate ``estimated_input_tokens``
        fallback callable so the context clamp budgets against what the Provider
        actually receives without re-estimating a matching scoped Chat budget.
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

        def estimate_input() -> int:
            if estimated_input_tokens is None:
                return self.estimate_request_input_tokens(
                    messages,
                    model_id=model_id,
                    tools=tool_definitions,
                )
            value = (
                estimated_input_tokens()
                if callable(estimated_input_tokens)
                else estimated_input_tokens
            )
            return max(0, int(value))

        rules = self.wire_profile(model_id).request
        estimated_input = resolve_request_input_budget(model_id, estimate_input)
        resolved = resolve_request_output_limit(
            explicit_limit=explicit_limit,
            model_output_limit=self._model_max_output_tokens(model_id),
            provider_default=rules.output_limit_default
            or (self._config.defaults.get("max_tokens") if self._config.defaults else None),
            effective_context_window=resolve_context_window(
                self._model_context_window(model_id), self._config
            ),
            estimated_input_tokens=estimated_input,
        )
        if resolved is None:
            return
        if rules.output_limit_cap is not None:
            resolved = min(resolved, rules.output_limit_cap)
        if explicit_values:
            for key in OUTPUT_LIMIT_PARAMETER_NAMES:
                if _positive_int(request_kwargs.get(key)) is not None:
                    request_kwargs[key] = min(int(request_kwargs[key]), resolved)
            return
        if rules.output_limit_field is not None:
            request_kwargs[rules.output_limit_field] = resolved

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

    def _describe_reasoning(self, model_id: str, effort: str | None) -> ReasoningIntent:
        """The reasoning decision a request with ``effort`` carries on this wire."""

        wire = self.wire_profile(model_id).reasoning
        return describe_reasoning(wire, wire.plan(effort))

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
    ) -> None:
        """Plan the Agent's reasoning effort and render it onto the payload.

        Consumes ``thinking_effort``/``reasoning_effort`` from ``request_kwargs``;
        :meth:`ReasoningWire.plan` decides, :meth:`_render_reasoning` spells the
        decision in the profile's dialect. A Model known not to reason has the
        raw reasoning controls (including the dialect's own fields) stripped
        and gets nothing.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        reasoning_effort = request_kwargs.pop("reasoning_effort", "")
        wire = self.wire_profile(model_id).reasoning
        if wire.supported is False:
            remove_reasoning_kwargs(
                request_kwargs,
                *REASONING_PARAMETER_NAMES,
                *dialect_request_fields(wire.dialect),
            )
            return
        self._render_reasoning(payload, wire.plan(thinking_effort or reasoning_effort), wire=wire)

    def _render_reasoning(
        self,
        payload: dict[str, Any],
        intent: ReasoningIntent,
        *,
        wire: ReasoningWire,
    ) -> None:
        """Spell a planned reasoning intent in the profile's reasoning dialect."""

        render_reasoning(wire, intent, payload)

    @classmethod
    @override
    def describe_reasoning_render(
        cls,
        *,
        model_lookup: ModelLookup | None,
        model_id: str,
        effort: str | None,
        provider_config: ProviderConfig | None = None,
    ) -> ReasoningIntent:
        """Describe the reasoning a request with ``effort`` carries for ``model_id``.

        A Model the catalog marks as non-reasoning reports ``off``. On Chat
        Completions and Messages the Provider's wire profile plans the effort
        exactly like the codec renders it, and its dialect reports what the
        rendered request carries. Other protocols of subclasses (Responses) and
        subclasses that spell reasoning themselves
        (:attr:`DESCRIBES_REASONING_FROM_PROFILE`) keep the declared-control
        description against the profile's ladder, reporting an ``on``/``budget``
        intent as the effort level such a wire sends.
        """

        if model_reasoning_supported(model_lookup, model_id) is False:
            return ReasoningIntent(REASONING_INTENT_OFF)
        profile = cls._standalone_wire_profile(
            model_lookup=model_lookup, provider_config=provider_config, model_id=model_id
        )
        wire = profile.reasoning
        if (
            profile.protocol in _PROFILE_DESCRIBED_PROTOCOLS
            and cls.DESCRIBES_REASONING_FROM_PROFILE
        ):
            return describe_reasoning(wire, wire.plan(effort))
        intent = resolve_reasoning_intent(
            supported=model_reasoning_supported(model_lookup, model_id),
            control=model_reasoning_control(model_lookup, model_id),
            levels=wire.ladder or tuple(OPENAI_REASONING_EFFORTS),
            effort=effort,
            budget_max=model_reasoning_budget_max(model_lookup, model_id),
            max_tokens=None,
        )
        if (
            intent.kind in (REASONING_INTENT_BUDGET, REASONING_INTENT_ON)
            and intent.effort_level is not None
        ):
            return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=intent.effort_level)
        return intent

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

    @override
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

        request_headers = self._request_headers_from_kwargs(kwargs)
        request_headers.update(self.wire_profile(model_id).request.extra_headers)
        # Capture the agent-selected effort before ``_build_payload`` consumes the
        # reasoning kwargs, so the observability signals below can name it.
        selected_effort = _selected_thinking_effort(kwargs)
        reasoning_fields = self._reasoning_fields(model_id)

        def describe() -> ReasoningIntent:
            # The swallowed-effort signal judges what this request actually
            # renders (catalog support, ladder snapping, dialect), not the raw
            # selection: a catalog non-reasoning Model has its effort stripped.
            return self._describe_reasoning(model_id, selected_effort or None)

        payload = self._build_payload(messages, model_id, **kwargs)

        auth_recovery = OAuthRequestRecovery(self._token_getter, self._auth_config)

        async def _do_request(body: bytes) -> dict[str, Any]:
            headers = httpx.Headers(await self._build_request_headers(messages, payload))
            headers.update(request_headers)
            headers.setdefault("Content-Type", "application/json")
            try:
                response = await self._client.post(
                    CHAT_COMPLETIONS_ENDPOINT,
                    content=body,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc

            auth_recovery.record_response(
                response.status_code, headers, response.text if response.status_code >= 400 else ""
            )
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
            returned_reasoning = _openai_response_carries_reasoning(parsed, fields=reasoning_fields)
            message = _openai_response_message(parsed)
            found = _find_openai_reasoning(message, reasoning_fields) if message else None
            if found is not None:
                self.wire.observe_reasoning_field(model_id, found[0])
            elif returned_reasoning:
                self.wire.observe_reasoning_returned(model_id)
            # Requested reasoning that comes back with 0 reasoning tokens and no
            # returned Reasoning was effectively swallowed — surface it.
            warn_effort_swallowed(
                rendered=describe(),
                usage=parsed.get("usage"),
                returned_reasoning=returned_reasoning,
                model_id=model_id,
                provider_logger=_LOGGER,
            )
            return parsed

        async def _execute_payload() -> dict[str, Any]:
            # Sampling fallback changes the payload; ordinary/OAuth retries do
            # not. Prepare once for each revision and reuse its exact bytes.
            body = await self._prepare_request_body(payload, model_id)
            return await auth_recovery.run(lambda: retry_async(lambda: _do_request(body)))

        return await execute_learning_from_rejections(
            _execute_payload,
            payload,
            rebuild=lambda: self._build_payload(messages, model_id, **kwargs),
            sent_effort=lambda: describe().effort_level,
            wire=self.wire,
            model_id=model_id,
            logger=_LOGGER,
            provider_label=self._config.id,
        )

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

    async def _prepare_request_body(self, payload: dict[str, Any], model_id: str) -> bytes:
        """Prepare the exact JSON bytes once and reject a known size overflow."""
        body = await prepare_json_body(payload)
        limit = self.request_body_limit(model_id)
        if limit is not None and len(body) > limit:
            raise ProviderRequestTooLargeError(len(body), limit)
        return body

    @override
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
        request_headers = self._request_headers_from_kwargs(kwargs)
        request_headers.update(self.wire_profile(model_id).request.extra_headers)
        selected_effort = _selected_thinking_effort(kwargs)

        def build_stream_payload() -> dict[str, Any]:
            built = self._build_payload(messages, model_id, **kwargs)
            self._prepare_stream_payload(built)
            return built

        payload = build_stream_payload()
        auth_recovery = OAuthRequestRecovery(self._token_getter, self._auth_config)

        async def _build_headers() -> dict[str, str]:
            headers = await self._build_request_headers(messages, payload)
            headers.update(request_headers)
            return headers

        def _handle_error_status(
            status_code: int,
            error_body: str,
            response_headers: httpx.Headers,
        ) -> None:
            self._classify_http_status(
                status_code,
                detail=format_http_error_detail(status_code, error_body),
                response_headers=response_headers,
            )

        async def _connect_payload() -> httpx.Response:
            body = await self._prepare_request_body(payload, model_id)
            return await connect_streaming_with_retry(
                self._client,
                CHAT_COMPLETIONS_ENDPOINT,
                body,
                build_headers=_build_headers,
                handle_error_status=_handle_error_status,
                auth_recovery=auth_recovery,
                wrap_transport_error=self._wrap_transport_error,
            )

        response = await execute_learning_from_rejections(
            _connect_payload,
            payload,
            rebuild=build_stream_payload,
            sent_effort=lambda: (
                self._describe_reasoning(model_id, selected_effort or None).effort_level
            ),
            wire=self.wire,
            model_id=model_id,
            logger=_LOGGER,
            provider_label=self._config.id,
        )

        tool_call_slots: set[int] = set()
        normalization_state: dict[str, Any] = {
            _OPENAI_STREAM_REASONING_FIELDS_STATE_KEY: self._reasoning_fields(model_id)
        }
        observed_reasoning_field = False
        seen_done_marker = False

        try:
            async for event in iter_sse_events(response):
                data = event.data
                if data is None:
                    # Every event without data is a transport comment.
                    yield {"type": "heartbeat"}
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
                if not observed_reasoning_field:
                    field = normalization_state.get(_OPENAI_STREAM_REASONING_FIELD_SEEN_STATE_KEY)
                    if isinstance(field, str):
                        self.wire.observe_reasoning_field(model_id, field)
                        observed_reasoning_field = True
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


def _collapse_output_limit(payload: dict[str, Any], field: str) -> None:
    """Send exactly one output-limit field: the smallest positive alias value."""

    limits = [
        value
        for key in OUTPUT_LIMIT_PARAMETER_NAMES
        if (value := _positive_int(payload.pop(key, None))) is not None
    ]
    if limits:
        payload[field] = min(limits)
