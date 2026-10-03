"""OpenCode Zen multi-protocol provider adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, ClassVar, override

import httpx

from core.models.models import Model
from core.providers._http_shared import (
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    iter_sse_events,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._opencode_zen_gemini import (
    ZEN_MAX_IMAGES_PER_REQUEST,
    _apply_gemini_response_format,
    _content_text,
    _gemini_tool_choice,
    _move_integer,
    _move_number,
    _normalize_gemini_response,
    _normalize_gemini_stream_chunk,
    _to_gemini_content,
    gemini_returned_reasoning,
)
from core.providers._responses_profile import take_reasoning_renderer
from core.providers._wire_learning import (
    execute_learning_from_rejections,
    stream_learning_from_rejections,
)
from core.providers.adapter import (
    ModelLookup,
    project_tool_result_content_fallbacks,
)
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRequestTooLargeError,
)
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import OAuthRequestRecovery, TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.providers.wire_profile import Protocol
from core.providers.wire_profiles import WireBinding
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = ["OpenCodeZenAdapter"]

_FREE_TIER_ACCESS_MESSAGE = (
    "This OpenCode Zen free Model is only available inside OpenCode. "
    "Choose a supported Zen Model or an OpenCode Go Model. "
    "Another API key does not enable access to this Model from vBot."
)

# Zen returns allowance exhaustion with HTTP 429 and entitlement failures with
# HTTP 401; these markers separate them from throttling and bad credentials.
_PERMANENT_429_MARKERS = (
    "freeusagelimiterror",
    "gousagelimiterror",
    "blackusagelimiterror",
    "monthly limit",
    "weekly limit",
    "usage limit",
    "quota exceeded",
)

_NON_AUTH_401_MARKERS = (
    "creditserror",
    "monthlylimiterror",
    "userlimiterror",
    "modelerror",
)

_AUTH_401_MARKERS = ("autherror", "invalid api key", "missing api key")


def _classify_zen_status(
    status_code: int,
    *,
    detail: str,
    response_headers: httpx.Headers,
    extra_retryable: set[int] | None = None,
) -> None:
    normalized = detail.casefold()
    if status_code == 403 and "freetiererror" in normalized:
        error = ProviderError(_FREE_TIER_ACCESS_MESSAGE, retryable=False)
        error.status_code = status_code
        raise error
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
        extra_retryable=extra_retryable,
        detail=detail,
        response_headers=response_headers,
    )


class _OpenCodeZenMessagesAdapter(AnthropicCompatibleAdapter):
    """Zen's Anthropic Messages route with Zen error semantics."""

    @staticmethod
    @override
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        # Zen's error name/type determines entitlement vs authentication even
        # when the gateway omits the human-readable message.
        return f"{status_code} {response_body}".strip()

    @override
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
            extra_retryable=self._extra_retryable_statuses,
        )


