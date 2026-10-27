"""OpenAI provider adapter.

The wire profile (``resources/wire/openai.json``) decides per Model and
Connection whether a request speaks Chat Completions or Responses and how it is
shaped. Responses requests go to the Platform ``/responses`` endpoint
(``api-key`` connection) or to the ChatGPT Codex ``/codex/responses`` endpoint
(``subscription`` connection with ``mode: codex_responses``). On both, a
Session's Responses requests continue over a kept-open WebSocket and fall back
to SSE with a zstd-compressed body; that transport lives here."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

import httpx
from websockets.asyncio.client import connect as websocket_connect

from core.models.models import (
    Model,
)
from core.providers._openai_constants import (
    _CODEX_CACHE_SCOPE_MAX_LENGTH,
    _CODEX_STABLE_VERSION_PATTERN,
    _NORMALIZED_CODEX_STREAM_RESPONSE_KEY,
    _RESPONSES_TRANSPORT_AUTO,
    _RESPONSES_TRANSPORT_SSE,
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
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OPENAI_SUBSCRIPTION_WIRE_KEY,
    OPTIONAL_REQUEST_PARAMETER_NAMES,
    PROMPT_CACHE_AFFINITY_ID_KWARG,
    REASONING_PARAMETER_NAMES,
    RESPONSES_POLICY_ENDPOINT,
    STRUCTURED_OUTPUT_PARAMETER_NAMES,
    TOOL_PARAMETER_NAMES,
    ResponsesTransport,
    ResponsesWebSocketConnector,
    ResponsesWebSocketRoute,
)
from core.providers._openai_policy import (
    _is_platform_chat_or_embedding_model,
    normalize_catalog_entry,
    normalize_subscription_catalog_entry,
)
from core.providers._responses_profile import (
    RESPONSES_DOCUMENT_TYPES,
    RESPONSES_REASONING_FIELDS,
    profile_responses_policy,
    take_reasoning_renderer,
)
from core.providers._responses_websocket import (
    ResponsesConnectionKey,
    ResponsesRequestDigest,
    ResponsesTransports,
    ResponsesWebSocket,
    _ResponsesWebSocketTransportError,
    responses_conversation_id,
    responses_request_digest,
)
from core.providers._responses_wire import (
    ResponsesStreamState,
    build_responses_payload,
    estimate_responses_input_tokens,
    normalize_responses_response,
    responses_returned_reasoning,
    stream_responses_sse,
)
from core.providers._wire_learning import (
    execute_learning_from_rejections,
    stream_learning_from_rejections,
)
from core.providers.adapter import ModelLookup
from core.providers.effort_updates import apply_effort_updates, effort_levels
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    exhausted_quota_code,
    usage_limit_error,
)
from core.providers.http import (
    PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
    HttpErrorStatusHandler,
    PreparedRequest,
    classify_http_status,
    connect_streaming_with_retry,
    format_http_error_detail,
    post_json_with_retry,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import OAuthRequestRecovery, TokenGetter
from core.providers.wire_profile import Protocol
from core.utils.http_status import parse_rate_limit_retry_after
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import tzinfo

    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope

__all__ = [
    "CODEX_CACHE_SCOPE_HEADERS",
    "CODEX_CLIENT_VERSION_FALLBACK",
    "CODEX_EXTRA_HEADERS",
    "CODEX_PACKAGE_METADATA_URL",
    "CODEX_RESPONSES_ENDPOINT",
    "CODEX_RESPONSES_MODE",
    "CODEX_WEBSOCKET_BETA",
    "CONVERSATION_ID_KWARG",
    "ResponsesTransport",
    "ResponsesTransports",
    "ResponsesWebSocketConnector",
    "ResponsesWebSocketRoute",
    "DISCOVERY_JSON_PARAMETER_NAMES",
    "DISCOVERY_REASONING_PARAMETER_NAMES",
    "DISCOVERY_TOOL_PARAMETER_NAMES",
    "OPENAI_API_KEY_WIRE_KEY",
    "OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS",
    "OPENAI_SUBSCRIPTION_WIRE_KEY",
    "OPTIONAL_REQUEST_PARAMETER_NAMES",
    "OpenAIAdapter",
    "PROMPT_CACHE_AFFINITY_ID_KWARG",
    "REASONING_PARAMETER_NAMES",
    "RESPONSES_POLICY_ENDPOINT",
    "STRUCTURED_OUTPUT_PARAMETER_NAMES",
    "TOOL_PARAMETER_NAMES",
]

_LOGGER = get_logger("providers.openai")

RESPONSES_ZSTD_MIN_BYTES = 32 * 1024
"""A Responses HTTP request body at least this large is sent zstd-compressed, like
OpenAI's Codex client sends its bodies; a smaller one uploads in milliseconds."""

