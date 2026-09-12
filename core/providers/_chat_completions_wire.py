"""Chat Completions message serialization and response normalization."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.providers._chat_completions_constants import (
    _OPENAI_INPUT_AUDIO_FORMATS,
    OPENAI_ERROR_FINISH_REASONS,
    OPENAI_NATIVE_TRANSPORT_FINISH_REASONS,
    OPENAI_REASONING_KEYS,
    OPENAI_REASONING_META_KEYS,
    OPENAI_TOOL_FINISH_REASONS,
)
from core.providers.adapter import (
    TERMINAL_OUTCOME_CONTENT_FILTERED,
    TERMINAL_OUTCOME_ERROR,
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TerminalOutcome,
    normalize_tool_call_candidates,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
)
from core.providers.reasoning import (
    normalize_thinking_effort,
    reasoning_token_count,
)
from core.providers.tool_schema import ToolSchemaProfile, render_tool_definitions


def _openai_concealed_transport_failure(
    finish_reason: Any,
    native_finish_reason: Any,
) -> NetworkError | ProviderError | None:
    """Return a retryable error when ``stop`` conceals an upstream transport drop.

    Observed 2026-08-24 on OpenRouter ``stealth/ox-alpha``: HTTP 200 SSE with
    empty ``delta.content``, ``finish_reason=stop``, and
    ``native_finish_reason=network_error``. Treating that as a completed stop
    makes Chat try to persist an empty Assistant turn and die on validation,
    with no stream restart.
    """

    if (
        finish_reason != "stop"
        or native_finish_reason not in OPENAI_NATIVE_TRANSPORT_FINISH_REASONS
    ):
        return None
    if native_finish_reason == "network_error":
        return NetworkError("Provider stream ended with native_finish_reason=network_error")
    return ProviderError(
        "Provider stream ended with native_finish_reason=server_error",
        retryable=True,
    )


def _normalize_openai_finish_reason(
    finish_reason: Any,
    *,
    has_tool_calls: bool,
) -> TerminalOutcome:
    if finish_reason in OPENAI_TOOL_FINISH_REASONS:
        return TERMINAL_OUTCOME_TOOL_CALLS
    if finish_reason == "stop":
        return TERMINAL_OUTCOME_TOOL_CALLS if has_tool_calls else TERMINAL_OUTCOME_STOP
    if finish_reason == "length":
        return TERMINAL_OUTCOME_OUTPUT_TRUNCATED
    if finish_reason == "content_filter":
        return TERMINAL_OUTCOME_CONTENT_FILTERED
    if finish_reason in OPENAI_ERROR_FINISH_REASONS:
        return TERMINAL_OUTCOME_ERROR
    return TERMINAL_OUTCOME_UNKNOWN


def _extract_openai_terminal_outcome(
    response: dict[str, Any],
    *,
    has_tool_calls: bool,
) -> TerminalOutcome:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return TERMINAL_OUTCOME_UNKNOWN
    choice = choices[0]
    transport_failure = _openai_concealed_transport_failure(
        choice.get("finish_reason"),
        choice.get("native_finish_reason"),
    )
    if transport_failure is not None:
        raise transport_failure
    return _normalize_openai_finish_reason(
        choice.get("finish_reason"),
        has_tool_calls=has_tool_calls,
    )


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
        if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
            raise ProviderError(
                "media content block requires string base64 and media_type fields",
                retryable=False,
            )
        if media_type.startswith("image/"):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
            }
        audio_format = _OPENAI_INPUT_AUDIO_FORMATS.get(media_type)
        if audio_format is not None:
            return {
                "type": "input_audio",
                "input_audio": {"data": base64_data, "format": audio_format},
            }
        raise ProviderError(
            f"unsupported media type for OpenAI-compatible wire: {media_type}",
            retryable=False,
        )

    if part_type == "document":
        return _to_openai_file_part(part)

    if part_type == "text":
        text = part.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}

    return dict(part)


def _to_openai_file_part(part: dict[str, Any]) -> dict[str, Any]:
    """Translate a canonical document block into an OpenAI Chat Completions ``file`` part.

    Wire shape verified against the OpenAI Chat Completions file-input API: the
    bytes ride as a ``data:<mime>;base64,...`` URL under ``file_data`` with the
    original ``filename``. Declaring which adapters' wires actually carry
    documents stays in ``wire_media_support`` — this is encoding only.
    """
    base64_data = part.get("base64")
    media_type = part.get("media_type")
    filename = part.get("filename")
    if (
        not isinstance(base64_data, str)
        or not isinstance(media_type, str)
        or not media_type
        or not isinstance(filename, str)
        or not filename
    ):
        raise ProviderError(
            "document content block requires string base64, media_type, and filename fields",
            retryable=False,
        )
    return {
        "type": "file",
        "file": {
            "filename": filename,
            "file_data": f"data:{media_type};base64,{base64_data}",
        },
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
    if openai_message.get("content") is None and "tool_calls" not in openai_message:
        openai_message["content"] = ""
    _apply_openai_reasoning_meta(openai_message, message.get("reasoning_meta"))
    return openai_message


def _apply_openai_tools(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    profile: ToolSchemaProfile,
) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    rendered = render_tool_definitions(
        tools,
        profile=profile,
    )
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
                **({"strict": tool["strict"]} if isinstance(tool.get("strict"), bool) else {}),
            },
        }
        for tool in rendered
    ]


def _selected_thinking_effort(kwargs: Mapping[str, Any]) -> str:
    """Return the agent-selected reasoning effort from request kwargs.

    Mirrors ``_apply_openai_reasoning``'s precedence (``thinking_effort`` wins
    over a raw ``reasoning_effort``) but does not mutate kwargs, so it can read
    the selection before the payload builder consumes it for the observability
    signals. Returns the canonical effort, or an empty string when none was set.
    """
    thinking_effort = kwargs.get("thinking_effort") or ""
    reasoning_effort = kwargs.get("reasoning_effort") or ""
    return normalize_thinking_effort(thinking_effort or reasoning_effort)


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


def _extract_openai_reasoning(
    message: dict[str, Any], *, preferred_field: str | None = None
) -> str | None:
    """Return the visible reasoning text from an assistant message.

    When ``preferred_field`` names a visible-text reasoning field present as a
    string on the message, it wins; otherwise the default key scan
    (``OPENAI_REASONING_KEYS``) applies. A ``preferred_field`` that is actually a
    meta field (e.g. ``reasoning_details``) carries no visible text, so it is
    ignored here and surfaces through :func:`_extract_openai_reasoning_meta`.
    """

    if preferred_field is not None and preferred_field not in OPENAI_REASONING_META_KEYS:
        value = message.get(preferred_field)
        if isinstance(value, str) and value:
            return value
    for key in OPENAI_REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_openai_reasoning_meta(
    message: dict[str, Any], *, preferred_field: str | None = None
) -> dict[str, Any] | None:
    """Return the opaque reasoning-meta fields from an assistant message.

    The default meta keys (``OPENAI_REASONING_META_KEYS``) are always collected;
    a ``preferred_field`` that names a meta field not already in that set is also
    collected when present, so a catalog-named meta field is preserved for replay
    even if it is not a hardcoded default.
    """

    meta: dict[str, Any] = {}
    for key in OPENAI_REASONING_META_KEYS:
        if key in message:
            meta[key] = message[key]
    if (
        preferred_field is not None
        and preferred_field not in OPENAI_REASONING_KEYS
        and preferred_field not in meta
        and preferred_field in message
    ):
        meta[preferred_field] = message[preferred_field]
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
    normalized: dict[str, int] = {}
    if isinstance(prompt_tokens, int):
        normalized["input_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        normalized["output_tokens"] = completion_tokens
    cache_read_tokens = _openai_cached_prompt_tokens(usage)
    if cache_read_tokens is not None:
        normalized["cache_read_tokens"] = cache_read_tokens
    cache_write_tokens = _openai_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        normalized["cache_write_tokens"] = cache_write_tokens
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        normalized["reasoning_tokens"] = reasoning_tokens
    return normalized


def _extract_stream_usage(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an OpenAI-compatible streaming chunk.

    Yields a usage delta only when the chunk contains a ``usage`` dict
    with at least one integer primary token counter. Maps ``prompt_tokens``
    → ``input_tokens`` and ``completion_tokens`` → ``output_tokens``;
    an absent counter remains absent for Chat to estimate independently.

    Returns ``None`` when the chunk has no usable usage data, so that
    callers can skip yielding anything.
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    has_input = isinstance(prompt_tokens, int)
    has_output = isinstance(completion_tokens, int)
    if not has_input and not has_output:
        return None
    delta: dict[str, Any] = {"type": "usage"}
    if has_input:
        delta["input_tokens"] = prompt_tokens
    if has_output:
        delta["output_tokens"] = completion_tokens
    cache_read_tokens = _openai_cached_prompt_tokens(usage)
    if cache_read_tokens is not None:
        delta["cache_read_tokens"] = cache_read_tokens
    cache_write_tokens = _openai_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        delta["cache_write_tokens"] = cache_write_tokens
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        delta["reasoning_tokens"] = reasoning_tokens
    return delta


def _openai_cached_prompt_tokens(usage: dict[str, Any]) -> int | None:
    """Read cache-hit tokens from common OpenAI-compatible response shapes.

    Cached tokens are a subset of ``prompt_tokens`` on the OpenAI wire,
    so no input-token adjustment is needed.
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached_tokens = _optional_non_negative_usage_int(details.get("cached_tokens"))
        if cached_tokens is not None:
            return cached_tokens
    for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens"):
        cached_tokens = _optional_non_negative_usage_int(usage.get(key))
        if cached_tokens is not None:
            return cached_tokens
    return None


def _openai_cache_write_tokens(usage: dict[str, Any]) -> int | None:
    """Read cache-creation tokens from common compatible response shapes."""
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for key in ("cache_write_tokens", "cache_creation_tokens"):
            cache_write_tokens = _optional_non_negative_usage_int(details.get(key))
            if cache_write_tokens is not None:
                return cache_write_tokens
    cache_write_tokens = _optional_non_negative_usage_int(usage.get("cache_creation_input_tokens"))
    if cache_write_tokens is not None:
        return cache_write_tokens
    return None


def _optional_non_negative_usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