class OpenCodeZenAdapter(OpenAIAdapter):
    """Route OpenCode Zen Models across its four official wire protocols.

    The wire profile (``resources/wire/opencode-zen.json``) routes each Model
    to Chat Completions or Responses (the inherited codecs), Anthropic Messages
    (an inner Messages adapter sharing this Adapter's client and wire
    profiles) or Gemini ``generateContent``, and admits only reviewed Models:
    free Models are restricted to OpenCode itself and retired Models are
    refused before any request.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = (
        "chat_completions",
        "messages",
        "responses",
        "gemini",
    )

    @classmethod
    @override
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Keep every entry of Zen's Model listing."""

        del cls, raw, connection
        return True

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
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
        )
        self._messages.bind_wire_profiles(self.wire)

    @override
    def bind_wire_profiles(self, binding: WireBinding) -> None:
        super().bind_wire_profiles(binding)
        self._messages.bind_wire_profiles(binding)

    @override
    async def aclose(self) -> None:
        await self._messages.aclose()
        await super().aclose()

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one listed Model as a plain OpenAI-compatible entry.

        Discovery drops a Model the wire profile does not admit once the
        models.dev protocol hint is projected.
        """

        return OpenAICompatibleAdapter.normalize_catalog_entry(raw, defaults)

    @override
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

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return await self._messages.send(messages, model_id=model_id, **kwargs)
        if protocol == "gemini":
            return await self._send_gemini(messages, model_id=model_id, **kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return self._messages.stream(messages, model_id=model_id, **kwargs)
        if protocol == "gemini":
            return self._stream_gemini(messages, model_id=model_id, **kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    @override
    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self.wire_profile(model_id).protocol
            if protocol == "messages":
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == "gemini":
                return _normalize_gemini_response(response)
            return super().normalize_response(response, model_id=model_id)
        if "candidates" in response or "promptFeedback" in response:
            return _normalize_gemini_response(response)
        if response.get("type") == "message":
            return self._messages.normalize_response(response)
        return super().normalize_response(response)

    @override
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
        auth_recovery = self._gemini_auth_recovery()

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

        reply = await execute_learning_from_rejections(
            lambda: auth_recovery.run(lambda: retry_async(_request)),
            payload,
            rebuild=lambda: self._build_gemini_payload(messages, model_id, kwargs),
            wire=self.wire,
            model_id=model_id,
            provider_label=self._config.id,
        )
        if gemini_returned_reasoning(reply):
            self.wire.observe_reasoning_returned(model_id)
        return reply

    async def _stream_gemini(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        payload = self._build_gemini_payload(messages, model_id, kwargs)
        auth_recovery = self._gemini_auth_recovery()
        async with aclosing(
            stream_learning_from_rejections(
                lambda: self._gemini_stream_deltas(payload, model_id, auth_recovery),
                payload,
                rebuild=lambda: self._build_gemini_payload(messages, model_id, kwargs),
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def _gemini_stream_deltas(
        self, payload: dict[str, Any], model_id: str, auth_recovery: OAuthRequestRecovery
    ) -> AsyncGenerator[dict[str, Any]]:
        def _handle_error_status(status: int, body: str, headers: httpx.Headers) -> None:
            self._classify_http_status(
                status,
                detail=f"{status} {body}".strip(),
                response_headers=headers,
            )

        response = await connect_streaming_with_retry(
            self._client,
            f"/models/{model_id}:streamGenerateContent?alt=sse",
            payload,
            build_headers=self._gemini_headers,
            handle_error_status=_handle_error_status,
            auth_recovery=auth_recovery,
        )
        replay_parts: list[dict[str, Any]] = []
        seen_finish = False
        has_tool_calls = False
        try:
            async for event in iter_sse_events(response):
                data = event.data
                if data is None:
                    # Every event without data is a transport comment.
                    yield {"type": "heartbeat"}
                    continue
                raw = parse_sse_json_data(data, context="OpenCode Zen Gemini provider")
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

    def _gemini_auth_recovery(self) -> OAuthRequestRecovery:
        return OAuthRequestRecovery(
            self._token_getter, AuthConfig(header="x-goog-api-key", prefix="")
        )

    def _build_gemini_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Render one Gemini ``generateContent`` request from the wire profile.

        Raises:
            ProviderError: (not retryable, nothing sent) when the profile does
                not admit the Model, a media type or parameter is not carried,
                or the request holds too many images.
            ProviderRequestTooLargeError: when the request exceeds the profile's
                request body limit.
        """

        self._refuse_unadmitted_model(model_id)
        profile = self.wire_profile(model_id)
        request = {key: value for key, value in kwargs.items() if value is not None}
        reasoning_renderer = take_reasoning_renderer(profile, request)
        if profile.request.parameters:
            # Configured or learned parameter rules shape the request
            # parameters before they move into ``generationConfig``.
            thinking: dict[str, Any] = {}
            reasoning_renderer(thinking)
            profile.request.shape_parameters(
                request, reasoning_active=bool(thinking), provider_label=self._config.name
            )
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
            content, added_images = _to_gemini_content(projected_message, profile.media.types)
            image_count += added_images
            if content is not None:
                contents.append(content)
        if image_count > ZEN_MAX_IMAGES_PER_REQUEST:
            raise ProviderError(
                f"Gemini accepts at most {ZEN_MAX_IMAGES_PER_REQUEST} images per request",
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
        reasoning_renderer(payload)
        if request:
            unsupported = ", ".join(sorted(request))
            raise ProviderError(
                f"OpenCode Zen Gemini does not support request parameters: {unsupported}",
                retryable=False,
            )

        limit = self.request_body_limit(model_id)
        if limit is not None:
            encoded_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            if encoded_size > limit:
                raise ProviderRequestTooLargeError(encoded_size, limit)
        return payload


def _response_detail(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text}".strip()