_COMPRESSION_REJECTION = re.compile(r"content[- ]encoding|zstd|compress|unsupported encoding")
"""How a 400 names the request encoding it rejects (lowercased)."""


class _CompressedBodyRejectedError(Exception):
    """The backend refused a zstd request body; resend it uncompressed."""


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for the unified ``openai`` provider.

    The wire profile's ``protocol`` selects Chat Completions (the inherited
    ``/chat/completions`` codec) or Responses for each Model. A Responses
    request goes to ``/codex/responses`` when the Connection's ``mode`` is
    ``CODEX_RESPONSES_MODE`` (the ``subscription`` Connection) and to the
    Platform ``/responses`` otherwise. Its reasoning is the profile's plan in
    the ``responses_reasoning`` dialect; optional parameters, media and the
    output limit follow the profile too.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("chat_completions", "responses")

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
        http_transport: ProviderHTTPTransportScope | None = None,
        responses_transport: ResponsesTransport = _RESPONSES_TRANSPORT_AUTO,
        responses_websocket_connect: ResponsesWebSocketConnector | None = None,
        responses_transports: ResponsesTransports | None = None,
        responses_connection: ResponsesConnectionKey | None = None,
    ) -> None:
        """``responses_transports`` keeps Responses sockets and Connection facts
        beyond this Adapter (the Runtime passes its own, keyed by
        ``responses_connection``); without it the Adapter owns a private one and
        closes it with :meth:`aclose`.
        """
        if responses_transport not in {_RESPONSES_TRANSPORT_AUTO, _RESPONSES_TRANSPORT_SSE}:
            raise ValueError(f"unsupported Responses transport: {responses_transport}")
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
            http_transport=http_transport,
        )
        self._responses_transport = responses_transport
        self._owns_responses_transports = responses_transports is None
        self._responses_transports = responses_transports or ResponsesTransports()
        self._responses_connection: ResponsesConnectionKey = responses_connection or (
            config.id,
            connection_mode or "",
        )
        self._responses_endpoint = (
            CODEX_RESPONSES_ENDPOINT
            if connection_mode == CODEX_RESPONSES_MODE
            else RESPONSES_POLICY_ENDPOINT
        )
        self._responses_socket = ResponsesWebSocket(
            base_url=str(self._client.base_url),
            endpoint_path=self._responses_endpoint,
            connect=responses_websocket_connect
            or cast(ResponsesWebSocketConnector, websocket_connect),
            debug_recorder=self._debug_recorder,
            transports=self._responses_transports,
            connection=self._responses_connection,
        )

    @override
    async def aclose(self) -> None:
        """Close the inherited HTTP client, and the Responses sockets this Adapter owns.

        Shared Responses transports keep the Session's socket for its next Run.
        """
        if self._owns_responses_transports:
            await self._responses_transports.aclose()
        await super().aclose()

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        credential_value: str,
        headers: Mapping[str, str],
        *,
        connection: ConnectionConfig | None = None,
    ) -> dict[str, str]:
        """Add ChatGPT account routing and Codex headers for ``/codex/models``.

        The Platform ``/models`` listing needs only the Connection's API key.
        """

        if not _is_codex_connection(connection):
            return dict(headers)
        account_id = extract_chatgpt_account_id(credential_value)
        if account_id is None:
            raise ProviderAuthError(
                "OpenAI Subscription OAuth token is missing a ChatGPT account id; please reconnect"
            )
        return {**headers, "chatgpt-account-id": account_id, **CODEX_EXTRA_HEADERS}

    @classmethod
    def discovery_params(cls, *, connection: ConnectionConfig | None = None) -> dict[str, str]:
        """Return safe fallback parameters required by ``/codex/models`` discovery."""

        if not _is_codex_connection(connection):
            return {}
        return {"client_version": CODEX_CLIENT_VERSION_FALLBACK}

    @classmethod
    async def resolve_discovery_params(
        cls,
        fetch_json: Callable[[str], Awaitable[Any]],
        *,
        connection: ConnectionConfig | None = None,
    ) -> dict[str, str]:
        """Resolve the current stable Codex version used to gate the model catalog."""

        if not _is_codex_connection(connection):
            return {}
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
        """Keep visible Codex entries and plausible Platform chat/embedding ids.

        New Platform chat Models inherit wire defaults. Known task endpoint
        families remain outside this projection; curated task entries survive.
        """

        del cls
        if _is_codex_connection(connection):
            return raw.get("visibility") != "hide"
        model_id = raw.get("id")
        return (
            isinstance(model_id, str)
            and bool(model_id.strip())
            and _is_platform_chat_or_embedding_model(model_id)
        )

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        base_model = normalize_catalog_entry(raw, defaults)
        if cls is OpenAIAdapter and raw.get("object") == "model":
            # Public Platform identity objects report no subscription capabilities.
            return base_model
        return normalize_subscription_catalog_entry(raw, base_model)

    @override
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
        ``previous_response_id`` continuation on both Connections; the Platform
        ignores the affinity id.
        """
        del project_id
        conversation_id = responses_conversation_id(agent_id, session_id)
        return {
            CONVERSATION_ID_KWARG: conversation_id,
            PROMPT_CACHE_AFFINITY_ID_KWARG: (prompt_cache_affinity_id or conversation_id),
        }

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the selected OpenAI wire's rendered request footprint."""

        if not self._uses_responses(model_id):
            return super().estimate_request_input_tokens(
                messages,
                model_id=model_id,
                tools=tools,
            )
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
            document_media_types=self._responses_document_types(model_id),
            model_id=model_id,
            tools=tools,
        )

    @override
    def _stable_request_headers(
        self, model_id: str, request_kwargs: dict[str, Any]
    ) -> dict[str, str]:
        headers = super()._stable_request_headers(model_id, request_kwargs)
        if (
            self._responses_endpoint == CODEX_RESPONSES_ENDPOINT
            and self.wire_profile(model_id).request.fast_mode
            and request_kwargs.get("service_tier") == "priority"
        ):
            headers.setdefault("x-codex-routing-hint", f"model={model_id};tier=priority")
        return headers

    @override
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
        # A wire profile's per-Model ``request.extra_headers`` is a deliberate
        # declaration: the request paths add it after these headers.
        headers.update(CODEX_EXTRA_HEADERS)
        # Pin the prompt cache to the conversation (see CODEX_CACHE_SCOPE_HEADERS).
        if cache_scope_id:
            wire_cache_scope = _clamp_codex_cache_scope(cache_scope_id)
            for header_name in CODEX_CACHE_SCOPE_HEADERS:
                headers[header_name] = wire_cache_scope
        return headers

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return one completed response without exposing stream deltas.

        A Chat Completions Model delegates to the inherited non-streaming
        ``/chat/completions`` request. A Responses Model on the Codex
        Connection (``connection_mode`` ``CODEX_RESPONSES_MODE``) must stream,
        so the adapter consumes its single streaming exchange internally and
        returns the completed Responses object; Session-scoped calls prefer the
        cached WebSocket transport and otherwise use SSE. On the API-key
        Connection a Responses Model posts to the Platform ``/responses``.
        """

        conversation_id = kwargs.pop(CONVERSATION_ID_KWARG, None)
        prompt_cache_affinity_id = kwargs.pop(
            PROMPT_CACHE_AFFINITY_ID_KWARG,
            conversation_id,
        )
        if not self._uses_responses(model_id):
            return await super().send(messages, model_id=model_id, **kwargs)
        if self._connection_mode == CODEX_RESPONSES_MODE:
            states: list[ResponsesStreamState] = []
            response_events = self._stream_responses_request(
                messages,
                model_id=model_id,
                kwargs=kwargs,
                cache_scope_id=prompt_cache_affinity_id,
                conversation_id=conversation_id,
                states=states,
                handle_error_status=self._handle_error_status,
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
            if not states or states[-1].completed_response is None:
                raise NetworkError("Stream ended without a completed Responses object")
            return {_NORMALIZED_CODEX_STREAM_RESPONSE_KEY: states[-1].normalized_response()}

        request_headers = self._stable_request_headers(model_id, kwargs)

        def build() -> dict[str, Any]:
            return self._build_responses_payload(
                messages,
                model_id=model_id,
                **self._request_kwargs_with_defaults(kwargs),
            )

        response = await execute_learning_from_rejections(
            lambda request: self._post_json(
                RESPONSES_POLICY_ENDPOINT, request, request_headers=request_headers
            ),
            self._request_plan(messages, model_id, build),
            wire=self.wire,
            model_id=model_id,
            provider_label=self._config.id,
        )
        if responses_returned_reasoning(response):
            self.wire.observe_reasoning_returned(model_id)
        return response

    @override
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a request as normalized vBot deltas.

        A Chat Completions Model delegates to the inherited
        ``/chat/completions`` stream; a Responses Model streams from the Codex
        endpoint (``connection_mode`` ``CODEX_RESPONSES_MODE``) or the Platform
        ``/responses``, Session-scoped over the kept-open WebSocket where it can.
        """

        conversation_id = kwargs.pop(CONVERSATION_ID_KWARG, None)
        prompt_cache_affinity_id = kwargs.pop(
            PROMPT_CACHE_AFFINITY_ID_KWARG,
            conversation_id,
        )
        if not self._uses_responses(model_id):
            async with aclosing(
                cast(
                    AsyncGenerator[dict[str, Any]],
                    super().stream(messages, model_id=model_id, **kwargs),
                )
            ) as chat_deltas:
                async for delta in chat_deltas:
                    yield delta
            return
        async with aclosing(
            self._stream_responses_request(
                messages,
                model_id=model_id,
                kwargs=kwargs,
                cache_scope_id=prompt_cache_affinity_id,
                conversation_id=conversation_id,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    @override
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

    def _build_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._refuse_unadmitted_model(model_id)
        request_kwargs = dict(kwargs)
        profile = self.wire_profile(model_id)
        policy = profile_responses_policy(profile, self._catalog_model(model_id))
        # A Model that takes effort updates keeps the effort its history started
        # from and switches with a ``configuration_update`` item where it changed.
        messages = apply_effort_updates(
            messages,
            request_kwargs,
            effort_levels(profile.reasoning) if self.effort_updates(model_id) else None,
        )
        reasoning_renderer = take_reasoning_renderer(profile, request_kwargs)
        # A wire that takes no output-token field (Codex) is budgeted all the
        # same, so input that cannot fit fails before I/O; its policy drops the field.
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=policy,
            stream=stream,
            document_media_types=self._responses_document_types(model_id),
            reasoning_renderer=reasoning_renderer,
            output_limit=lambda kwargs: self._apply_model_output_limit(kwargs, model_id, messages),
            **request_kwargs,
        )
        if self._connection_mode == CODEX_RESPONSES_MODE:
            self._ensure_required_instructions(payload)
        payload["store"] = False
        rules = profile.request
        rules.apply_body(payload)
        # Configured or learned parameter rules; the reasoning fields are
        # the dialect's own output.
        rules.shape_parameters(
            payload,
            reasoning_active="reasoning" in payload,
            protected=RESPONSES_REASONING_FIELDS,
            provider_label=self._config.name,
        )
        return payload

    def _ensure_required_instructions(self, payload: dict[str, Any]) -> None:
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            return
        payload["instructions"] = OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS

    @override
    def effort_updates(self, model_id: str) -> bool:
        """Whether the Model switches its effort with a Responses ``configuration_update``."""

        profile = self.wire_profile(model_id)
        return (
            profile.protocol == "responses"
            and profile.reasoning.effort_updates
            and profile.reasoning.dialect == "responses_reasoning"
        )

    def _uses_responses(self, model_id: str) -> bool:
        """Whether ``model_id``'s requests speak Responses on this Connection."""

        return self.wire_profile(model_id).protocol == "responses"

    def _catalog_model(self, model_id: str) -> Model | None:
        return self._model_lookup(model_id) if self._model_lookup is not None else None

    def _responses_document_types(self, model_id: str) -> frozenset[str]:
        """Document media the Responses wire carries as native ``input_file`` parts."""

        return self.wire_profile(model_id).media.types & RESPONSES_DOCUMENT_TYPES

    def _stream_responses_request(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        kwargs: Mapping[str, Any],
        cache_scope_id: str | None,
        conversation_id: str | None,
        states: list[ResponsesStreamState] | None = None,
        handle_error_status: HttpErrorStatusHandler | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream one Responses request, learning from a rejection before its first delta.

        The wire reports a rejected request as an HTTP error on SSE and as an
        error event on the WebSocket; either arrives before the first delta.
        A failed WebSocket exchange drops its continuation and socket, so the
        rebuilt request opens a fresh exchange with full context. ``states``
        collects the stream state of every attempt (the last one completed).
        When the request will try the WebSocket, its preparation hop digests it
        for the socket's continuation, and its JSON body is encoded only if it
        falls back to HTTP: a WebSocket request encodes just the
        ``response.create`` it sends.
        """

        request_kwargs = dict(kwargs)
        request_headers = self._stable_request_headers(model_id, request_kwargs)

        def build() -> dict[str, Any]:
            return self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(request_kwargs),
            )

        def open_stream(request: PreparedRequest) -> AsyncGenerator[dict[str, Any]]:
            state = _responses_stream_state(self.display_timezone)
            if states is not None:
                states.append(state)
            return self._stream_responses(
                request,
                request_headers=request_headers,
                cache_scope_id=cache_scope_id,
                conversation_id=conversation_id,
                state=state,
                handle_error_status=handle_error_status,
            )

        plan = self._request_plan(messages, model_id, build)
        if self._tries_websocket(cache_scope_id, conversation_id):
            plan = replace(plan, lazy_body=True, digest=responses_request_digest)
        return stream_learning_from_rejections(
            open_stream,
            plan,
            wire=self.wire,
            model_id=model_id,
            provider_label=self._config.id,
        )

    @override
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

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        """Tell an exhausted allowance from a rate limit; both arrive as HTTP 429.

        The body's ``error`` decides, as in OpenAI's Codex client
        (``codex-api/src/api_bridge.rs``, read 2026-10-19): a reached
        subscription usage limit classifies by its reported reset
        (``usage_limit_error``), an exhausted quota fails without retry, and any
        other 429 stays a rate limit.
        """

        if status_code == 429:
            error = _http_error_object(status_code, detail)
            exhausted = usage_limit_error(
                error,
                detail,
                reported_wait=parse_rate_limit_retry_after(response_headers),
                display_timezone=self.display_timezone,
            )
            if exhausted is None and exhausted_quota_code(error) is not None:
                exhausted = ProviderError(f"Quota exhausted: {detail}", retryable=False)
            if exhausted is not None:
                exhausted.status_code = status_code
                raise exhausted
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )

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

    @override
    def _classify_stream_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        """OpenAI's HTTP 500 before a stream starts uses the caller's recovery budget."""
        if status_code == 500 and self._config.id == "openai":
            classify_http_status(
                status_code,
                idempotent=False,
                extra_retryable={500},
                detail=detail,
                response_headers=response_headers,
            )
        self._classify_http_status(status_code, detail=detail, response_headers=response_headers)

    def _handle_stream_error_status(
        self,
        status_code: int,
        error_body: str,
        response_headers: httpx.Headers,
    ) -> None:
        self._classify_stream_http_status(
            status_code,
            detail=format_http_error_detail(status_code, error_body),
            response_headers=response_headers,
        )

    async def _attempt_headers(
        self, cache_scope_id: str | None, request_headers: Mapping[str, str]
    ) -> dict[str, str]:
        """Rebuild one attempt's auth headers, then add the request's stable headers."""

        headers = await self._build_headers(cache_scope_id)
        headers.update(request_headers)
        return headers

    async def _post_json(
        self,
        endpoint_path: str,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
        cache_scope_id: str | None = None,
    ) -> dict[str, Any]:
        """POST one request and decode its reply; a large Responses body goes compressed."""

        def post(
            body: bytes,
            build_headers: Callable[[], Awaitable[dict[str, str]]],
            handle_error_status: HttpErrorStatusHandler,
        ) -> Awaitable[dict[str, Any]]:
            return post_json_with_retry(
                self._client,
                endpoint_path,
                body,
                build_headers=build_headers,
                handle_error_status=handle_error_status,
                provider_context="OpenAI provider",
                auth_recovery=OAuthRequestRecovery(self._token_getter, self._auth_config),
            )

        return await self._send_body(
            post,
            endpoint_path,
            request,
            headers={**request.headers, **request_headers},
            cache_scope_id=cache_scope_id,
            handle_error_status=self._handle_error_status,
        )

    async def _connect_stream(
        self,
        endpoint_path: str,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
        cache_scope_id: str | None = None,
        handle_error_status: HttpErrorStatusHandler | None = None,
    ) -> httpx.Response:
        """Establish one streaming POST; a large Responses body goes compressed."""

        def connect(
            body: bytes,
            build_headers: Callable[[], Awaitable[dict[str, str]]],
            handle_error_status: HttpErrorStatusHandler,
        ) -> Awaitable[httpx.Response]:
            return connect_streaming_with_retry(
                self._client,
                endpoint_path,
                body,
                build_headers=build_headers,
                handle_error_status=handle_error_status,
                auth_recovery=OAuthRequestRecovery(self._token_getter, self._auth_config),
            )

        return await self._send_body(
            connect,
            endpoint_path,
            request,
            headers={**request.headers, **request_headers},
            cache_scope_id=cache_scope_id,
            handle_error_status=handle_error_status or self._handle_stream_error_status,
        )

    async def _send_body[T](
        self,
        send: Callable[
            [bytes, Callable[[], Awaitable[dict[str, str]]], HttpErrorStatusHandler],
            Awaitable[T],
        ],
        endpoint_path: str,
        request: PreparedRequest,
        *,
        headers: Mapping[str, str],
        cache_scope_id: str | None,
        handle_error_status: HttpErrorStatusHandler,
    ) -> T:
        """Send ``request``'s body with ``send``, zstd-compressed when it is a large Responses body.

        A Responses body (Codex or Platform, which documents ``Content-Encoding:
        zstd`` for ``POST /v1/responses``) of at least ``RESPONSES_ZSTD_MIN_BYTES``
        is compressed once, off the Event Loop, and every attempt resends those
        bytes. When the backend rejects the encoding (HTTP 415, a 400 naming it,
        or ``x-openai-retry-uncompressed: true``), the request is sent once more
        uncompressed and the Connection's later bodies stay uncompressed.
        """
        body = await request.encode_body()
        if (
            endpoint_path == self._responses_endpoint
            and len(body) >= RESPONSES_ZSTD_MIN_BYTES
            and self._responses_transports.compresses_requests(self._responses_connection)
        ):
            compressed_headers = {**headers, "Content-Encoding": "zstd"}
            try:
                return await send(
                    await request.zstd_body(),
                    lambda: self._attempt_headers(cache_scope_id, compressed_headers),
                    partial(
                        self._handle_compressed_error_status,
                        handle_error_status=handle_error_status,
                    ),
                )
            except _CompressedBodyRejectedError as rejected:
                self._responses_transports.stop_compressing_requests(self._responses_connection)
                _LOGGER.warning(
                    "%s rejected a zstd request body (%s); sending uncompressed",
                    endpoint_path,
                    rejected,
                )
        return await send(
            body,
            lambda: self._attempt_headers(cache_scope_id, headers),
            handle_error_status,
        )

    def _handle_compressed_error_status(
        self,
        status_code: int,
        error_body: str,
        response_headers: httpx.Headers,
        *,
        handle_error_status: HttpErrorStatusHandler,
    ) -> None:
        """Tell a rejected request encoding from any other error of a compressed request."""
        if (
            status_code == 415
            or response_headers.get("x-openai-retry-uncompressed", "").strip().lower() == "true"
            or (status_code == 400 and _COMPRESSION_REJECTION.search(error_body.lower()))
        ):
            raise _CompressedBodyRejectedError(f"HTTP {status_code}")
        handle_error_status(status_code, error_body, response_headers)

    async def _stream_responses(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
        cache_scope_id: str | None = None,
        conversation_id: str | None = None,
        state: ResponsesStreamState | None = None,
        handle_error_status: HttpErrorStatusHandler | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        stream_state = state or _responses_stream_state(self.display_timezone)
        if (
            cache_scope_id
            and conversation_id
            and self._responses_transport == _RESPONSES_TRANSPORT_AUTO
        ):
            async with aclosing(
                self._stream_websocket_auto(
                    request,
                    request_headers=request_headers,
                    cache_scope_id=cache_scope_id,
                    conversation_id=conversation_id,
                    state=stream_state,
                    handle_error_status=handle_error_status,
                )
            ) as deltas:
                async for delta in deltas:
                    yield delta
            return
        async with aclosing(
            self._stream_responses_sse(
                request,
                request_headers=request_headers,
                cache_scope_id=cache_scope_id,
                state=stream_state,
                handle_error_status=handle_error_status,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def _stream_responses_sse(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
        cache_scope_id: str | None,
        state: ResponsesStreamState,
        handle_error_status: HttpErrorStatusHandler | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        response = await self._connect_stream(
            self._responses_endpoint,
            request,
            request_headers=request_headers,
            cache_scope_id=cache_scope_id,
            handle_error_status=handle_error_status,
        )
        async with aclosing(self._responses_sse_deltas(response, state)) as deltas:
            async for delta in deltas:
                yield delta

    async def _responses_sse_deltas(
        self,
        response: httpx.Response,
        state: ResponsesStreamState,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield the normalized deltas of an established Responses SSE stream, then close it."""

        async with aclosing(stream_responses_sse(response, state)) as deltas:
            async for delta in deltas:
                yield delta

    def _tries_websocket(self, cache_scope_id: str | None, conversation_id: str | None) -> bool:
        """Whether a Responses request with these ids starts on the WebSocket."""
        return bool(
            cache_scope_id
            and conversation_id
            and self._responses_transport == _RESPONSES_TRANSPORT_AUTO
            and self._responses_transports.websocket_available(self._responses_connection)
        )

    async def _stream_websocket_auto(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
        cache_scope_id: str,
        conversation_id: str,
        state: ResponsesStreamState,
        handle_error_status: HttpErrorStatusHandler | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        websocket_headers, account_id = await self._websocket_headers(cache_scope_id)
        # The socket sends its handshake headers once; the request's stable
        # headers ride on that handshake like on an SSE request.
        websocket_headers.update(request.headers)
        websocket_headers.update(request_headers)
        model_id = request.payload.get("model")
        if not isinstance(model_id, str) or not model_id:
            raise ProviderError("Responses WebSocket request is missing a model", retryable=False)
        # The provider-visible headers share cache locality across compatible
        # forks; the local route must remain Session-unique so one branch can
        # never consume another branch's ``previous_response_id`` state.
        route = (conversation_id, model_id, account_id)
        # A request planned while the WebSocket was unavailable has no digest.
        digest = request.digest
        if isinstance(
            digest, ResponsesRequestDigest
        ) and self._responses_transports.websocket_available(self._responses_connection):
            try:
                async with aclosing(
                    self._responses_socket.stream(
                        request.payload,
                        digest=digest,
                        headers=websocket_headers,
                        route=route,
                        state=state,
                    )
                ) as deltas:
                    async for delta in deltas:
                        yield delta
                return
            except _ResponsesWebSocketTransportError as exc:
                if exc.events_received:
                    raise
                # No event arrived, so no delta was yielded and the prepared
                # request is still held for SSE.
        async with aclosing(
            self._stream_responses_sse(
                request,
                request_headers=request_headers,
                cache_scope_id=cache_scope_id,
                state=state,
                handle_error_status=handle_error_status,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def _websocket_headers(self, cache_scope_id: str) -> tuple[dict[str, str], str]:
        """A socket's handshake headers and the account its route names.

        The Platform takes the API key as on HTTP, like OpenAI's Python SDK
        (``responses.connect()``, read 2026-10-19); its route names no account,
        and a changed key changes the handshake, so it opens a new socket. Codex
        needs its WebSocket beta header and cache-scope headers, and its route
        names the ChatGPT account.
        """
        if self._connection_mode != CODEX_RESPONSES_MODE:
            headers = await self._build_headers()
            return {
                name: value
                for name, value in headers.items()
                if name.lower() not in {"accept", "content-type"}
            }, ""
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
        return headers, headers["chatgpt-account-id"]


def _clamp_codex_cache_scope(cache_scope_id: str) -> str:
    return cache_scope_id[:_CODEX_CACHE_SCOPE_MAX_LENGTH]


def _responses_stream_state(display_timezone: tzinfo) -> ResponsesStreamState:
    """Retry in-band errors whose code is unknown, as OpenAI's Codex client does.

    Known deterministic codes and requests rejected with a client-error status
    stay fatal; Chat's recovery budget bounds the retries. A reported
    usage-limit reset is named in ``display_timezone``.
    """
    return ResponsesStreamState(
        lenient_unknown_errors=True,
        rejected_requests_fatal=True,
        display_timezone=display_timezone,
    )


def _http_error_object(status_code: int, detail: str) -> Mapping[str, Any]:
    """The ``error`` object of an HTTP error body; ``detail`` is ``"<status> <body>"``."""

    try:
        body = json.loads(detail.removeprefix(f"{status_code} "))
    except ValueError, RecursionError:
        return {}
    error = body.get("error") if isinstance(body, Mapping) else None
    return error if isinstance(error, Mapping) else {}


# Platform embedding Model ids; the ``/models`` listing has no other fact.


def _is_codex_connection(connection: ConnectionConfig | None) -> bool:
    """Return whether discovery targets the ChatGPT subscription Codex catalog."""

    return getattr(connection, "mode", None) == CODEX_RESPONSES_MODE
