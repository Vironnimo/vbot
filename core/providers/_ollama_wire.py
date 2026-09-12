"""Ollama wire."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.providers._ollama_constants import (
    _OLLAMA_TOOL_DONE_REASONS,
)
from core.providers.adapter import (
    normalize_tool_call_candidates,
    project_tool_result_content_fallbacks,
)
from core.providers.errors import ProviderError


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _to_ollama_message(message) for message in project_tool_result_content_fallbacks(messages)
    ]


def _to_ollama_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "tool":
        tool_message = {
            "role": "tool",
            "content": _flatten_text_content(message.get("content", "")),
            "tool_call_id": message.get("tool_call_id", ""),
        }
        tool_name = message.get("name")
        if isinstance(tool_name, str) and tool_name:
            # Native Ollama supports both fields. ``tool_name`` is the
            # documented template-facing identity; ``tool_call_id`` preserves
            # exact pairing when a Model returns ids.
            tool_message["tool_name"] = tool_name
        return tool_message
    if role == "assistant":
        return _to_ollama_assistant_message(message)

    content, images = _split_content_blocks(message.get("content", ""))
    wire_message: dict[str, Any] = {"role": role, "content": content}
    if images:
        wire_message["images"] = images
    return wire_message


def _to_ollama_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    wire_message: dict[str, Any] = {
        "role": "assistant",
        "content": _flatten_text_content(message.get("content") or ""),
    }
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        # Ollama round-trips visible thinking text via the ``thinking`` field.
        wire_message["thinking"] = reasoning
    tool_calls = message.get("tool_calls")
    if tool_calls:
        wire_message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "function": {
                    "name": tool_call["name"],
                    # Ollama's wire takes arguments as a JSON object, matching
                    # the canonical dict — no string encoding (unlike OpenAI).
                    "arguments": tool_call.get("arguments", {}),
                },
            }
            for tool_call in tool_calls
        ]
    return wire_message


def _split_content_blocks(content: Any) -> tuple[str, list[str]]:
    """Split canonical content into flat text plus a base64 image list.

    Ollama carries images as a per-message ``images`` array of bare base64
    strings, separate from the text content.
    """

    if not isinstance(content, list):
        return ("" if content is None else str(content), [])

    text_parts: list[str] = []
    images: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                text_parts.append(str(text))
        elif block_type == "media":
            base64_data = block.get("base64")
            media_type = block.get("media_type")
            if not isinstance(base64_data, str) or not isinstance(media_type, str):
                raise ProviderError(
                    "media content block requires string base64 and media_type fields",
                    retryable=False,
                )
            if not media_type.startswith("image/"):
                raise ProviderError(
                    f"Ollama adapter supports only image media blocks; received {media_type}",
                    retryable=False,
                )
            images.append(base64_data)
        else:
            raise ProviderError(
                f"Ollama adapter does not support '{block_type}' content blocks",
                retryable=False,
            )
    return ("\n\n".join(text_parts), images)


def _flatten_text_content(content: Any) -> str:
    text, _images = _split_content_blocks(content)
    return text


def _extract_ollama_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]] | None:
    if not raw_tool_calls:
        return None
    raw_call_values = raw_tool_calls if isinstance(raw_tool_calls, list) else [raw_tool_calls]
    tool_calls: list[dict[str, Any]] = []
    for position, raw_call_value in enumerate(raw_call_values):
        raw_call = raw_call_value if isinstance(raw_call_value, Mapping) else {}
        function_value = raw_call.get("function")
        function = function_value if isinstance(function_value, Mapping) else {}
        tool_calls.extend(
            normalize_tool_call_candidates(
                tool_call_id=raw_call.get("id"),
                name=function.get("name"),
                arguments=function.get("arguments"),
                fallback_id=f"tool_call_{position}",
            )
        )
    return tool_calls or None


def _ollama_stream_tool_calls(raw_tool_calls: Any, *, start_index: int = 0) -> list[dict[str, Any]]:
    """Preserve malformed wire values so Chat can reject rather than dispatch them."""

    if not raw_tool_calls:
        return []
    raw_call_values = raw_tool_calls if isinstance(raw_tool_calls, list) else [raw_tool_calls]
    tool_calls: list[dict[str, Any]] = []
    for position, raw_call_value in enumerate(raw_call_values):
        raw_call = raw_call_value if isinstance(raw_call_value, Mapping) else {}
        function_value = raw_call.get("function")
        function = function_value if isinstance(function_value, Mapping) else {}
        tool_call_id = raw_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = f"tool_call_{start_index + position}"
        name = function.get("name")
        arguments = function.get("arguments")
        tool_calls.append(
            {
                "id": tool_call_id,
                "name": name if isinstance(name, str) else "",
                "arguments": arguments if arguments is not None else {},
            }
        )
    return tool_calls


def _extract_ollama_usage(response: Mapping[str, Any]) -> dict[str, Any] | None:
    input_tokens = response.get("prompt_eval_count")
    output_tokens = response.get("eval_count")
    usage: dict[str, Any] = {}
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
        usage["input_tokens"] = input_tokens
    if (
        isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        usage["output_tokens"] = output_tokens
    return usage or None


def _normalize_ollama_done_reason(done_reason: Any, *, has_tool_calls: bool) -> str:
    if done_reason in _OLLAMA_TOOL_DONE_REASONS or has_tool_calls:
        return "tool_calls"
    return "stop"


def _build_error_detail(status_code: int, response_body: str = "") -> str:
    """Build an error detail from Ollama's ``{"error": "..."}`` response shape."""

    detail = str(status_code)
    try:
        error_data = json.loads(response_body) if response_body else {}
        error_message = error_data.get("error", "") if isinstance(error_data, dict) else ""
        if error_message:
            detail = f"{status_code}: {error_message}"
    except json.JSONDecodeError:
        if response_body:
            detail = f"{status_code}: {response_body}"
    return detail
