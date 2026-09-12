"""OpenAI provider adapter.

Handles the model-selected OpenAI Platform endpoint (``api-key`` connection)
and the ChatGPT Codex ``/codex/responses`` endpoint (``subscription``
connection with ``mode: codex_responses``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import httpx
from websockets.asyncio.client import connect as websocket_connect

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._codex_websocket import (
    CodexWebSocket,
    _CodexWebSocketTransportError,
)
from core.providers._http_shared import (
    PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
    connect_streaming_with_retry,
    format_http_error_detail,
    post_json_with_retry,
    wrap_network_error,
)
from core.providers._openai_constants import (
    _CODEX_CACHE_SCOPE_MAX_LENGTH,
    _CODEX_STABLE_VERSION_PATTERN,
    _CODEX_TRANSPORT_AUTO,
    _CODEX_TRANSPORT_SSE,
    _NORMALIZED_CODEX_STREAM_RESPONSE_KEY,
    CODEX_CACHE_SCOPE_HEADERS,
    CODEX_CLIENT_VERSION_FALLBACK,
    CODEX_EXTRA_HEADERS,
    CODEX_PACKAGE_METADATA_URL,
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
    CODEX_WEBSOCKET_BETA,
    CONVERSATION_ID_KWARG,
    DISCOVERY_JSON_PARAMETER_NAMES,
    DISCOVERY_REASONING_PARAMETER_NAMES,
    DISCOVERY_TOOL_PARAMETER_NAMES,
    OPENAI_API_KEY_WIRE_KEY,
    OPENAI_METADATA_KEY,
    OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS,
    OPENAI_REASONING_CONTEXTS,
    OPENAI_RESPONSES_PROTOCOL,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OPENAI_SUBSCRIPTION_REASONING_EFFORTS,
    OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS,
    OPENAI_SUBSCRIPTION_WIRE_KEY,
    OPENAI_WIRE_POLICIES_KEY,
    OPTIONAL_REQUEST_PARAMETER_NAMES,
    PROMPT_CACHE_AFFINITY_ID_KWARG,
    REASONING_PARAMETER_NAMES,
    RESPONSES_POLICY_ENDPOINT,
    STRUCTURED_OUTPUT_PARAMETER_NAMES,
    TOOL_PARAMETER_NAMES,
    CodexTransport,
    CodexWebSocketConnector,
    CodexWebSocketRoute,
)
from core.providers._openai_policy import (
    OpenAISubscriptionResponsesPolicy,
    _normalize_catalog_raw,
    _optional_mapping,
    _optional_string,
    _string_set,
    _subscription_capability_supported,
    _subscription_supported_parameters,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ModelLookup
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    estimate_responses_input_tokens,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_INTENT_EFFORT,
    ReasoningIntent,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.providers.token_getter import OAuthRequestRecovery, TokenGetter

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "CODEX_CACHE_SCOPE_HEADERS",
    "CODEX_CLIENT_VERSION_FALLBACK",
    "CODEX_EXTRA_HEADERS",
    "CODEX_PACKAGE_METADATA_URL",
    "CODEX_RESPONSES_ENDPOINT",
    "CODEX_RESPONSES_MODE",
    "CODEX_WEBSOCKET_BETA",
    "CONVERSATION_ID_KWARG",
    "CodexTransport",
    "CodexWebSocketConnector",
    "CodexWebSocketRoute",
    "DISCOVERY_JSON_PARAMETER_NAMES",
    "DISCOVERY_REASONING_PARAMETER_NAMES",
    "DISCOVERY_TOOL_PARAMETER_NAMES",
    "OPENAI_API_KEY_WIRE_KEY",
    "OPENAI_METADATA_KEY",
    "OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS",
    "OPENAI_REASONING_CONTEXTS",
    "OPENAI_RESPONSES_PROTOCOL",
    "OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS",
    "OPENAI_SUBSCRIPTION_REASONING_EFFORTS",
    "OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS",
    "OPENAI_SUBSCRIPTION_WIRE_KEY",
    "OPENAI_WIRE_POLICIES_KEY",
    "OPTIONAL_REQUEST_PARAMETER_NAMES",
    "OpenAIAdapter",
    "OpenAISubscriptionResponsesPolicy",
    "PROMPT_CACHE_AFFINITY_ID_KWARG",
    "REASONING_PARAMETER_NAMES",
    "RESPONSES_POLICY_ENDPOINT",
    "STRUCTURED_OUTPUT_PARAMETER_NAMES",
    "TOOL_PARAMETER_NAMES",
]


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for the unified ``openai`` provider.

    The connection's ``mode`` selects the wire variant:

    - ``CODEX_RESPONSES_MODE`` (``"codex_responses"``): Codex Responses API
      (``/codex/responses``) — used by the ``subscription`` connection.
    - ``None`` (default): the Model's ``metadata.openai.wire_policies.api-key``
      selects public ``/responses`` or the inherited ``/chat/completions``
      fallback.
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
        codex_transport: CodexTransport = _CODEX_TRANSPORT_AUTO,
        codex_websocket_connect: CodexWebSocketConnector | None = None,
    ) -> None:
        if codex_transport not in {_CODEX_TRANSPORT_AUTO, _CODEX_TRANSPORT_SSE}:
            raise ValueError(f"unsupported Codex transport: {codex_transport}")
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
        )
        self._codex_transport = codex_transport
        self._codex_websocket_disabled_routes: set[CodexWebSocketRoute] = set()
        self._codex_socket = CodexWebSocket(
            base_url=str(self._client.base_url),
            connect=codex_websocket_connect or cast(CodexWebSocketConnector, websocket_connect),
            debug_recorder=self._debug_recorder,
            response_input=lambda response, model_id: self._build_responses_payload(
                [response], model_id=model_id, stream=True
            ).get("input"),
        )

    async def aclose(self) -> None:
        """Close the cached Codex WebSocket and inherited HTTP client."""
        await self._codex_socket.aclose()
        await super().aclose()

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        credential_value: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Add ChatGPT account routing and Codex headers for ``/codex/models``."""

        account_id = extract_chatgpt_account_id(credential_value)
        if account_id is None:
            raise ProviderAuthError(
                "OpenAI Subscription OAuth token is missing a ChatGPT account id; please reconnect"
            )
        return {**headers, "chatgpt-account-id": account_id, **CODEX_EXTRA_HEADERS}

    @classmethod
    def discovery_params(cls) -> dict[str, str]:
        """Return safe fallback parameters required by ``/codex/models`` discovery."""

        return {"client_version": CODEX_CLIENT_VERSION_FALLBACK}

    @classmethod
    async def resolve_discovery_params(
        cls,
        fetch_json: Callable[[str], Awaitable[Any]],
    ) -> dict[str, str]:
        """Resolve the current stable Codex version used to gate the model catalog."""

        payload = await fetch_json(CODEX_PACKAGE_METADATA_URL)
        if not isinstance(payload, Mapping) or payload.get("name") != "@openai/codex":
            raise ValueError("Codex package metadata has an unexpected shape")
        version = payload.get("version")
        if not isinstance(version, str) or not _CODEX_STABLE_VERSION_PATTERN.fullmatch(version):
            raise ValueError("Codex package metadata has no stable semantic version")
        return {"client_version": version}

    @classmethod
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Exclude Codex catalog entries that OpenAI marks as hidden."""

        del cls
        if getattr(connection, "mode", None) != CODEX_RESPONSES_MODE:
            return True
        return raw.get("visibility") != "hide"

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one OpenAI Subscription ``/codex/models`` entry."""

        normalized_raw = _normalize_catalog_raw(raw)
        base_model = OpenAICompatibleAdapter.normalize_catalog_entry(normalized_raw, defaults)
        capabilities = _optional_mapping(normalized_raw.get("capabilities"))
        supports = _optional_mapping(capabilities.get("supports"))
        raw_parameters = _string_set(normalized_raw.get("supported_parameters"))
        tools_supported = _subscription_capability_supported(
            raw_parameters,
            base_model.capabilities.tools,
            (normalized_raw, capabilities, supports),
            ("supports_tools", "tools", "tool_calls", "function_calling"),
            DISCOVERY_TOOL_PARAMETER_NAMES,
        )
        json_supported = _subscription_capability_supported(
            raw_parameters,
            base_model.capabilities.json_mode or not raw_parameters,
            (normalized_raw, capabilities, supports),
            (
                "supports_json_mode",
                "json_mode",
                "supports_structured_outputs",
                "structured_outputs",
            ),
            DISCOVERY_JSON_PARAMETER_NAMES,
        )
        reasoning_supported = _subscription_capability_supported(
            raw_parameters,
            base_model.capabilities.reasoning.supported or not raw_parameters,
            (normalized_raw, capabilities, supports),
            ("supports_reasoning", "reasoning_supported", "reasoning"),
            DISCOVERY_REASONING_PARAMETER_NAMES,
        )

        return Model(
            model_id=base_model.model_id,
            name=base_model.name,
            capabilities=Capabilities(
                vision=base_model.capabilities.vision,
                tools=tools_supported,
                json_mode=json_supported,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
                input_modalities=base_model.capabilities.input_modalities,
                output_modalities=base_model.capabilities.output_modalities,
                supported_parameters=tuple(
                    _subscription_supported_parameters(
                        raw_parameters,
                        tools_supported,
                        json_supported,
                        reasoning_supported,
                    )
                ),
            ),
            context_window=base_model.context_window,
            max_output_tokens=base_model.max_output_tokens,
            metadata=base_model.metadata,
        )

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Separate Codex transport continuation from prompt-cache routing.

        The ChatGPT Codex backend routes its prompt cache by per-request headers
        (``CODEX_CACHE_SCOPE_HEADERS``), not the body-level ``prompt_cache_key``.
        Cache-compatible forks share ``prompt_cache_affinity_id`` so their exact
        copied prefix reaches the same cache-warm route. ``conversation_id``
        remains unique per vBot Session and scopes the local WebSocket plus
        ``previous_response_id`` continuation. The API-key paths ignore both.
        """
        del project_id
        conversation_id = f"{agent_id}:{session_id}"
        return {
            CONVERSATION_ID_KWARG: conversation_id,
            PROMPT_CACHE_AFFINITY_ID_KWARG: (prompt_cache_affinity_id or conversation_id),
        }

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Wire media depends on the connection's wire variant.

        The Codex Responses wire (``subscription`` connection) carries images
        only; the inherited ``/chat/completions`` wire (``api-key`` connection)
        additionally carries the OpenAI ``input_audio`` formats and, on this
        verified adapter, native ``application/pdf`` documents (Chat Completions
        ``file`` parts).
        """
        if self._connection_mode == CODEX_RESPONSES_MODE:
            return IMAGE_WIRE_MEDIA_TYPES
        if self._uses_platform_responses(model_id):
            return IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"}
        return super().wire_media_support(model_id) | {"application/pdf"}

    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the selected OpenAI wire's rendered request footprint."""

        if self._connection_mode == CODEX_RESPONSES_MODE or self._uses_platform_responses(model_id):
            request_messages = [dict(message) for message in messages]
            if self._connection_mode == CODEX_RESPONSES_MODE and not any(
                message.get("role") == "system" and str(message.get("content") or "").strip()
                for message in request_messages
            ):
                request_messages.insert(
                    0,
                    {"role": "system", "content": OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS},
                )
            return estimate_responses_input_tokens(
                request_messages,
                document_media_types=(
                    frozenset({"application/pdf"})
                    if self._uses_platform_responses(model_id)
                    else frozenset()
                ),
                model_id=model_id,
                tools=tools,
            )
        return super().estimate_request_input_tokens(
            messages,
            model_id=model_id,
            tools=tools,
        )

    async def _build_headers(self, cache_scope_id: str | None = None) -> dict[str, str]:
        if self._connection_mode == CODEX_RESPONSES_MODE:
            return await self._build_codex_headers(cache_scope_id)
        return await super()._build_headers()

    async def _build_codex_headers(self, cache_scope_id: str | None = None) -> dict[str, str]:
        token = await self._token_getter()
        account_id = extract_chatgpt_account_id(token)
        if account_id is None:
            raise ProviderAuthError(
                "OpenAI Subscription OAuth token is missing a ChatGPT account id; please reconnect"
            )
        headers = {
            self._auth_config.header: f"{self._auth_config.prefix}{token}",
            "chatgpt-account-id": account_id,
        }
        # The Codex Responses endpoint owns its required headers; provider-level
        # extra_headers are deliberately not merged here so a stray config entry can
        # never leak onto the Codex wire (the OpenAI provider forbids extra_headers).
        headers.update(CODEX_EXTRA_HEADERS)
        # Pin the prompt cache to the conversation (see CODEX_CACHE_SCOPE_HEADERS).
        if cache_scope_id:
            wire_cache_scope = _clamp_codex_cache_scope(cache_scope_id)
            for header_name in CODEX_CACHE_SCOPE_HEADERS:
                headers[header_name] = wire_cache_scope
        return headers

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return one completed response without exposing stream deltas.

        Routes to the Codex Responses endpoint when ``connection_mode`` is
        ``CODEX_RESPONSES_MODE``. That wire requires ``stream: true``, so the
        adapter consumes its single streaming exchange internally and returns
        the completed Responses object. Session-scoped subscription calls prefer
        the cached WebSocket transport and otherwise use SSE. Other connections
        delegate to the inherited non-streaming ``/chat/completions`` request.
        """

        conversation_id = kwargs.pop(CONVERSATION_ID_KWARG, None)
        prompt_cache_affinity_id = kwargs.pop(
            PROMPT_CACHE_AFFINITY_ID_KWARG,
            conversation_id,
        )
        if self._connection_mode == CODEX_RESPONSES_MODE:
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            state = ResponsesStreamState()
            response_events = cast(
                AsyncGenerator[dict[str, Any], None],
                self._stream_responses(
                    payload,
                    endpoint_path=CODEX_RESPONSES_ENDPOINT,
                    cache_scope_id=prompt_cache_affinity_id,
                    conversation_id=conversation_id,
                    state=state,
                ),
            )
            try:
                while True:
                    try:
                        await asyncio.wait_for(
                            anext(response_events),
                            timeout=PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
                        )
                    except StopAsyncIteration:
                        break
            except TimeoutError as exc:
                raise ProviderTimeoutError(
                    "Non-streaming Provider request timed out waiting for response data"
                ) from exc
            finally:
                await response_events.aclose()
            if state.completed_response is None:
                raise NetworkError("Stream ended without a completed Responses object")
            return {_NORMALIZED_CODEX_STREAM_RESPONSE_KEY: state.normalized_response()}
        if self._uses_platform_responses(model_id):
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                **self._request_kwargs_with_defaults(kwargs),
            )
            return await self._post_json(RESPONSES_POLICY_ENDPOINT, payload)
        return await super().send(messages, model_id=model_id, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a request as normalized vBot deltas.

        Routes to the Codex Responses endpoint when ``connection_mode`` is
        ``CODEX_RESPONSES_MODE``; otherwise delegates to the inherited
        ``/chat/completions`` stream.
        """

        conversation_id = kwargs.pop(CONVERSATION_ID_KWARG, None)
        prompt_cache_affinity_id = kwargs.pop(
            PROMPT_CACHE_AFFINITY_ID_KWARG,
            conversation_id,
        )
        if self._connection_mode == CODEX_RESPONSES_MODE:
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            async for delta in self._stream_responses(
                payload,
                endpoint_path=CODEX_RESPONSES_ENDPOINT,
                cache_scope_id=prompt_cache_affinity_id,
                conversation_id=conversation_id,
            ):
                yield delta
            return
        if self._uses_platform_responses(model_id):
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            async for delta in self._stream_responses(
                payload,
                endpoint_path=RESPONSES_POLICY_ENDPOINT,
            ):
                yield delta
            return
        async for delta in super().stream(messages, model_id=model_id, **kwargs):
            yield delta

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize a provider response to canonical assistant fields."""

        normalized_stream_response = response.get(_NORMALIZED_CODEX_STREAM_RESPONSE_KEY)
        if isinstance(normalized_stream_response, dict):
            return dict(normalized_stream_response)
        if isinstance(response.get("output"), list):
            return normalize_responses_response(response)
        return super().normalize_response(response, model_id=model_id)

    def _request_kwargs_with_defaults(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        # ``None``-valued caller kwargs mean unspecified — drop them before
        # Provider defaults, matching ``OpenAICompatibleAdapter._build_payload``.
        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                request_kwargs.setdefault(key, value)
        # Output-limit defaults are resolved by _apply_model_output_limit in
        # _build_responses_payload, which honors the full precedence chain:
        # explicit caller limit → model ceiling → provider default. Leaving the
        # flat provider default here would make it look like an explicit caller
        # value and skip the model ceiling — the bug that capped Grok-4.5 at
        # 8192 instead of its 500000-token ceiling.
        for key in ("max_tokens", "max_output_tokens"):
            if kwargs.get(key) is None:
                request_kwargs.pop(key, None)
        return request_kwargs

    def _build_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = dict(kwargs)
        document_media_types = (
            frozenset({"application/pdf"})
            if self._uses_platform_responses(model_id)
            else frozenset()
        )
        policy = self._responses_policy_for_model(model_id)
        # Codex rejects output-token fields. Budgeting them locally would abort
        # a still-valid subscription request (the 25% reserve sits at the same
        # 80% line as Compaction, but Compaction never sees this pre-send path).
        if policy.supports_request_parameter("max_tokens") or policy.supports_request_parameter(
            "max_output_tokens"
        ):
            self._apply_model_output_limit(
                request_kwargs,
                model_id,
                messages,
                estimated_input_tokens=self.estimate_request_input_tokens(
                    messages,
                    model_id=model_id,
                    tools=request_kwargs.get("tools"),
                ),
            )
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=policy,
            stream=stream,
            document_media_types=document_media_types,
            **request_kwargs,
        )
        if self._connection_mode == CODEX_RESPONSES_MODE:
            self._ensure_required_instructions(payload)
        self._apply_reasoning_context(payload, model_id)
        payload["store"] = False
        return payload

    def _ensure_required_instructions(self, payload: dict[str, Any]) -> None:
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            return
        payload["instructions"] = OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS

    def _responses_policy_for_model(self, model_id: str) -> OpenAISubscriptionResponsesPolicy:
        model = self._model_lookup(model_id) if self._model_lookup is not None else None
        capabilities = model.capabilities if model is not None else None
        supported_parameters = set(capabilities.supported_parameters) if capabilities else set()
        reasoning_supported = True
        if capabilities is not None:
            reasoning_supported = capabilities.reasoning.supported
        supports_tools = capabilities.tools if capabilities is not None else True
        supports_structured_outputs = capabilities.json_mode if capabilities is not None else True
        return OpenAISubscriptionResponsesPolicy(
            allowed_reasoning_efforts=self._allowed_reasoning_efforts(
                model_id, reasoning_supported
            ),
            supports_tools=supports_tools,
            supports_parallel_tool_calls=(
                supports_tools
                and (
                    not supported_parameters
                    or "parallel_tool_calls" in supported_parameters
                    or "tools" in supported_parameters
                )
            ),
            supports_structured_outputs=supports_structured_outputs,
            minimum_reasoning_effort=_optional_string(
                self._model_wire_policy(model_id).get("minimum_reasoning_effort")
            )
            or None,
            supported_request_parameters=(
                OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS
                if self._connection_mode == CODEX_RESPONSES_MODE
                else OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS
            ),
        )

    def _uses_platform_responses(self, model_id: str) -> bool:
        if self._connection_mode == CODEX_RESPONSES_MODE:
            return False
        return self._model_wire_policy(model_id).get("protocol") == OPENAI_RESPONSES_PROTOCOL

    @classmethod
    def describe_reasoning_render(
        cls,
        *,
        model_lookup: ModelLookup | None,
        model_id: str,
        effort: str | None,
        provider_config: ProviderConfig | None = None,
    ) -> ReasoningIntent:
        model = model_lookup(model_id) if model_lookup is not None else None
        if (
            model is not None
            and model.capabilities.reasoning.supported
            and normalize_thinking_effort(effort) == "none"
        ):
            provider_metadata = _optional_mapping(model.metadata.get(OPENAI_METADATA_KEY))
            policies = _optional_mapping(provider_metadata.get(OPENAI_WIRE_POLICIES_KEY))
            # Without a Connection argument, describe a minimum only when all
            # allowed Connections agree. Do not guess another wire's behavior.
            minima = [
                _optional_mapping(policies.get(connection)).get("minimum_reasoning_effort")
                for connection in model.connections
            ]
            if minima and all(value == minima[0] for value in minima):
                minimum = minima[0]
                if isinstance(minimum, str) and minimum in model.capabilities.reasoning.levels:
                    return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=minimum)
        return super().describe_reasoning_render(
            model_lookup=model_lookup,
            model_id=model_id,
            effort=effort,
            provider_config=provider_config,
        )

    def _model_context_window(self, model_id: str) -> int | None:
        """Resolve the Context window for the active OpenAI Connection."""

        if self._model_lookup is None:
            return None
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return None
        connection_id = (
            OPENAI_SUBSCRIPTION_WIRE_KEY
            if self._connection_mode == CODEX_RESPONSES_MODE
            else OPENAI_API_KEY_WIRE_KEY
        )
        return model.context_window_for(connection_id)

    def _model_wire_policy(self, model_id: str) -> Mapping[str, Any]:
        if self._model_lookup is None:
            return {}
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return {}
        provider_metadata = model.metadata.get(OPENAI_METADATA_KEY)
        if not isinstance(provider_metadata, Mapping):
            return {}
        wire_policies = provider_metadata.get(OPENAI_WIRE_POLICIES_KEY)
        if not isinstance(wire_policies, Mapping):
            return {}
        wire_key = (
            OPENAI_SUBSCRIPTION_WIRE_KEY
            if self._connection_mode == CODEX_RESPONSES_MODE
            else OPENAI_API_KEY_WIRE_KEY
        )
        policy = wire_policies.get(wire_key)
        return policy if isinstance(policy, Mapping) else {}

    def _apply_reasoning_context(
        self,
        payload: dict[str, Any],
        model_id: str,
    ) -> None:
        context = self._model_wire_policy(model_id).get("reasoning_context")
        if context not in OPENAI_REASONING_CONTEXTS:
            return
        reasoning = payload.get("reasoning")
        reasoning_payload = dict(reasoning) if isinstance(reasoning, Mapping) else {}
        reasoning_payload.setdefault("context", context)
        payload["reasoning"] = reasoning_payload

    def _allowed_reasoning_efforts(
        self,
        model_id: str,
        reasoning_supported: bool,
    ) -> frozenset[str]:
        """Return the effort ladder the Responses policy snaps against for a model.

        The effective per-model ladder from the DB wins when present, so a
        subscription model that publishes its own ladder snaps against it. The
        ``OPENAI_SUBSCRIPTION_REASONING_EFFORTS`` constant is only the floor for a
        reasoning model without a feed ladder. A non-reasoning model gets an empty
        set, which suppresses every reasoning control downstream.
        """
        if not reasoning_supported:
            return frozenset()
        ladder = model_reasoning_levels(self._model_lookup, model_id)
        if ladder is not None:
            return frozenset(ladder)
        return OPENAI_SUBSCRIPTION_REASONING_EFFORTS

    def _handle_error_status(
        self,
        status_code: int,
        error_body: str,
        response_headers: httpx.Headers,
    ) -> None:
        """Classify one HTTP error status with the default wire detail."""
        self._classify_http_status(
            status_code,
            detail=format_http_error_detail(status_code, error_body),
            response_headers=response_headers,
        )

    async def _post_json(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        cache_scope_id: str | None = None,
    ) -> dict[str, Any]:
        return await post_json_with_retry(
            self._client,
            endpoint_path,
            payload,
            build_headers=lambda: self._build_headers(cache_scope_id),
            handle_error_status=self._handle_error_status,
            provider_context="OpenAI provider",
            auth_recovery=OAuthRequestRecovery(self._token_getter, self._auth_config),
        )

    async def _connect_stream(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        cache_scope_id: str | None = None,
    ) -> httpx.Response:
        return await connect_streaming_with_retry(
            self._client,
            endpoint_path,
            payload,
            build_headers=lambda: self._build_headers(cache_scope_id),
            handle_error_status=self._handle_error_status,
            auth_recovery=OAuthRequestRecovery(self._token_getter, self._auth_config),
        )

    async def _stream_responses(
        self,
        payload: dict[str, Any],
        *,
        endpoint_path: str,
        cache_scope_id: str | None = None,
        conversation_id: str | None = None,
        state: ResponsesStreamState | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        stream_state = state or ResponsesStreamState()
        if (
            endpoint_path == CODEX_RESPONSES_ENDPOINT
            and cache_scope_id
            and conversation_id
            and self._codex_transport == _CODEX_TRANSPORT_AUTO
        ):
            async for delta in self._stream_codex_auto(
                payload,
                cache_scope_id=cache_scope_id,
                conversation_id=conversation_id,
                state=stream_state,
            ):
                yield delta
            return
        async for delta in self._stream_responses_sse(
            payload,
            endpoint_path=endpoint_path,
            cache_scope_id=cache_scope_id,
            state=stream_state,
        ):
            yield delta

    async def _stream_responses_sse(
        self,
        payload: dict[str, Any],
        *,
        endpoint_path: str,
        cache_scope_id: str | None,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_stream(endpoint_path, payload, cache_scope_id=cache_scope_id)
        event_lines: list[str] = []
        seen_finish_delta = False
        try:
            async for line in response.aiter_lines():
                if line:
                    event_lines.append(line)
                    continue
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
                event_lines = []
            if event_lines:
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
            if not seen_finish_delta:
                raise NetworkError("Stream ended without response completion event")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    async def _stream_codex_auto(
        self,
        payload: dict[str, Any],
        *,
        cache_scope_id: str,
        conversation_id: str,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        websocket_headers = await self._build_codex_websocket_headers(cache_scope_id)
        account_id = websocket_headers["chatgpt-account-id"]
        model_id = payload.get("model")
        if not isinstance(model_id, str) or not model_id:
            raise ProviderError("Codex WebSocket request is missing a model", retryable=False)
        # The provider-visible headers share cache locality across compatible
        # forks; the local route must remain Session-unique so one branch can
        # never consume another branch's ``previous_response_id`` state.
        route = (conversation_id, model_id, account_id)
        if route in self._codex_websocket_disabled_routes:
            async for delta in self._stream_responses_sse(
                payload,
                endpoint_path=CODEX_RESPONSES_ENDPOINT,
                cache_scope_id=cache_scope_id,
                state=state,
            ):
                yield delta
            return

        try:
            async for delta in self._codex_socket.stream(
                payload,
                headers=websocket_headers,
                route=route,
                state=state,
            ):
                yield delta
        except _CodexWebSocketTransportError as exc:
            self._codex_websocket_disabled_routes.add(route)
            if exc.events_received:
                raise
            async for delta in self._stream_responses_sse(
                payload,
                endpoint_path=CODEX_RESPONSES_ENDPOINT,
                cache_scope_id=cache_scope_id,
                state=state,
            ):
                yield delta

    async def _build_codex_websocket_headers(self, cache_scope_id: str) -> dict[str, str]:
        wire_cache_scope = _clamp_codex_cache_scope(cache_scope_id)
        headers = await self._build_codex_headers(wire_cache_scope)
        headers = {
            name: value
            for name, value in headers.items()
            if name.lower() not in {"accept", "content-type", "openai-beta", "session_id"}
        }
        headers["OpenAI-Beta"] = CODEX_WEBSOCKET_BETA
        headers["session-id"] = wire_cache_scope
        headers["x-client-request-id"] = wire_cache_scope
        return headers


def _clamp_codex_cache_scope(cache_scope_id: str) -> str:
    return cache_scope_id[:_CODEX_CACHE_SCOPE_MAX_LENGTH]
