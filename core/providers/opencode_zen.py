"""OpenCode Zen multi-protocol provider adapter."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import Model
from core.providers._http_shared import (
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
    TerminalOutcome,
    normalize_tool_call_candidates,
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
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_POLICIES,
    ReasoningReplayPolicy,
    closest_supported_effort,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.providers.token_getter import TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.utils.retry import retry_async

OPENCODE_ZEN_METADATA_KEY = "opencode_zen"
PROTOCOL_METADATA_KEY = "protocol"
REASONING_REPLAY_METADATA_KEY = "reasoning_replay"
PRIVACY_METADATA_KEY = "privacy"
DEPRECATES_AT_METADATA_KEY = "deprecates_at"

PROTOCOL_RESPONSES = "responses"
PROTOCOL_MESSAGES = "messages"
PROTOCOL_CHAT = "chat_completions"
PROTOCOL_GEMINI = "gemini_generate_content"
_KNOWN_PROTOCOLS = frozenset(
    {PROTOCOL_RESPONSES, PROTOCOL_MESSAGES, PROTOCOL_CHAT, PROTOCOL_GEMINI}
)

# OpenCode publishes the endpoint family per exact model id. The public /models
# response contains no route metadata, so a new id stays unavailable until its
# official wire is reviewed instead of being guessed from a name prefix.
_RESPONSES_MODELS = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-nano",
        "grok-4.5",
        "grok-build-0.1",
    }
)
_MESSAGES_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-haiku-4-5",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
    }
)
_GEMINI_MODELS = frozenset(
    {
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro",
        "gemini-3-flash",
    }
)
_CHAT_MODELS = frozenset(
    {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "minimax-m3",
        "minimax-m2.7",
        "minimax-m2.5",
        "glm-5.2",
        "glm-5.1",
        "glm-5",
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "big-pickle",
        "mimo-v2.5-free",
        "laguna-s-2.1-free",
        "ling-3.0-flash-free",
        "north-mini-code-free",
        "nemotron-3-ultra-free",
        "deepseek-v4-flash-free",
    }
)
_PROTOCOL_BY_MODEL = {
    **dict.fromkeys(_RESPONSES_MODELS, PROTOCOL_RESPONSES),
    **dict.fromkeys(_MESSAGES_MODELS, PROTOCOL_MESSAGES),
    **dict.fromkeys(_GEMINI_MODELS, PROTOCOL_GEMINI),
    **dict.fromkeys(_CHAT_MODELS, PROTOCOL_CHAT),
}
_FREE_MODELS = frozenset(
    model_id for model_id in _CHAT_MODELS if model_id == "big-pickle" or model_id.endswith("-free")
)
_IMMINENT_DEPRECATIONS = {
    "claude-opus-4-1": "2026-08-05",
    "kimi-k2.5": "2026-08-05",
    "minimax-m2.5": "2026-08-05",
}
_RETIRED_MODELS = frozenset(
    {
        "gpt-5.2-codex",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5-codex",
        "claude-sonnet-4",
        "glm-5",
    }
)

_ZEN_GEMINI_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "audio/wav",
        "audio/mp3",
        "audio/mpeg",
        "audio/aiff",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/mpeg",
        "video/quicktime",
        "video/avi",
        "video/x-flv",
        "video/mpg",
        "video/webm",
        "video/wmv",
        "video/3gpp",
        "application/pdf",
    }
)
_ZEN_INLINE_REQUEST_MAX_BYTES = 20_000_000
_ZEN_MAX_IMAGES_PER_REQUEST = 3_600

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
        profile: dict[str, Any] = {
            PROTOCOL_METADATA_KEY: protocol,
            REASONING_REPLAY_METADATA_KEY: (
                REASONING_REPLAY_FULL_HISTORY
                if protocol == PROTOCOL_GEMINI
                else REASONING_REPLAY_CURRENT_RUN
            ),
        }
        if model.model_id in _FREE_MODELS:
            profile[PRIVACY_METADATA_KEY] = "free_model_data_collection"
        if deprecates_at := _IMMINENT_DEPRECATIONS.get(model.model_id):
            profile[DEPRECATES_AT_METADATA_KEY] = deprecates_at
        return replace(
            model,
            metadata={**model.metadata, OPENCODE_ZEN_METADATA_KEY: profile},
        )

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        replay = self._profile_value(model_id, REASONING_REPLAY_METADATA_KEY)
        if replay in REASONING_REPLAY_POLICIES:
            return cast(ReasoningReplayPolicy, replay)
        return REASONING_REPLAY_CURRENT_RUN

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
        policy: dict[str, Any] = {
            "reasoning_replay": self._profile_value(
                model_id,
                REASONING_REPLAY_METADATA_KEY,
            )
        }
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
            self._classify_http_status(
                response.status_code,
                detail=_response_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "OpenCode Zen Gemini provider"))

        return await retry_async(_request)

    async def _stream_gemini(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._build_gemini_payload(messages, model_id, kwargs)

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
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                self._classify_http_status(
                    response.status_code,
                    detail=f"{response.status_code} {body}".strip(),
                    response_headers=response.headers,
                )
            return response

        response = await retry_async(_connect)
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


def _to_gemini_content(message: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int]:
    role = message.get("role")
    if role == "assistant":
        replay = message.get("reasoning_meta")
        if isinstance(replay, Mapping) and isinstance(replay.get("gemini_parts"), list):
            replay_parts = [
                copy.deepcopy(part) for part in replay["gemini_parts"] if isinstance(part, Mapping)
            ]
            if replay_parts:
                return {"role": "model", "parts": replay_parts}, 0
        parts: list[dict[str, Any]] = []
        content = message.get("content")
        if isinstance(content, str) and content:
            parts.append({"text": content})
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, Mapping):
                continue
            parts.append(
                {
                    "functionCall": {
                        "id": tool_call.get("id"),
                        "name": tool_call.get("name"),
                        "args": tool_call.get("arguments", {}),
                    }
                }
            )
        return ({"role": "model", "parts": parts} if parts else None), 0
    if role == "tool":
        raw_content = message.get("content", "")
        try:
            parsed_content = (
                json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            )
        except json.JSONDecodeError:
            parsed_content = raw_content
        response = (
            parsed_content if isinstance(parsed_content, Mapping) else {"result": parsed_content}
        )
        return (
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": message.get("tool_call_id"),
                            "name": message.get("name") or "tool",
                            "response": response,
                        }
                    }
                ],
            },
            0,
        )
    if role != "user":
        return None, 0
    user_parts, image_count = _to_gemini_user_parts(message.get("content", ""))
    return {"role": "user", "parts": user_parts}, image_count


def _to_gemini_user_parts(content: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(content, list):
        return [{"text": _content_text(content)}], 0
    parts: list[dict[str, Any]] = []
    image_count = 0
    for block in content:
        if not isinstance(block, Mapping):
            parts.append({"text": _content_text(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"text": _content_text(block.get("text"))})
            continue
        if block_type not in {"media", "document"}:
            raise ProviderError(
                f"Unsupported Gemini content block type: {block_type}",
                retryable=False,
            )
        base64_data = block.get("base64")
        media_type = block.get("media_type")
        if not isinstance(base64_data, str) or not isinstance(media_type, str):
            raise ProviderError(
                "Gemini media blocks require string base64 and media_type fields",
                retryable=False,
            )
        if media_type not in _ZEN_GEMINI_MEDIA_TYPES:
            raise ProviderError(f"Unsupported Gemini media type: {media_type}", retryable=False)
        parts.append({"inlineData": {"mimeType": media_type, "data": base64_data}})
        if media_type.startswith("image/"):
            image_count += 1
    return parts, image_count


def _normalize_gemini_response(response: Mapping[str, Any]) -> dict[str, Any]:
    candidates = response.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    candidate = candidate if isinstance(candidate, Mapping) else {}
    content = candidate.get("content")
    raw_parts = content.get("parts") if isinstance(content, Mapping) else None
    parts = (
        raw_parts
        if isinstance(raw_parts, list)
        else [raw_parts]
        if isinstance(raw_parts, Mapping)
        else []
    )
    replay_parts = [copy.deepcopy(dict(part)) for part in parts if isinstance(part, Mapping)]
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, part in enumerate(replay_parts):
        text = part.get("text")
        if isinstance(text, str):
            (reasoning_parts if part.get("thought") is True else text_parts).append(text)
        function_call = part.get("functionCall")
        if isinstance(function_call, Mapping):
            tool_calls.extend(
                normalize_tool_call_candidates(
                    tool_call_id=_gemini_tool_call_id(function_call, response, index),
                    name=function_call.get("name"),
                    arguments=function_call.get("args"),
                    fallback_id=f"tool_call_{index}",
                )
            )
    result: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
        "reasoning": "".join(reasoning_parts) or None,
        "reasoning_meta": {"gemini_parts": replay_parts} if replay_parts else None,
        "tool_calls": tool_calls,
        "terminal_outcome": _gemini_finish_reason(
            candidate.get("finishReason"),
            has_tool_calls=bool(tool_calls),
            prompt_feedback=response.get("promptFeedback"),
        ),
    }
    usage = _normalize_gemini_usage(response.get("usageMetadata"))
    if usage is not None:
        result["usage"] = usage
    return result


def _normalize_gemini_stream_chunk(
    chunk: Mapping[str, Any],
    replay_parts: list[dict[str, Any]],
    *,
    has_tool_calls: bool,
) -> tuple[list[dict[str, Any]], bool, bool]:
    error = chunk.get("error")
    if isinstance(error, Mapping):
        raise ProviderError(
            f"OpenCode Zen Gemini stream error: {error.get('message') or error}",
            retryable=False,
        )
    deltas: list[dict[str, Any]] = []
    chunk_has_tools = False
    candidates = chunk.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    candidate = candidate if isinstance(candidate, Mapping) else {}
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else []
    part_values = (
        parts if isinstance(parts, list) else [parts] if isinstance(parts, Mapping) else []
    )
    for position, raw_part in enumerate(part_values):
        if not isinstance(raw_part, Mapping):
            continue
        part = copy.deepcopy(dict(raw_part))
        replay_parts.append(part)
        text = part.get("text")
        if isinstance(text, str) and text:
            deltas.append(
                {
                    "type": "reasoning_delta" if part.get("thought") is True else "content_delta",
                    "text": text,
                }
            )
        function_call = part.get("functionCall")
        if isinstance(function_call, Mapping):
            chunk_has_tools = True
            name = function_call.get("name")
            arguments = function_call.get("args")
            deltas.append(
                {
                    "type": "tool_call_delta",
                    "id": _gemini_tool_call_id(function_call, chunk, position),
                    "name_delta": name if isinstance(name, str) else "",
                    "arguments_delta": json.dumps(
                        arguments if arguments is not None else {},
                        separators=(",", ":"),
                    ),
                }
            )
    if replay_parts:
        deltas.append(
            {
                "type": "reasoning_meta",
                "reasoning_meta": {"gemini_parts": copy.deepcopy(replay_parts)},
            }
        )
    finish_reason = candidate.get("finishReason")
    finished = finish_reason is not None
    if finished:
        deltas.append(
            {
                "type": "finish",
                "reason": _gemini_finish_reason(
                    finish_reason,
                    has_tool_calls=has_tool_calls or chunk_has_tools,
                    prompt_feedback=chunk.get("promptFeedback"),
                ),
            }
        )
    usage = _normalize_gemini_usage(chunk.get("usageMetadata"))
    if usage is not None:
        deltas.append({"type": "usage", **usage})
    return deltas, chunk_has_tools, finished


def _normalize_gemini_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, Mapping):
        return None
    input_tokens = _nonnegative_int(raw.get("promptTokenCount"))
    visible_output = _nonnegative_int(raw.get("candidatesTokenCount"))
    reasoning_tokens = _nonnegative_int(raw.get("thoughtsTokenCount"))
    cache_read = _nonnegative_int(raw.get("cachedContentTokenCount"))
    usage = {
        "input_tokens": max(0, input_tokens - cache_read),
        "output_tokens": visible_output + reasoning_tokens,
    }
    if reasoning_tokens:
        usage["reasoning_tokens"] = reasoning_tokens
    if cache_read:
        usage["cache_read_tokens"] = cache_read
    return usage


def _gemini_finish_reason(
    value: Any,
    *,
    has_tool_calls: bool,
    prompt_feedback: Any = None,
) -> TerminalOutcome:
    if isinstance(prompt_feedback, Mapping) and prompt_feedback.get("blockReason"):
        return TERMINAL_OUTCOME_CONTENT_FILTERED
    if value == "STOP":
        return TERMINAL_OUTCOME_TOOL_CALLS if has_tool_calls else TERMINAL_OUTCOME_STOP
    if value == "MAX_TOKENS":
        return TERMINAL_OUTCOME_OUTPUT_TRUNCATED
    if value in {
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }:
        return TERMINAL_OUTCOME_CONTENT_FILTERED
    if value in {
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "MISSING_THOUGHT_SIGNATURE",
        "MALFORMED_RESPONSE",
        "ESCALATION",
    }:
        return TERMINAL_OUTCOME_ERROR
    return TERMINAL_OUTCOME_UNKNOWN


def _gemini_tool_call_id(call: Mapping[str, Any], response: Mapping[str, Any], index: int) -> str:
    call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        return call_id
    response_id = response.get("responseId")
    suffix = response_id if isinstance(response_id, str) and response_id else "response"
    return f"gemini_{suffix}_{index}"


def _gemini_tool_choice(value: Any) -> dict[str, Any]:
    if value == "auto":
        return {"mode": "AUTO"}
    if value == "required":
        return {"mode": "ANY"}
    if value == "none":
        return {"mode": "NONE"}
    if isinstance(value, Mapping):
        function = value.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return {"mode": "ANY", "allowedFunctionNames": [function["name"]]}
    raise ProviderError("Unsupported Gemini tool_choice", retryable=False)


def _apply_gemini_response_format(generation: dict[str, Any], value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ProviderError("Gemini response_format must be an object", retryable=False)
    format_type = value.get("type")
    if format_type == "json_object":
        generation["responseMimeType"] = "application/json"
        return
    if format_type == "json_schema":
        json_schema = value.get("json_schema")
        schema = json_schema.get("schema") if isinstance(json_schema, Mapping) else None
        if not isinstance(schema, Mapping):
            raise ProviderError("Gemini json_schema requires an object schema", retryable=False)
        generation["responseMimeType"] = "application/json"
        generation["responseJsonSchema"] = copy.deepcopy(dict(schema))
        return
    raise ProviderError(f"Unsupported Gemini response_format type: {format_type}", retryable=False)


def _move_number(
    source: dict[str, Any],
    target: dict[str, Any],
    source_key: str,
    target_key: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    value = source.pop(source_key, None)
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not minimum <= value <= maximum
    ):
        raise ProviderError(
            f"Gemini {source_key} must be between {minimum} and {maximum}",
            retryable=False,
        )
    target[target_key] = value


def _move_integer(
    source: dict[str, Any],
    target: dict[str, Any],
    source_key: str,
    target_key: str,
    *,
    minimum: int | None = None,
) -> None:
    value = source.pop(source_key, None)
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (minimum is not None and value < minimum)
    ):
        raise ProviderError(f"Gemini {source_key} must be an integer", retryable=False)
    target[target_key] = value


def _content_text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _response_detail(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text}".strip()


__all__ = ["OpenCodeZenAdapter"]
