"""Anthropic provider adapter.

Handles the ``/messages`` endpoint format used by the Anthropic Messages API.
Owns the full wire protocol: message format, authentication, streaming,
and error classification.

Key differences from the OpenAI-compatible adapter:

- Endpoint: ``/messages`` (not ``/chat/completions``)
- System messages are extracted into a top-level ``system`` field
- Auth: ``x-api-key`` header (no ``Bearer`` prefix)
- Required header: ``anthropic-version``
- Content blocks instead of flat ``content`` strings
- Thinking/reasoning via ``thinking`` and ``output_config`` parameters
- Streaming uses ``event:`` + ``data:`` SSE lines (not ``data:`` only)
- Stream ends on ``message_stop`` event (not ``[DONE]``)
- Anthropic-specific error format and status code 529 (overloaded)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.adapter import ProviderAdapter
from core.providers.errors import ProviderError
from core.providers.providers import ProviderConfig
from core.utils.retry import retry_async

# ---------------------------------------------------------------------------
# Anthropic-specific constants
# ---------------------------------------------------------------------------

# Status code 529 is Anthropic-specific: server overloaded.
_HTTP_OVERLOADED = 529

# SSE / API constants
SSE_DATA_PREFIX = "data: "
SSE_EVENT_PREFIX = "event: "
MESSAGES_ENDPOINT = "/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
TEXT_BLOCK_TYPE = "text"
TOOL_USE_BLOCK_TYPE = "tool_use"
THINKING_BLOCK_TYPE = "thinking"
REDACTED_THINKING_BLOCK_TYPE = "redacted_thinking"
REASONING_META_CONTENT_BLOCKS = "content_blocks"
ANTHROPIC_TOOL_STOP_REASONS = {"tool_use"}
ANTHROPIC_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "pause_turn",
    "refusal",
    "stop_sequence",
}


class AnthropicAdapter(ProviderAdapter):
    """Adapter for the Anthropic Messages API.

    Uses the ``/messages`` endpoint with Anthropic's own request and response
    format.  Provider-specific differences (base URL, auth header, extra
    headers, default parameters) come from ``ProviderConfig``.

    Args:
        config: Immutable provider configuration.
        api_key: API key for authentication (sent via the header from config).
    """

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=60.0,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> AnthropicAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Build request headers for the Anthropic API.

        Includes the auth header from provider config, the required
        ``anthropic-version`` header, and any ``extra_headers``.
        """
        headers: dict[str, str] = {
            self._config.auth.header: f"{self._config.auth.prefix}{self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize an Anthropic response to canonical assistant fields."""
        content_blocks = response.get("content", [])
        return {
            "role": "assistant",
            "content": _extract_anthropic_text(content_blocks),
            "reasoning": _extract_anthropic_reasoning(content_blocks),
            "reasoning_meta": _extract_anthropic_reasoning_meta(content_blocks),
            "tool_calls": _extract_anthropic_tool_calls(content_blocks),
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the Anthropic Messages API request payload.

        Extracts system-role messages into the ``system`` field (required by
        the Anthropic API — system messages must not appear in the messages
        array) and assembles model, messages, defaults, and overrides.
        """
        request_kwargs = dict(kwargs)
        system_content: str | list[dict[str, Any]] | None = None
        conversation_messages: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                # Anthropic requires system messages in a separate top-level
                # field, not in the messages array.
                content = message.get("content")
                if isinstance(content, (str, list)):
                    system_content = content
            else:
                conversation_messages.append(message)

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": _to_anthropic_messages(conversation_messages),
        }
        if system_content is not None:
            payload["system"] = system_content
        _apply_anthropic_tools(payload, request_kwargs)
        _apply_anthropic_reasoning(payload, request_kwargs)

        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(request_kwargs)
        return payload

    # ------------------------------------------------------------------
    # Error detail helper (Anthropic-specific)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        """Build an error detail string from an Anthropic error response.

        Parses the Anthropic error response format for richer error messages.
        The Anthropic API returns errors as::

            {"type": "error", "error": {"type": "...", "message": "..."}}

        Args:
            status_code: HTTP response status code.
            response_body: Response body text for context.

        Returns:
            A human-readable detail string combining the status code with
            any structured error information available.
        """
        detail = str(status_code)
        try:
            error_data = json.loads(response_body) if response_body else {}
            error_info = error_data.get("error", {})
            error_type = error_info.get("type", "")
            error_message = error_info.get("message", "")
            if error_type and error_message:
                detail = f"{status_code} ({error_type}): {error_message}"
            elif error_message:
                detail = f"{status_code}: {error_message}"
        except (json.JSONDecodeError, AttributeError):
            if response_body:
                detail = f"{status_code}: {response_body}"
        return detail

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
        """Send a non-streaming request to the Anthropic Messages API.

        Retries on retryable errors (429, 502, 503, 529) via
        ``retry_async``.  Fails immediately on auth errors (401/403).

        Args:
            messages: Conversation messages.  System-role messages are
                automatically extracted into the ``system`` field.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (thinking, output_config, …).

        Returns:
            Parsed response dict from the provider.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            ProviderTimeoutError: Connection / timeout errors.
            ProviderError: Other HTTP errors.
        """

        async def _do_request() -> dict[str, Any]:
            headers = self._build_headers()
            payload = self._build_payload(messages, model_id, **kwargs)
            try:
                response = await self._client.post(
                    MESSAGES_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                raise wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
                raise wrap_network_error(exc) from exc

            detail = self._build_error_detail(response.status_code, response.text)
            classify_http_status(
                response.status_code, extra_retryable={_HTTP_OVERLOADED}, detail=detail
            )
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
        """Send a streaming request to the Anthropic Messages API and yield
        normalized provider-agnostic deltas.

        Anthropic uses ``event:`` and ``data:`` lines in its SSE stream.
        The stream ends on a ``message_stop`` event.  Provider-specific
        stream events are translated into ``content_delta``,
        ``reasoning_delta``, ``tool_call_delta``, ``reasoning_meta``, and
        ``finish`` dictionaries before being yielded.

        Retries the initial connection on retryable errors (429, 502, 503,
        529).  Once the stream is established, yields parsed SSE data
        chunks as dicts until ``message_stop`` is received.

        Args:
            messages: Conversation messages.  System-role messages are
                automatically extracted into the ``system`` field.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (thinking, output_config, …).

        Yields:
            Normalized delta dicts from the SSE event stream.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            ProviderTimeoutError: Connection / timeout errors.
            ProviderError: Other HTTP errors.
        """
        headers = self._build_headers()
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True

        async def _connect_stream() -> httpx.Response:
            request = self._client.build_request(
                "POST",
                MESSAGES_ENDPOINT,
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
                detail = self._build_error_detail(response.status_code, error_body)
                classify_http_status(
                    response.status_code, extra_retryable={_HTTP_OVERLOADED}, detail=detail
                )
                # classify_http_status always raises for >= 400; this is unreachable
                # but satisfies type checkers.
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        response = await retry_async(_connect_stream)
        content_blocks_by_index: dict[int, dict[str, Any]] = {}
        reasoning_meta_blocks: list[dict[str, Any]] = []

        try:
            async for line in response.aiter_lines():
                if line.startswith(SSE_EVENT_PREFIX):
                    # Anthropic sends event type lines (e.g. "event: message_start")
                    # before data lines.  We use the data's own "type" field for
                    # classification, so we just skip the event line here.
                    continue
                if not line.startswith(SSE_DATA_PREFIX):
                    # Skip blank lines, comments, and unknown prefixes
                    continue
                data = line[len(SSE_DATA_PREFIX) :]
                if not data.strip():
                    continue
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    continue
                for normalized_delta in _normalize_anthropic_stream_event(
                    parsed,
                    content_blocks_by_index,
                    reasoning_meta_blocks,
                ):
                    yield normalized_delta
                if parsed.get("type") == "message_stop":
                    break
        finally:
            await response.aclose()


def _normalize_anthropic_stream_event(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
    reasoning_meta_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "content_block_start":
        return _normalize_anthropic_content_block_start(event, content_blocks_by_index)
    if event_type == "content_block_delta":
        return _normalize_anthropic_content_block_delta(event, content_blocks_by_index)
    if event_type == "content_block_stop":
        return _normalize_anthropic_content_block_stop(
            event,
            content_blocks_by_index,
            reasoning_meta_blocks,
        )
    if event_type == "message_delta":
        return _normalize_anthropic_message_delta(event, content_blocks_by_index)
    return []


def _normalize_anthropic_content_block_start(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    content_block = event.get("content_block", {})
    if index is None or not isinstance(content_block, dict):
        return []

    block_type = content_block.get("type")
    block_state: dict[str, Any] = {"type": block_type}
    if block_type == TOOL_USE_BLOCK_TYPE:
        tool_call_id = content_block.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = f"tool_call_{index}"
        name = content_block.get("name")
        block_state["id"] = tool_call_id
        block_state["name"] = name if isinstance(name, str) else ""
        content_blocks_by_index[index] = block_state
        if block_state["name"]:
            return [
                {
                    "type": "tool_call_delta",
                    "id": tool_call_id,
                    "name_delta": block_state["name"],
                    "arguments_delta": "",
                }
            ]
        return []

    if _is_supported_reasoning_block(content_block):
        block_state["block"] = dict(content_block)

    content_blocks_by_index[index] = block_state
    return []


def _normalize_anthropic_content_block_delta(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    delta = event.get("delta", {})
    if index is None or not isinstance(delta, dict):
        return []

    block_state = content_blocks_by_index.get(index, {})
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        text = delta.get("text")
        return [{"type": "content_delta", "text": text}] if isinstance(text, str) and text else []
    if delta_type == "thinking_delta":
        return _normalize_anthropic_thinking_delta(delta, block_state)
    if delta_type == "signature_delta":
        _apply_anthropic_signature_delta(delta, block_state)
        return []
    if delta_type == "input_json_delta":
        return _normalize_anthropic_tool_input_delta(delta, block_state)
    return []


def _normalize_anthropic_content_block_stop(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
    reasoning_meta_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    index = _anthropic_stream_index(event)
    if index is None:
        return []
    block_state = content_blocks_by_index.get(index, {})
    block = block_state.get("block")
    if not isinstance(block, dict) or not _is_supported_reasoning_block(block):
        return []

    reasoning_meta_blocks.append(dict(block))
    return [
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                REASONING_META_CONTENT_BLOCKS: [
                    dict(meta_block) for meta_block in reasoning_meta_blocks
                ]
            },
        }
    ]


def _normalize_anthropic_message_delta(
    event: dict[str, Any],
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    delta = event.get("delta", {})
    if not isinstance(delta, dict):
        return []
    stop_reason = delta.get("stop_reason")
    if stop_reason is None:
        return []
    return [
        {
            "type": "finish",
            "reason": _normalize_anthropic_stop_reason(
                stop_reason,
                has_tool_calls=_has_anthropic_stream_tool_calls(content_blocks_by_index),
            ),
        }
    ]


def _normalize_anthropic_thinking_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> list[dict[str, Any]]:
    thinking = delta.get("thinking")
    if not isinstance(thinking, str) or not thinking:
        return []
    block = block_state.get("block")
    if isinstance(block, dict):
        block["thinking"] = f"{block.get('thinking', '')}{thinking}"
    return [{"type": "reasoning_delta", "text": thinking}]


def _apply_anthropic_signature_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> None:
    signature = delta.get("signature")
    block = block_state.get("block")
    if isinstance(signature, str) and signature and isinstance(block, dict):
        block["signature"] = signature


def _normalize_anthropic_tool_input_delta(
    delta: dict[str, Any],
    block_state: dict[str, Any],
) -> list[dict[str, Any]]:
    if block_state.get("type") != TOOL_USE_BLOCK_TYPE:
        return []
    arguments_delta = delta.get("partial_json")
    if not isinstance(arguments_delta, str):
        arguments_delta = delta.get("input_delta")
    if not isinstance(arguments_delta, str) or not arguments_delta:
        return []
    tool_call_id = block_state.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return []
    return [
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "name_delta": "",
            "arguments_delta": arguments_delta,
        }
    ]


def _normalize_anthropic_stop_reason(stop_reason: Any, *, has_tool_calls: bool) -> str:
    if stop_reason in ANTHROPIC_TOOL_STOP_REASONS:
        return "tool_calls"
    if stop_reason in ANTHROPIC_STOP_REASONS:
        return "stop"
    return "tool_calls" if has_tool_calls else "stop"


def _anthropic_stream_index(event: dict[str, Any]) -> int | None:
    index = event.get("index")
    return index if isinstance(index, int) else None


def _has_anthropic_stream_tool_calls(
    content_blocks_by_index: dict[int, dict[str, Any]],
) -> bool:
    return any(
        block.get("type") == TOOL_USE_BLOCK_TYPE for block in content_blocks_by_index.values()
    )


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "tool":
            pending_tool_results.append(_to_anthropic_tool_result_block(message))
            continue

        if pending_tool_results:
            anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))
            pending_tool_results = []
        anthropic_messages.append(_to_anthropic_message(message))

    if pending_tool_results:
        anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))

    return anthropic_messages


def _to_anthropic_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "tool":
        return _to_anthropic_tool_result_message([_to_anthropic_tool_result_block(message)])
    if role == "assistant":
        return {
            "role": "assistant",
            "content": _to_anthropic_assistant_content(message),
        }
    return {
        "role": role,
        "content": _to_anthropic_text_content(message.get("content", "")),
    }


def _to_anthropic_tool_result_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": blocks}


def _to_anthropic_tool_result_block(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message["tool_call_id"],
        "content": message["content"],
    }


def _to_anthropic_text_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": "" if content is None else str(content)}]


def _to_anthropic_assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    reasoning_meta = message.get("reasoning_meta")
    reasoning_blocks = _reasoning_blocks_from_meta(reasoning_meta)
    if reasoning_blocks:
        content_blocks.extend(reasoning_blocks)

    content = message.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        content_blocks.extend(content)

    for tool_call in message.get("tool_calls") or []:
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tool_call["id"],
                "name": tool_call["name"],
                "input": tool_call.get("arguments", {}),
            }
        )
    return content_blocks


def _reasoning_blocks_from_meta(reasoning_meta: Any) -> list[dict[str, Any]]:
    if not isinstance(reasoning_meta, dict):
        return []
    blocks = reasoning_meta.get(REASONING_META_CONTENT_BLOCKS)
    if not isinstance(blocks, list):
        return []
    return [dict(block) for block in blocks if _is_supported_reasoning_block(block)]


def _is_supported_reasoning_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return block.get("type") in (THINKING_BLOCK_TYPE, REDACTED_THINKING_BLOCK_TYPE)


def _apply_anthropic_tools(payload: dict[str, Any], kwargs: dict[str, Any]) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    payload["tools"] = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        for tool in tools
    ]


def _apply_anthropic_reasoning(payload: dict[str, Any], kwargs: dict[str, Any]) -> None:
    thinking_effort = kwargs.pop("thinking_effort", "")
    if not thinking_effort:
        return
    if thinking_effort == "none":
        payload["thinking"] = {"type": "disabled"}
        return
    if thinking_effort in ANTHROPIC_EFFORTS:
        payload["thinking"] = {"type": "adaptive"}
        if thinking_effort != "minimal":
            payload["output_config"] = {"effort": thinking_effort}
        payload["thinking"]["display"] = "summarized"


def _extract_anthropic_text(content_blocks: Any) -> str | None:
    text_parts = [
        block["text"] for block in _content_blocks(content_blocks) if block.get("type") == "text"
    ]
    return "".join(text_parts) if text_parts else None


def _extract_anthropic_reasoning(content_blocks: Any) -> str | None:
    reasoning_parts = [
        block["thinking"]
        for block in _content_blocks(content_blocks)
        if block.get("type") == THINKING_BLOCK_TYPE and isinstance(block.get("thinking"), str)
    ]
    return "".join(reasoning_parts) if reasoning_parts else None


def _extract_anthropic_reasoning_meta(content_blocks: Any) -> dict[str, Any] | None:
    reasoning_blocks = [
        dict(block)
        for block in _content_blocks(content_blocks)
        if _is_supported_reasoning_block(block)
    ]
    if not reasoning_blocks:
        return None
    return {REASONING_META_CONTENT_BLOCKS: reasoning_blocks}


def _extract_anthropic_tool_calls(content_blocks: Any) -> list[dict[str, Any]] | None:
    tool_calls = [
        {
            "id": block["id"],
            "name": block["name"],
            "arguments": block.get("input", {}),
        }
        for block in _content_blocks(content_blocks)
        if block.get("type") == "tool_use"
    ]
    return tool_calls or None


def _content_blocks(content_blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(content_blocks, list):
        return []
    return [block for block in content_blocks if isinstance(block, dict)]
