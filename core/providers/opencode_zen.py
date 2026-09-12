"""OpenCode Zen multi-protocol provider adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import httpx

from core.models.models import Model
from core.providers._http_shared import (
    build_streaming_request,
    classify_http_status,
    decode_response_json,
    iter_sse_events,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._opencode_zen_gemini import (
    _apply_gemini_response_format,
    _content_text,
    _gemini_tool_choice,
    _move_integer,
    _move_number,
    _normalize_gemini_response,
    _normalize_gemini_stream_chunk,
    _to_gemini_content,
)
from core.providers._opencode_zen_profiles import (
    _AUTH_401_MARKERS,
    _FREE_MODELS,
    _IMMINENT_DEPRECATIONS,
    _KNOWN_PROTOCOLS,
    _NON_AUTH_401_MARKERS,
    _PERMANENT_429_MARKERS,
    _PROTOCOL_BY_MODEL,
    _RETIRED_MODELS,
    _ZEN_GEMINI_MEDIA_TYPES,
    _ZEN_INLINE_REQUEST_MAX_BYTES,
    _ZEN_MAX_IMAGES_PER_REQUEST,
    DEPRECATES_AT_METADATA_KEY,
    OPENCODE_ZEN_METADATA_KEY,
    PRIVACY_METADATA_KEY,
    PROTOCOL_CHAT,
    PROTOCOL_GEMINI,
    PROTOCOL_MESSAGES,
    PROTOCOL_METADATA_KEY,
    PROTOCOL_RESPONSES,
)
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    ModelLookup,
    project_tool_result_content_fallbacks,
)
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.errors import (
    CatalogEntrySkipped,
    NetworkError,
    ProviderAuthError,
    ProviderError,
)
from core.providers.openai import OPENAI_RESPONSES_PROTOCOL, OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.providers.token_getter import OAuthRequestRecovery, TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "DEPRECATES_AT_METADATA_KEY",
    "OPENCODE_ZEN_METADATA_KEY",
    "OpenCodeZenAdapter",
    "PRIVACY_METADATA_KEY",
    "PROTOCOL_CHAT",
    "PROTOCOL_GEMINI",
    "PROTOCOL_MESSAGES",
    "PROTOCOL_METADATA_KEY",
    "PROTOCOL_RESPONSES",
]


def _classify_zen_status(
    status_code: int,
    *,
    detail: str,
    response_headers: httpx.Headers,
) -> None:
    normalized = detail.casefold()
    if status_code == 401:
        if any(marker in normalized for marker in _NON_AUTH_401_MARKERS):
            raise ProviderError(
                f"OpenCode Zen account or Model access denied: {detail}", retryable=False
            )
        if any(marker in normalized for marker in _AUTH_401_MARKERS):
            raise ProviderAuthError(f"OpenCode Zen authentication failed: {detail}")
    if status_code == 403 and "regionerror" in normalized:
        raise ProviderError(f"OpenCode Zen region is not allowed: {detail}", retryable=False)
    if status_code == 429 and any(marker in normalized for marker in _PERMANENT_429_MARKERS):
        raise ProviderError(f"OpenCode Zen allowance exhausted: {detail}", retryable=False)
    classify_http_status(
        status_code,
        idempotent=False,
        detail=detail,
        response_headers=response_headers,
    )


class _OpenCodeZenMessagesAdapter(AnthropicCompatibleAdapter):
    """Zen's Anthropic Messages route with Zen error semantics."""

    @staticmethod
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        # Zen's error name/type determines entitlement vs authentication even
        # when the gateway omits the human-readable message.
        return f"{status_code} {response_body}".strip()

    def wire_media_support(self, _model_id: str) -> frozenset[str]:
        return IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"}

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_zen_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


