"""OpenAI provider adapter.

Handles the model-selected OpenAI Platform endpoint (``api-key`` connection)
and the ChatGPT Codex ``/codex/responses`` endpoint (``subscription``
connection with ``mode: codex_responses``).
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from websockets.asyncio.client import connect as websocket_connect

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import (
    PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
    build_streaming_request,
    decode_response_json,
    wrap_network_error,
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
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
    normalize_responses_stream_event,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.providers.token_getter import TokenGetter
from core.utils.retry import retry_async

CODEX_RESPONSES_MODE = "codex_responses"
CODEX_EXTRA_HEADERS: dict[str, str] = {
    "OpenAI-Beta": "responses=experimental",
    "originator": "vbot",
}
CODEX_WEBSOCKET_BETA = "responses_websockets=2026-02-06"
CODEX_RESPONSES_ENDPOINT = "/codex/responses"
RESPONSES_POLICY_ENDPOINT = "/responses"
OPENAI_METADATA_KEY = "openai"
OPENAI_WIRE_POLICIES_KEY = "wire_policies"
OPENAI_API_KEY_WIRE_KEY = "api-key"
OPENAI_SUBSCRIPTION_WIRE_KEY = "subscription"
OPENAI_RESPONSES_PROTOCOL = "responses"
OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS = frozenset(
    {"max_tokens", "max_output_tokens", "top_p"}
)
OPENAI_REASONING_CONTEXTS = frozenset({"auto", "current_turn", "all_turns"})
OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
OPENAI_SUBSCRIPTION_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS = frozenset({"top_p"})
OPTIONAL_REQUEST_PARAMETER_NAMES = frozenset(
    {"max_tokens", "max_output_tokens", "temperature", "top_p", "top_k", "stop_sequences"}
)
REASONING_PARAMETER_NAMES = frozenset(
    {"thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"}
)
STRUCTURED_OUTPUT_PARAMETER_NAMES = frozenset(
    {"response_format", "structured_outputs", "json_mode"}
)
TOOL_PARAMETER_NAMES = frozenset({"tools", "tool_choice", "parallel_tool_calls"})
DISCOVERY_TOOL_PARAMETER_NAMES = frozenset({"tools", "tool_calls", "function_calling"})
DISCOVERY_JSON_PARAMETER_NAMES = frozenset({"response_format", "structured_outputs", "json_mode"})
DISCOVERY_REASONING_PARAMETER_NAMES = frozenset(
    {"reasoning", "reasoning_effort", "include_reasoning", "thinking_effort"}
)
CODEX_CLIENT_VERSION_FALLBACK = "0.144.0"
CODEX_PACKAGE_METADATA_URL = "https://registry.npmjs.org/@openai%2Fcodex/latest"
_CODEX_STABLE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
# Codex backend prompt-cache routing: the ChatGPT ``/codex/responses`` backend
# routes the prompt cache by these request HEADERS scoped to the conversation —
# NOT by the body-level ``prompt_cache_key`` (verified live 2026-07-09: the body
# field has no measurable effect, while a stable conversation scope on these
# headers lifts cache hits from ~1/6 to ~5/6). Mirrors the Codex CLI and the
# hermes-agent transport.
CODEX_CACHE_SCOPE_HEADERS = ("session_id", "x-client-request-id")
CONVERSATION_ID_KWARG = "conversation_id"
PROMPT_CACHE_AFFINITY_ID_KWARG = "prompt_cache_affinity_id"
_NORMALIZED_CODEX_STREAM_RESPONSE_KEY = "_normalized_codex_stream_response"
_CODEX_TRANSPORT_AUTO: Literal["auto"] = "auto"
_CODEX_TRANSPORT_SSE: Literal["sse"] = "sse"
_CODEX_CACHE_SCOPE_MAX_LENGTH = 64
_CODEX_WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 10.0
_CODEX_WEBSOCKET_STATUS_CODE = 101

CodexTransport = Literal["auto", "sse"]
CodexWebSocketConnector = Callable[..., Awaitable[Any]]
CodexWebSocketRoute = tuple[str, str, str]


@dataclass
class _CodexWebSocketContinuation:
    route: CodexWebSocketRoute
    last_request_payload: dict[str, Any]
    last_response_id: str
    last_response_items: list[dict[str, Any]]


class _CodexPreviousResponseMissingError(Exception):
    """Connection-scoped continuation vanished and must be replayed in full."""


class _CodexWebSocketTransportError(NetworkError):
    """Codex WebSocket failed before or after receiving a provider event."""

    def __init__(self, message: str, *, events_received: bool) -> None:
        super().__init__(message)
        self.events_received = events_received


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
        self._codex_websocket_connect = codex_websocket_connect or cast(
            CodexWebSocketConnector, websocket_connect
        )
        self._codex_websocket: Any | None = None
        self._codex_websocket_route: CodexWebSocketRoute | None = None
        self._codex_websocket_continuation: _CodexWebSocketContinuation | None = None
        self._codex_websocket_disabled_routes: set[CodexWebSocketRoute] = set()
        self._codex_websocket_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the cached Codex WebSocket and inherited HTTP client."""
        await self._close_codex_websocket()
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
        request_kwargs: dict[str, Any] = {}
        if self._config.defaults:
            request_kwargs.update(self._config.defaults)
        request_kwargs.update(kwargs)
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
        self._apply_model_output_limit(request_kwargs, model_id, messages)
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=self._responses_policy_for_model(model_id),
            stream=stream,
            document_media_types=(
                frozenset({"application/pdf"})
                if self._uses_platform_responses(model_id)
                else frozenset()
            ),
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

    async def _post_json(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        cache_scope_id: str | None = None,
    ) -> dict[str, Any]:
        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers(cache_scope_id)
            try:
                response = await self._client.post(endpoint_path, json=payload, headers=headers)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            self._classify_http_status(
                response.status_code,
                detail=_http_error_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "OpenAI provider"))

        return await retry_async(_do_request)

    async def _connect_stream(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        cache_scope_id: str | None = None,
    ) -> httpx.Response:
        async def _connect() -> httpx.Response:
            # Rebuild headers per attempt: an OAuth token may refresh during a
            # retry backoff, and the getter must be re-consulted each time.
            headers = await self._build_headers(cache_scope_id)
            request = build_streaming_request(
                self._client,
                "POST",
                endpoint_path,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                self._classify_http_status(
                    response.status_code,
                    detail=_http_error_detail(response, error_body),
                    response_headers=response.headers,
                )
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)
            return response

        return await retry_async(_connect)

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
            async for delta in self._stream_codex_websocket(
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

    async def _stream_codex_websocket(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._codex_websocket_lock:
            if self._codex_websocket_route not in {None, route}:
                await self._close_codex_websocket()
            request_payload = self._build_codex_cached_request(payload, route)
            retried_missing_continuation = False
            while True:
                try:
                    async for delta in self._stream_codex_websocket_attempt(
                        request_payload,
                        headers=headers,
                        route=route,
                        state=state,
                    ):
                        yield delta
                except _CodexPreviousResponseMissingError:
                    if (
                        "previous_response_id" not in request_payload
                        or retried_missing_continuation
                    ):
                        self._codex_websocket_continuation = None
                        await self._close_codex_websocket()
                        raise ProviderError(
                            "Codex WebSocket continuation was not found",
                            retryable=False,
                        ) from None
                    retried_missing_continuation = True
                    self._codex_websocket_continuation = None
                    await self._close_codex_websocket()
                    request_payload = copy.deepcopy(payload)
                    continue
                except BaseException:
                    self._codex_websocket_continuation = None
                    await self._close_codex_websocket()
                    raise

                self._remember_codex_websocket_continuation(payload, route, state)
                return

    async def _stream_codex_websocket_attempt(
        self,
        request_payload: dict[str, Any],
        *,
        headers: dict[str, str],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> AsyncIterator[dict[str, Any]]:
        wire_payload = {"type": "response.create", **request_payload}
        wire_text = json.dumps(wire_payload, ensure_ascii=False, separators=(",", ":"))
        capture = (
            self._debug_recorder.begin_capture(
                method="WEBSOCKET",
                url=self._codex_websocket_url(),
                headers=headers,
                body=wire_text.encode("utf-8"),
            )
            if self._debug_recorder is not None
            else None
        )
        events_received = False
        model_delta_received = False
        finish_received = False
        try:
            websocket = await self._ensure_codex_websocket(route, headers)
            if capture is not None:
                status_code, response_headers = _codex_websocket_response_head(websocket)
                capture.record_response_head(status_code, response_headers)
            send_result = websocket.send(wire_text)
            if inspect.isawaitable(send_result):
                await send_result
            while True:
                raw_frame = await websocket.recv()
                if isinstance(raw_frame, bytes):
                    frame_bytes = raw_frame
                    frame_text = raw_frame.decode("utf-8", errors="replace")
                elif isinstance(raw_frame, str):
                    frame_text = raw_frame
                    frame_bytes = raw_frame.encode("utf-8")
                else:
                    raise TypeError("Codex WebSocket returned a non-text frame")
                events_received = True
                if capture is not None:
                    capture.feed_body(frame_bytes + b"\n")
                event = json.loads(frame_text)
                if not isinstance(event, Mapping):
                    raise ValueError("Codex WebSocket event must be an object")
                event_data = dict(event)
                if (
                    "previous_response_id" in request_payload
                    and not model_delta_received
                    and _codex_responses_error_code(event_data) == "previous_response_not_found"
                ):
                    raise _CodexPreviousResponseMissingError
                event_type = event_data.get("type")
                event_name = event_type if isinstance(event_type, str) else ""
                deltas = normalize_responses_stream_event(event_name, event_data, state)
                for delta in deltas:
                    if delta.get("type") in {
                        "content_delta",
                        "reasoning_delta",
                        "tool_call_delta",
                    }:
                        model_delta_received = True
                    if delta.get("type") == "finish":
                        finish_received = True
                    yield delta
                if finish_received:
                    return
        except _CodexPreviousResponseMissingError:
            raise
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transport_error = (
                exc
                if isinstance(exc, _CodexWebSocketTransportError)
                else _CodexWebSocketTransportError(
                    f"Codex WebSocket failed: {exc}",
                    events_received=events_received,
                )
            )
            if capture is not None:
                capture.record_error(transport_error)
            raise transport_error from exc
        finally:
            if capture is not None:
                capture.finalize()

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

    async def _ensure_codex_websocket(
        self,
        route: CodexWebSocketRoute,
        headers: dict[str, str],
    ) -> Any:
        if self._codex_websocket is not None and self._codex_websocket_route == route:
            return self._codex_websocket
        await self._close_codex_websocket()
        connection = self._codex_websocket_connect(
            self._codex_websocket_url(),
            additional_headers=headers,
            open_timeout=_CODEX_WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
            max_size=None,
        )
        websocket = await connection if inspect.isawaitable(connection) else connection
        self._codex_websocket = websocket
        self._codex_websocket_route = route
        return websocket

    async def _close_codex_websocket(self) -> None:
        websocket = self._codex_websocket
        self._codex_websocket = None
        self._codex_websocket_route = None
        self._codex_websocket_continuation = None
        if websocket is None:
            return
        try:
            close_result = websocket.close()
            if inspect.isawaitable(close_result):
                await close_result
        except Exception:
            pass

    def _codex_websocket_url(self) -> str:
        base_url = str(self._client.base_url).rstrip("/")
        if base_url.startswith("https://"):
            websocket_base = f"wss://{base_url.removeprefix('https://')}"
        elif base_url.startswith("http://"):
            websocket_base = f"ws://{base_url.removeprefix('http://')}"
        else:
            raise ProviderError(
                f"Codex WebSocket requires an HTTP(S) base URL, got {base_url!r}",
                retryable=False,
            )
        return f"{websocket_base}{CODEX_RESPONSES_ENDPOINT}"

    def _build_codex_cached_request(
        self,
        payload: dict[str, Any],
        route: CodexWebSocketRoute,
    ) -> dict[str, Any]:
        continuation = self._codex_websocket_continuation
        if continuation is None or continuation.route != route:
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        if not _codex_payloads_match_except_input(
            payload,
            continuation.last_request_payload,
        ):
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        current_input = payload.get("input")
        previous_input = continuation.last_request_payload.get("input")
        if not isinstance(current_input, list) or not isinstance(previous_input, list):
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        baseline = [*previous_input, *continuation.last_response_items]
        if len(current_input) < len(baseline) or current_input[: len(baseline)] != baseline:
            self._codex_websocket_continuation = None
            return copy.deepcopy(payload)
        request_payload = copy.deepcopy(payload)
        request_payload["previous_response_id"] = continuation.last_response_id
        request_payload["input"] = copy.deepcopy(current_input[len(baseline) :])
        return request_payload

    def _remember_codex_websocket_continuation(
        self,
        payload: dict[str, Any],
        route: CodexWebSocketRoute,
        state: ResponsesStreamState,
    ) -> None:
        completed_response = state.completed_response
        if not isinstance(completed_response, Mapping):
            self._codex_websocket_continuation = None
            return
        response_id = completed_response.get("id")
        normalized_response = state.normalized_response()
        response_items = self._build_responses_payload(
            [normalized_response],
            model_id=route[1],
            stream=True,
        ).get("input")
        if (
            not isinstance(response_id, str)
            or not response_id
            or not isinstance(response_items, list)
        ):
            self._codex_websocket_continuation = None
            return
        self._codex_websocket_continuation = _CodexWebSocketContinuation(
            route=route,
            last_request_payload=copy.deepcopy(payload),
            last_response_id=response_id,
            last_response_items=[
                copy.deepcopy(item) for item in response_items if isinstance(item, dict)
            ],
        )


def _codex_websocket_response_head(websocket: Any) -> tuple[int, dict[str, str]]:
    response = getattr(websocket, "response", None)
    raw_status = getattr(response, "status_code", _CODEX_WEBSOCKET_STATUS_CODE)
    status_code = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool)
        else _CODEX_WEBSOCKET_STATUS_CODE
    )
    raw_headers = getattr(response, "headers", None)
    try:
        headers = dict(raw_headers) if raw_headers is not None else {}
    except (TypeError, ValueError):
        headers = {}
    return status_code, {str(name): str(value) for name, value in headers.items()}


def _clamp_codex_cache_scope(cache_scope_id: str) -> str:
    return cache_scope_id[:_CODEX_CACHE_SCOPE_MAX_LENGTH]


def _codex_responses_error_code(event: Mapping[str, Any]) -> str | None:
    response = event.get("response")
    payload = response if isinstance(response, Mapping) else event
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    code = payload.get("code")
    return code if isinstance(code, str) and code else None


def _codex_payloads_match_except_input(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    ignored = {"input", "previous_response_id"}
    current_rest = {key: value for key, value in current.items() if key not in ignored}
    previous_rest = {key: value for key, value in previous.items() if key not in ignored}
    return current_rest == previous_rest


@dataclass(frozen=True)
class OpenAISubscriptionResponsesPolicy:
    """Responses request policy for OpenAI Subscription models."""

    allowed_reasoning_efforts: frozenset[str]
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool
    supports_streaming: bool = True
    endpoint_path: str = RESPONSES_POLICY_ENDPOINT
    supported_request_parameters: frozenset[str] = OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(self.allowed_reasoning_efforts)

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered_kwargs = dict(kwargs)
        if not self.supports_tools:
            for parameter_name in TOOL_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        elif not self.supports_parallel_tool_calls:
            filtered_kwargs.pop("parallel_tool_calls", None)

        if not self.supports_structured_outputs:
            for parameter_name in STRUCTURED_OUTPUT_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)

        if not self.allows_any_reasoning_controls:
            for parameter_name in REASONING_PARAMETER_NAMES:
                filtered_kwargs.pop(parameter_name, None)
        else:
            self._normalize_reasoning_effort(filtered_kwargs, "thinking_effort")
            self._normalize_reasoning_effort(filtered_kwargs, "reasoning_effort")

        for parameter_name in OPTIONAL_REQUEST_PARAMETER_NAMES:
            if (
                parameter_name in filtered_kwargs
                and parameter_name not in self.supported_request_parameters
            ):
                filtered_kwargs.pop(parameter_name, None)
        return filtered_kwargs

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized_effort = normalize_thinking_effort(effort)
        if not normalized_effort:
            return None
        if normalized_effort == "none":
            return "none" if self.allows_any_reasoning_controls else None
        return closest_supported_effort(normalized_effort, self.allowed_reasoning_efforts)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in self.supported_request_parameters

    def _normalize_reasoning_effort(
        self,
        filtered_kwargs: dict[str, Any],
        parameter_name: str,
    ) -> None:
        if parameter_name not in filtered_kwargs:
            return
        safe_effort = self.closest_reasoning_effort(filtered_kwargs.get(parameter_name))
        if safe_effort is None:
            filtered_kwargs.pop(parameter_name, None)
            return
        filtered_kwargs[parameter_name] = safe_effort


def _http_error_detail(response: httpx.Response, body: str | None = None) -> str:
    reason = response.text if body is None else body
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _normalize_catalog_raw(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = dict(raw)
    if not _optional_string(normalized.get("id")):
        slug = _optional_string(normalized.get("slug")) or _optional_string(normalized.get("model"))
        if slug:
            normalized["id"] = slug
    if not _optional_string(normalized.get("name")):
        display_name = _optional_string(normalized.get("display_name")) or _optional_string(
            normalized.get("title")
        )
        if display_name:
            normalized["name"] = display_name
    return normalized


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def _subscription_capability_supported(
    raw_parameters: frozenset[str],
    default_value: bool,
    metadata_sources: tuple[Mapping[str, Any], ...],
    explicit_keys: tuple[str, ...],
    matching_parameters: frozenset[str],
) -> bool:
    explicit_value = _first_optional_bool(metadata_sources, explicit_keys)
    if explicit_value is not None:
        return explicit_value
    if raw_parameters:
        return bool(raw_parameters & matching_parameters)
    return default_value


def _subscription_supported_parameters(
    raw_parameters: frozenset[str],
    tools_supported: bool,
    json_supported: bool,
    reasoning_supported: bool,
) -> list[str]:
    supported_parameters: list[str] = []
    sparse_catalog = not raw_parameters
    if tools_supported:
        supported_parameters.append("tools")
    if json_supported:
        supported_parameters.append("response_format")
    if reasoning_supported:
        supported_parameters.append("reasoning")
    if tools_supported and (sparse_catalog or "parallel_tool_calls" in raw_parameters):
        supported_parameters.append("parallel_tool_calls")
    return supported_parameters


def _first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
            if key == "reasoning" and isinstance(value, Mapping):
                supported = value.get("supported")
                if isinstance(supported, bool):
                    return supported
    return None
