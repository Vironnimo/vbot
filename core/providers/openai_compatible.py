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
    classify_http_status,
    decode_response_json,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.adapter import ModelLookup, ProviderAdapter
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_supported,
    remove_reasoning_kwargs,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.utils.retry import retry_async

# ---------------------------------------------------------------------------
# SSE parsing constants
# ---------------------------------------------------------------------------

SSE_DONE_MARKER = "[DONE]"
CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
OPENAI_REASONING_EFFORTS = {"low", "medium", "high"}
OPENAI_REASONING_EFFORTS_WITH_NONE = {"none", *OPENAI_REASONING_EFFORTS}
OPENAI_NONE_REASONING_PROVIDER_IDS = {"openai"}
OPENAI_REASONING_KEYS = ("reasoning", "reasoning_content", "thinking")
OPENAI_REASONING_META_KEYS = ("encrypted_content", "reasoning_details")
OPENAI_TOOL_FINISH_REASONS = {"tool_calls", "function_call"}
OPENAI_STOP_FINISH_REASONS = {"stop", "length", "content_filter"}
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 0
CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "contextWindow")
MAX_OUTPUT_TOKEN_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "maxOutputTokens",
    "maxCompletionTokens",
)
JSON_MODE_PARAMETER_NAMES = {"response_format", "structured_outputs", "json_mode"}
REASONING_PARAMETER_NAMES = {"reasoning", "include_reasoning", "reasoning_effort"}


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
            context_window=_read_first_optional_int(raw, CONTEXT_WINDOW_KEYS)
            or _read_first_optional_int(architecture, CONTEXT_WINDOW_KEYS)
            or DEFAULT_CONTEXT_WINDOW,
            max_output_tokens=_read_first_optional_int(top_provider, MAX_OUTPUT_TOKEN_KEYS)
            or _read_first_optional_int(raw, MAX_OUTPUT_TOKEN_KEYS)
            or _read_first_optional_int(architecture, MAX_OUTPUT_TOKEN_KEYS),
        )

    async def _build_headers(self) -> dict[str, str]:
        """Build request headers from selected connection auth and extra_headers."""
        token = await self._token_getter()
        headers: dict[str, str] = {
            self._auth_config.header: f"{self._auth_config.prefix}{token}",
        }
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize an OpenAI-compatible response to canonical assistant fields."""
        message = _first_choice_message(response)
        content = message.get("content")
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": content if isinstance(content, str) or content is None else str(content),
            "reasoning": _extract_openai_reasoning(message),
            "reasoning_meta": _extract_openai_reasoning_meta(message),
            "tool_calls": _extract_openai_tool_calls(message),
        }
        usage = _extract_openai_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

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
        return _to_openai_message(message)

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
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [self._format_message(message) for message in messages],
        }
        _apply_openai_tools(payload, request_kwargs)
        _apply_openai_reasoning(
            payload,
            request_kwargs,
            reasoning_supported=self._model_reasoning_supported(model_id),
            supported_efforts=self._supported_reasoning_efforts(),
        )
        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        return payload

    def _model_reasoning_supported(self, model_id: str) -> bool | None:
        return model_reasoning_supported(self._model_lookup, model_id)

    def _supported_reasoning_efforts(self) -> set[str]:
        if self._config.id in OPENAI_NONE_REASONING_PROVIDER_IDS:
            return OPENAI_REASONING_EFFORTS_WITH_NONE
        return OPENAI_REASONING_EFFORTS

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

        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            payload = self._build_payload(messages, model_id, **kwargs)
            try:
                response = await self._client.post(
                    CHAT_COMPLETIONS_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            reason = response.text
            detail = (
                f"{response.status_code} {reason}".strip() if reason else str(response.status_code)
            )
            classify_http_status(response.status_code, detail=detail)
            return dict(decode_response_json(response, "OpenAI-compatible provider"))

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
        headers = await self._build_headers()
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True
        _merge_stream_usage_options(payload)

        async def _connect_stream() -> httpx.Response:
            request = self._client.build_request(
                "POST",
                CHAT_COMPLETIONS_ENDPOINT,
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
                detail = (
                    f"{response.status_code} {error_body}".strip()
                    if error_body
                    else str(response.status_code)
                )
                classify_http_status(response.status_code, detail=detail)
                # classify_http_status always raises for >= 400; this is unreachable
                # but satisfies type checkers.
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        response = await retry_async(_connect_stream)

        tool_call_ids_by_index: dict[int, str] = {}
        seen_done_marker = False

        try:
            async for data in iter_sse_data(response):
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
                    tool_call_ids_by_index,
                ):
                    yield normalized_delta
            if not seen_done_marker:
                raise NetworkError("Stream ended without [DONE] marker")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_ids_by_index: dict[int, str],
    ) -> list[dict[str, Any]]:
        return _normalize_openai_stream_chunk(raw_chunk, tool_call_ids_by_index)


def _normalize_openai_stream_chunk(
    chunk: dict[str, Any],
    tool_call_ids_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    error = chunk.get("error")
    if isinstance(error, dict):
        message = error.get("message") or str(error)
        raise ProviderError(f"Provider stream error: {message}", retryable=False)

    normalized_deltas: list[dict[str, Any]] = []
    for choice in _stream_choices(chunk):
        delta = choice.get("delta", {})
        if isinstance(delta, dict):
            normalized_deltas.extend(_normalize_openai_message_delta(delta, tool_call_ids_by_index))

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            normalized_deltas.append(
                {
                    "type": "finish",
                    "reason": _normalize_openai_finish_reason(
                        finish_reason,
                        has_tool_calls=bool(tool_call_ids_by_index),
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
    tool_call_ids_by_index: dict[int, str],
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
        normalized_deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})

    normalized_deltas.extend(_normalize_openai_tool_call_deltas(delta, tool_call_ids_by_index))
    return normalized_deltas


def _normalize_openai_tool_call_deltas(
    delta: dict[str, Any],
    tool_call_ids_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    raw_tool_calls = delta.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    normalized_deltas: list[dict[str, Any]] = []
    for position, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, dict):
            continue
        tool_call_index = _openai_tool_call_index(raw_tool_call, position)
        tool_call_id = _openai_stream_tool_call_id(
            raw_tool_call, tool_call_index, tool_call_ids_by_index
        )
        function = raw_tool_call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        name_delta = function.get("name")
        arguments_delta = function.get("arguments")
        if not isinstance(name_delta, str):
            name_delta = ""
        if not isinstance(arguments_delta, str):
            arguments_delta = ""
        if not name_delta and not arguments_delta:
            continue
        normalized_deltas.append(
            {
                "type": "tool_call_delta",
                "id": tool_call_id,
                "name_delta": name_delta,
                "arguments_delta": arguments_delta,
            }
        )
    return normalized_deltas


def _openai_tool_call_index(raw_tool_call: dict[str, Any], position: int) -> int:
    index = raw_tool_call.get("index")
    return index if isinstance(index, int) else position


def _openai_stream_tool_call_id(
    raw_tool_call: dict[str, Any],
    index: int,
    tool_call_ids_by_index: dict[int, str],
) -> str:
    existing_id = tool_call_ids_by_index.get(index)
    if existing_id:
        return existing_id
    provider_id = raw_tool_call.get("id")
    if isinstance(provider_id, str) and provider_id:
        tool_call_ids_by_index[index] = provider_id
        return provider_id
    generated_id = f"tool_call_{index}"
    tool_call_ids_by_index[index] = generated_id
    return generated_id


def _normalize_openai_finish_reason(finish_reason: Any, *, has_tool_calls: bool) -> str:
    if finish_reason in OPENAI_TOOL_FINISH_REASONS:
        return "tool_calls"
    if finish_reason in OPENAI_STOP_FINISH_REASONS:
        return "stop"
    return "tool_calls" if has_tool_calls else "stop"


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
        if not isinstance(base64_data, str):
            base64_data = "" if base64_data is None else str(base64_data)
        if not isinstance(media_type, str) or not media_type:
            media_type = "application/octet-stream"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
        }

    if part_type == "text":
        text = part.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}

    return dict(part)


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


def _apply_openai_tools(payload: dict[str, Any], kwargs: dict[str, Any]) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


def _apply_openai_reasoning(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    reasoning_supported: bool | None,
    supported_efforts: set[str],
) -> None:
    thinking_effort = kwargs.pop("thinking_effort", "")
    reasoning_effort = kwargs.pop("reasoning_effort", "")
    if reasoning_supported is False:
        remove_reasoning_kwargs(kwargs, *REASONING_PARAMETER_NAMES)
        return
    supported_effort = closest_supported_effort(
        thinking_effort or reasoning_effort,
        supported_efforts,
    )
    if supported_effort is None:
        return
    if supported_effort == "none" and reasoning_supported is not True:
        return
    payload["reasoning_effort"] = supported_effort


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
    tool_calls: list[dict[str, Any]] = []
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function", {})
        if not isinstance(function, dict):
            continue
        arguments = _parse_tool_arguments(function.get("arguments"))
        if arguments is None:
            continue
        tool_calls.append(
            {
                "id": raw_call["id"],
                "name": function.get("name", ""),
                "arguments": arguments,
            }
        )
    return tool_calls or None


def _parse_tool_arguments(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, dict):
        return dict(arguments)
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        return None
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_openai_reasoning(message: dict[str, Any]) -> str | None:
    for key in OPENAI_REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str):
            return value
    return None


def _extract_openai_reasoning_meta(message: dict[str, Any]) -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    for key in OPENAI_REASONING_META_KEYS:
        if key in message:
            meta[key] = message[key]
    return meta or None


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
    return {
        "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else 0,
        "output_tokens": completion_tokens if isinstance(completion_tokens, int) else 0,
    }


def _extract_stream_usage(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an OpenAI-compatible streaming chunk.

    Yields a usage delta only when the chunk contains a ``usage`` dict
    with at least ``prompt_tokens`` (as int).  Maps
    ``prompt_tokens`` → ``input_tokens`` and
    ``completion_tokens`` → ``output_tokens`` (defaulting to ``0`` if
    absent or not an int).

    Returns ``None`` when the chunk has no usable usage data, so that
    callers can skip yielding anything.
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    if not isinstance(prompt_tokens, int):
        return None
    completion_tokens = usage.get("completion_tokens")
    return {
        "type": "usage",
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens if isinstance(completion_tokens, int) else 0,
    }


def _provider_default_max_tokens(defaults: Mapping[str, Any] | None) -> int:
    if defaults is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    max_tokens = defaults.get("max_tokens")
    if max_tokens is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return int(max_tokens)


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


def _read_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Expected '{key}' to be an integer")
    return value


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
