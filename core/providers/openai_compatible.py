"""OpenAI-compatible provider adapter.

Handles the ``/chat/completions`` endpoint format used by OpenAI, OpenRouter,
Groq, Together, and other providers that follow the OpenAI API convention.
Differences in base URL, auth headers, and default parameters are expressed
through ``ProviderConfig`` — no subclassing needed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.adapter import ProviderAdapter
from core.providers.errors import ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.utils.retry import retry_async

# ---------------------------------------------------------------------------
# SSE parsing constants
# ---------------------------------------------------------------------------

SSE_DATA_PREFIX = "data: "
SSE_DONE_MARKER = "[DONE]"
CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
OPENAI_REASONING_EFFORTS = {"low", "medium", "high"}
OPENROUTER_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
OPENAI_REASONING_KEYS = ("reasoning", "reasoning_content")
OPENAI_REASONING_META_KEYS = ("encrypted_content", "reasoning_details")
OPENAI_VISIBLE_REASONING_DETAIL_TYPES = {"reasoning.text", "reasoning.summary"}
OPENAI_TOOL_FINISH_REASONS = {"tool_calls", "function_call"}
OPENAI_STOP_FINISH_REASONS = {"stop", "length", "content_filter"}
STREAM_USAGE_PROVIDER_IDS = {"openai", "openrouter"}
COPILOT_REASONING_MODEL_PREFIX = "gpt-5"


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
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        self._client = httpx.AsyncClient(
            base_url=base_url or config.base_url,
            timeout=60.0,
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
        reasoning_meta = _extract_openai_reasoning_meta(message)
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": content if isinstance(content, str) or content is None else str(content),
            "reasoning": _extract_openai_reasoning(message, reasoning_meta=reasoning_meta),
            "reasoning_meta": reasoning_meta,
            "tool_calls": _extract_openai_tool_calls(message),
        }
        usage = _extract_openai_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the request payload with model, messages, defaults, and overrides."""
        request_kwargs = dict(kwargs)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [_to_openai_message(message) for message in messages],
        }
        _apply_openai_tools(payload, request_kwargs)
        _apply_openai_reasoning(payload, self._config.id, request_kwargs)
        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        _apply_openai_token_limit(payload, self._config.id, model_id)
        return payload

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
            ProviderTimeoutError: Connection / timeout errors (retried, then raised).
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
            except httpx.TimeoutException as exc:
                raise wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
                raise wrap_network_error(exc) from exc

            reason = response.text
            detail = (
                f"{response.status_code} {reason}".strip() if reason else str(response.status_code)
            )
            classify_http_status(response.status_code, detail=detail)
            return dict(response.json())

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
            ProviderTimeoutError: Connection / timeout errors (retried, then raised).
            ProviderError: Other HTTP errors.
        """
        headers = await self._build_headers()
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True
        _apply_openai_stream_options(payload, self._config.id)

        async def _connect_stream() -> httpx.Response:
            request = self._client.build_request(
                "POST",
                CHAT_COMPLETIONS_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
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

        try:
            async for line in response.aiter_lines():
                if not line.startswith(SSE_DATA_PREFIX):
                    continue
                data = line[len(SSE_DATA_PREFIX) :]
                if data.strip() == SSE_DONE_MARKER:
                    break
                raw_chunk = json.loads(data)
                for normalized_delta in _normalize_openai_stream_chunk(
                    raw_chunk,
                    tool_call_ids_by_index,
                ):
                    yield normalized_delta
        finally:
            await response.aclose()


def _normalize_openai_stream_chunk(
    chunk: dict[str, Any],
    tool_call_ids_by_index: dict[int, str],
) -> list[dict[str, Any]]:
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

    reasoning_meta = _extract_openai_reasoning_meta(delta)
    reasoning = _extract_openai_reasoning(delta, reasoning_meta=reasoning_meta)
    if reasoning:
        normalized_deltas.append({"type": "reasoning_delta", "text": reasoning})

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

    return {
        "role": role,
        "content": message.get("content", ""),
    }


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
    provider_id: str,
    kwargs: dict[str, Any],
) -> None:
    thinking_effort = kwargs.pop("thinking_effort", "")
    if not thinking_effort or thinking_effort == "none":
        return
    if provider_id == "openrouter":
        if thinking_effort in OPENROUTER_REASONING_EFFORTS:
            payload["reasoning"] = {"effort": thinking_effort}
            payload["include_reasoning"] = True
        return
    if thinking_effort in OPENAI_REASONING_EFFORTS:
        payload["reasoning_effort"] = thinking_effort


def _apply_openai_token_limit(
    payload: dict[str, Any],
    provider_id: str,
    model_id: str,
) -> None:
    if provider_id != "github-copilot":
        return
    if not model_id.startswith(COPILOT_REASONING_MODEL_PREFIX):
        return
    if "max_completion_tokens" in payload or "max_tokens" not in payload:
        return
    payload["max_completion_tokens"] = payload.pop("max_tokens")


def _apply_openai_stream_options(payload: dict[str, Any], provider_id: str) -> None:
    if provider_id not in STREAM_USAGE_PROVIDER_IDS:
        return
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
        function = raw_call.get("function", {})
        arguments = _parse_tool_arguments(function.get("arguments"))
        tool_calls.append(
            {
                "id": raw_call["id"],
                "name": function.get("name", ""),
                "arguments": arguments,
            }
        )
    return tool_calls


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_openai_reasoning(
    message: dict[str, Any],
    *,
    reasoning_meta: dict[str, Any] | None = None,
) -> str | None:
    for key in OPENAI_REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str):
            return value
    if reasoning_meta is None:
        reasoning_meta = _extract_openai_reasoning_meta(message)
    return _extract_visible_reasoning_from_meta(reasoning_meta)


def _extract_visible_reasoning_from_meta(reasoning_meta: dict[str, Any] | None) -> str | None:
    if not isinstance(reasoning_meta, dict):
        return None
    reasoning_details = reasoning_meta.get("reasoning_details")
    if not isinstance(reasoning_details, list):
        return None
    visible_parts = [
        text
        for item in reasoning_details
        for text in _extract_visible_reasoning_texts(item)
        if text
    ]
    if not visible_parts:
        return None
    return "".join(visible_parts)


def _extract_visible_reasoning_texts(detail: Any) -> list[str]:
    if not isinstance(detail, dict):
        return []
    detail_type = detail.get("type")
    if detail_type not in OPENAI_VISIBLE_REASONING_DETAIL_TYPES:
        return []
    if detail_type == "reasoning.text":
        text = detail.get("text")
        return [text] if isinstance(text, str) else []
    summary = detail.get("summary")
    if not isinstance(summary, list):
        return []
    return [
        text
        for summary_item in summary
        if isinstance(summary_item, dict)
        for text in [summary_item.get("text")]
        if isinstance(text, str)
    ]


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
