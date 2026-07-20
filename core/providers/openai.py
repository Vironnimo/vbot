"""OpenAI provider adapter.

Handles both the OpenAI Platform ``/chat/completions`` endpoint (default
``api-key`` connection) and the ChatGPT Codex ``/codex/responses`` endpoint
(``subscription`` connection with ``mode: codex_responses``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import (
    classify_http_status,
    decode_response_json,
    wrap_network_error,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import NetworkError, ProviderAuthError, ProviderError
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openai_subscription_auth import extract_chatgpt_account_id
from core.providers.providers import ProviderConfig
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.utils.retry import retry_async

CODEX_RESPONSES_MODE = "codex_responses"
CODEX_EXTRA_HEADERS: dict[str, str] = {
    "OpenAI-Beta": "responses=experimental",
    "originator": "vbot",
}
CODEX_RESPONSES_ENDPOINT = "/codex/responses"
RESPONSES_POLICY_ENDPOINT = "/responses"
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
_NORMALIZED_CODEX_STREAM_RESPONSE_KEY = "_normalized_codex_stream_response"


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for the unified ``openai`` provider.

    The connection's ``mode`` selects the wire variant:

    - ``CODEX_RESPONSES_MODE`` (``"codex_responses"``): Codex Responses API
      (``/codex/responses``) — used by the ``subscription`` connection.
    - ``None`` (default): inherited OpenAI-compatible ``/chat/completions``
      — used by the ``api-key`` connection.
    """

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
    ) -> dict[str, Any]:
        """Scope the Codex prompt cache to the conversation.

        The ChatGPT Codex backend routes its prompt cache by per-request headers
        (``CODEX_CACHE_SCOPE_HEADERS``), not the body-level ``prompt_cache_key``.
        Handing the adapter a stable conversation id lets the ``subscription``
        (Codex) path stamp those headers so a growing shared prefix reliably hits
        the same cache-warm machine across turns. The value only steers routing,
        never correctness — the wire prefix still decides what actually caches —
        so ``agent_id:session_id`` (stable per conversation) is enough. The
        ``api-key`` ``/chat/completions`` path ignores it (that path caches by
        OpenAI's default prefix-hash routing).
        """
        del project_id
        return {CONVERSATION_ID_KWARG: f"{agent_id}:{session_id}"}

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
            for header_name in CODEX_CACHE_SCOPE_HEADERS:
                headers[header_name] = cache_scope_id
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
        adapter consumes its single SSE request internally and returns the
        completed Responses object. Other connections delegate to the inherited
        non-streaming ``/chat/completions`` request.
        """

        conversation_id = kwargs.pop(CONVERSATION_ID_KWARG, None)
        if self._connection_mode == CODEX_RESPONSES_MODE:
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            state = ResponsesStreamState()
            async for _delta in self._stream_responses(
                payload,
                cache_scope_id=conversation_id,
                state=state,
            ):
                pass
            if state.completed_response is None:
                raise NetworkError("Stream ended without a completed Responses object")
            return {_NORMALIZED_CODEX_STREAM_RESPONSE_KEY: state.normalized_response()}
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
        if self._connection_mode == CODEX_RESPONSES_MODE:
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            async for delta in self._stream_responses(payload, cache_scope_id=conversation_id):
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
        if self._connection_mode == CODEX_RESPONSES_MODE and isinstance(
            response.get("output"), list
        ):
            return normalize_responses_response(response)
        return super().normalize_response(response, model_id=model_id)

    def _request_kwargs_with_defaults(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {}
        if self._config.defaults:
            request_kwargs.update(self._config.defaults)
        request_kwargs.update(kwargs)
        return request_kwargs

    def _build_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=self._responses_policy_for_model(model_id),
            stream=stream,
            **kwargs,
        )
        self._ensure_required_instructions(payload)
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
        )

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

            classify_http_status(
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
            request = self._client.build_request(
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
                classify_http_status(
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
        cache_scope_id: str | None = None,
        state: ResponsesStreamState | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_stream(
            CODEX_RESPONSES_ENDPOINT, payload, cache_scope_id=cache_scope_id
        )
        stream_state = state or ResponsesStreamState()
        event_lines: list[str] = []
        seen_finish_delta = False
        try:
            async for line in response.aiter_lines():
                if line:
                    event_lines.append(line)
                    continue
                for delta in iter_responses_sse_deltas_with_state(event_lines, stream_state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
                event_lines = []
            if event_lines:
                for delta in iter_responses_sse_deltas_with_state(event_lines, stream_state):
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