class OpenCodeZenAdapter(OpenAIAdapter):
    """Route OpenCode Zen Models across its four official wire protocols."""

    @classmethod
    async def resolve_discovery_params(cls, fetch_json: Any) -> dict[str, str]:
        """Zen's public Model listing has no Codex client-version query."""

        del cls, fetch_json
        return {}

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        _credential_value: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Use the selected Zen Connection header without OpenAI account routing."""

        del cls
        return dict(headers)

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
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
        )
        selected_auth = auth_config or config.connections[0].auth
        self._messages = _OpenCodeZenMessagesAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=selected_auth.credential_key,
            ),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
            api_version=ANTHROPIC_VERSION,
            prompt_caching=True,
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
        )

    async def aclose(self) -> None:
        await self._messages.aclose()
        await super().aclose()

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        raw_model_id = raw.get("id")
        if isinstance(raw_model_id, str) and raw_model_id in _RETIRED_MODELS:
            raise CatalogEntrySkipped(f"OpenCode Zen Model {raw_model_id!r} is retired")
        model = OpenAICompatibleAdapter.normalize_catalog_entry(raw, defaults)
        protocol = _PROTOCOL_BY_MODEL.get(model.model_id)
        if protocol is None:
            raise CatalogEntrySkipped(
                f"OpenCode Zen Model {model.model_id!r} has no reviewed endpoint protocol"
            )
        profile: dict[str, Any] = {PROTOCOL_METADATA_KEY: protocol}
        if model.model_id in _FREE_MODELS:
            profile[PRIVACY_METADATA_KEY] = "free_model_data_collection"
        if deprecates_at := _IMMINENT_DEPRECATIONS.get(model.model_id):
            profile[DEPRECATES_AT_METADATA_KEY] = deprecates_at
        return replace(
            model,
            metadata={**model.metadata, OPENCODE_ZEN_METADATA_KEY: profile},
        )

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, session_id, project_id, prompt_cache_affinity_id
        return {}

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        protocol = self._model_protocol(model_id)
        if protocol == PROTOCOL_GEMINI:
            return _ZEN_GEMINI_MEDIA_TYPES
        if protocol == PROTOCOL_MESSAGES:
            return self._messages.wire_media_support(model_id)
        # Zen's format converters preserve images on Responses and Chat paths;
        # they do not establish native PDF/audio/video forwarding there.
        return IMAGE_WIRE_MEDIA_TYPES

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        protocol = self._model_protocol(model_id)
        if protocol == PROTOCOL_MESSAGES:
            return await self._messages.send(messages, model_id=model_id, **kwargs)
        if protocol == PROTOCOL_GEMINI:
            return await self._send_gemini(messages, model_id=model_id, **kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        protocol = self._model_protocol(model_id)
        if protocol == PROTOCOL_MESSAGES:
            return self._messages.stream(messages, model_id=model_id, **kwargs)
        if protocol == PROTOCOL_GEMINI:
            return self._stream_gemini(messages, model_id=model_id, **kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self._model_protocol(model_id)
            if protocol == PROTOCOL_MESSAGES:
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == PROTOCOL_GEMINI:
                return _normalize_gemini_response(response)
            return super().normalize_response(response, model_id=model_id)
        if "candidates" in response or "promptFeedback" in response:
            return _normalize_gemini_response(response)
        if response.get("type") == "message":
            return self._messages.normalize_response(response)
        return super().normalize_response(response)

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_zen_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )

    def _model_wire_policy(self, model_id: str) -> Mapping[str, Any]:
        protocol = self._model_protocol(model_id)
        policy: dict[str, Any] = {}
        if protocol == PROTOCOL_RESPONSES:
            policy["protocol"] = OPENAI_RESPONSES_PROTOCOL
        return policy

    def _model_protocol(self, model_id: str) -> str:
        protocol = self._profile_value(model_id, PROTOCOL_METADATA_KEY)
        if protocol not in _KNOWN_PROTOCOLS:
            raise ProviderError(
                f"OpenCode Zen Model {model_id!r} has no reviewed wire protocol",
                retryable=False,
            )
        return cast(str, protocol)

    def _profile_value(self, model_id: str, key: str) -> Any:
        if self._model_lookup is None:
            return None
        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if model is None:
                continue
            profile = model.metadata.get(OPENCODE_ZEN_METADATA_KEY)
            return profile.get(key) if isinstance(profile, Mapping) else None
        return None

    async def _gemini_headers(self) -> dict[str, str]:
        token = await self._token_getter()
        return {**(self._config.extra_headers or {}), "x-goog-api-key": token}

    async def _send_gemini(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = self._build_gemini_payload(messages, model_id, kwargs)
        auth_recovery = OAuthRequestRecovery(
            self._token_getter, AuthConfig(header="x-goog-api-key", prefix="")
        )

        async def _request() -> dict[str, Any]:
            headers = await self._gemini_headers()
            try:
                response = await self._client.post(
                    f"/models/{model_id}:generateContent",
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            auth_recovery.record_response(
                response.status_code, headers, response.text if response.status_code >= 400 else ""
            )
            self._classify_http_status(
                response.status_code,
                detail=_response_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "OpenCode Zen Gemini provider"))

        return await auth_recovery.run(lambda: retry_async(_request))

    async def _stream_gemini(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._build_gemini_payload(messages, model_id, kwargs)
        auth_recovery = OAuthRequestRecovery(
            self._token_getter, AuthConfig(header="x-goog-api-key", prefix="")
        )

        async def _connect() -> httpx.Response:
            headers = await self._gemini_headers()
            request = build_streaming_request(
                self._client,
                "POST",
                f"/models/{model_id}:streamGenerateContent?alt=sse",
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            auth_recovery.record_response(response.status_code, headers)
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                auth_recovery.record_response(response.status_code, headers, body)
                self._classify_http_status(
                    response.status_code,
                    detail=f"{response.status_code} {body}".strip(),
                    response_headers=response.headers,
                )
            return response

        response = await auth_recovery.run(lambda: retry_async(_connect))
        replay_parts: list[dict[str, Any]] = []
        seen_finish = False
        has_tool_calls = False
        try:
            async for event in iter_sse_events(response):
                if event.comment is not None:
                    yield {"type": "heartbeat"}
                    continue
                if event.data is None:
                    continue
                raw = parse_sse_json_data(event.data, context="OpenCode Zen Gemini provider")
                if not isinstance(raw, dict):
                    raise ProviderError(
                        "OpenCode Zen Gemini provider sent non-object JSON in stream",
                        retryable=False,
                    )
                deltas, chunk_has_tools, chunk_finished = _normalize_gemini_stream_chunk(
                    raw,
                    replay_parts,
                    has_tool_calls=has_tool_calls,
                )
                has_tool_calls = has_tool_calls or chunk_has_tools
                seen_finish = seen_finish or chunk_finished
                for delta in deltas:
                    yield delta
            if not seen_finish:
                raise NetworkError("Stream ended without a Gemini finish reason")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    def _build_gemini_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = {key: value for key, value in kwargs.items() if value is not None}
        self._apply_model_output_limit(request, model_id, messages)
        if model_ceiling := self._model_max_output_tokens(model_id):
            for output_key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                value = request.get(output_key)
                if isinstance(value, int) and not isinstance(value, bool):
                    request[output_key] = min(value, model_ceiling)
        projected = project_tool_result_content_fallbacks(messages)
        system_parts: list[dict[str, str]] = []
        contents: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}
        image_count = 0
        for message in projected:
            if message.get("role") == "system":
                system_parts.append({"text": _content_text(message.get("content"))})
                continue
            if message.get("role") == "assistant":
                for tool_call in message.get("tool_calls") or []:
                    if not isinstance(tool_call, Mapping):
                        continue
                    call_id = tool_call.get("id")
                    name = tool_call.get("name")
                    if isinstance(call_id, str) and isinstance(name, str):
                        tool_names[call_id] = name
            projected_message = message
            if message.get("role") == "tool" and not message.get("name"):
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str) and call_id in tool_names:
                    projected_message = {**message, "name": tool_names[call_id]}
            content, added_images = _to_gemini_content(projected_message)
            image_count += added_images
            if content is not None:
                contents.append(content)
        if image_count > _ZEN_MAX_IMAGES_PER_REQUEST:
            raise ProviderError(
                f"Gemini accepts at most {_ZEN_MAX_IMAGES_PER_REQUEST} images per request",
                retryable=False,
            )

        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        generation: dict[str, Any] = {}
        output_limits = [
            request.pop(key)
            for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            if isinstance(request.get(key), int) and not isinstance(request.get(key), bool)
        ]
        if output_limits:
            generation["maxOutputTokens"] = min(output_limits)
        _move_number(request, generation, "temperature", "temperature", minimum=0, maximum=2)
        _move_number(request, generation, "top_p", "topP", minimum=0, maximum=1)
        _move_integer(request, generation, "top_k", "topK", minimum=1)
        _move_integer(request, generation, "seed", "seed")
        _move_number(
            request, generation, "presence_penalty", "presencePenalty", minimum=-2, maximum=2
        )
        _move_number(
            request, generation, "frequency_penalty", "frequencyPenalty", minimum=-2, maximum=2
        )
        stop = request.pop("stop", None)
        if isinstance(stop, str):
            generation["stopSequences"] = [stop]
        elif isinstance(stop, list) and all(isinstance(item, str) for item in stop):
            generation["stopSequences"] = stop
        elif stop is not None:
            raise ProviderError("Gemini stop must be a string or list of strings", retryable=False)

        thinking_effort = request.pop("thinking_effort", None)
        reasoning_effort = request.pop("reasoning_effort", None)
        selected_effort = normalize_thinking_effort(
            thinking_effort if thinking_effort is not None else reasoning_effort
        )
        if selected_effort:
            levels = model_reasoning_levels(self._model_lookup, model_id) or (
                "minimal",
                "low",
                "medium",
                "high",
            )
            mapped = (
                next((level for level in levels if level != "none"), None)
                if selected_effort == "none"
                else closest_supported_effort(selected_effort, levels)
            )
            if mapped is not None:
                generation["thinkingConfig"] = {
                    "includeThoughts": True,
                    "thinkingLevel": mapped,
                }

        response_format = request.pop("response_format", None)
        if response_format is not None:
            _apply_gemini_response_format(generation, response_format)

        tools = request.pop("tools", None)
        if tools:
            if not isinstance(tools, Sequence) or isinstance(tools, str | bytes):
                raise ProviderError("Gemini tools must be a list", retryable=False)
            rendered = render_tool_definitions(tools, profile="omit_strict")
            payload["tools"] = [{"functionDeclarations": rendered}]
        tool_choice = request.pop("tool_choice", None)
        if tool_choice is not None:
            payload["toolConfig"] = {"functionCallingConfig": _gemini_tool_choice(tool_choice)}
        request.pop("parallel_tool_calls", None)
        if generation:
            payload["generationConfig"] = generation
        if request:
            unsupported = ", ".join(sorted(request))
            raise ProviderError(
                f"OpenCode Zen Gemini does not support request parameters: {unsupported}",
                retryable=False,
            )

        encoded_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if encoded_size > _ZEN_INLINE_REQUEST_MAX_BYTES:
            raise ProviderError(
                "OpenCode Zen Gemini inline request exceeds the documented 20 MB limit",
                retryable=False,
            )
        return payload


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection]
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _response_detail(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text}".strip()
