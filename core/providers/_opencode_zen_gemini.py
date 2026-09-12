"""Opencode zen gemini."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from core.providers._opencode_zen_profiles import (
    _ZEN_GEMINI_MEDIA_TYPES,
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
    ProviderError,
)


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
    for raw_part in part_values:
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
                    "id": _gemini_tool_call_id(function_call, chunk, len(replay_parts) - 1),
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
        "input_tokens": input_tokens,
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
