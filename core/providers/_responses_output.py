"""Responses output."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers._responses_values import (
    RESPONSES_RESPONSE_OUTPUT_META_KEY,
    _function_call_arguments,
    _function_call_name,
    _joined_or_none,
    _response_output_items,
)
from core.providers.adapter import (
    normalize_tool_call_candidates,
)
from core.providers.reasoning import reasoning_token_count


def normalize_responses_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a non-streaming Responses result to canonical assistant fields."""

    output_items = _response_output_items(response.get("output"))
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": _joined_or_none(_extract_output_text_parts(output_items)),
        "reasoning": _joined_or_none(_extract_reasoning_parts(output_items)),
        "reasoning_meta": _extract_reasoning_meta(response, output_items),
        "tool_calls": _extract_function_calls(output_items),
    }
    phase = _assistant_phase_from_output(output_items)
    if phase is not None:
        normalized["phase"] = phase
    usage = _extract_responses_usage(response.get("usage"))
    if usage is not None:
        normalized["usage"] = usage
    return normalized


def _extract_output_text_parts(output_items: list[Mapping[str, Any]]) -> list[str]:
    parts: list[str] = []
    for item in output_items:
        if item.get("type") == "message":
            parts.extend(_content_text_parts(item.get("content"), {"output_text", "text"}))
        elif item.get("type") in {"output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return parts


def _extract_reasoning_parts(output_items: list[Mapping[str, Any]]) -> list[str]:
    parts: list[str] = []
    for item in output_items:
        if item.get("type") != "reasoning":
            continue
        parts.extend(_content_text_parts(item.get("summary"), {"summary_text", "text"}))
        parts.extend(
            _content_text_parts(
                item.get("content"),
                {"summary_text", "reasoning_text", "output_text", "text"},
            )
        )
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return parts


def _content_text_parts(content: Any, allowed_types: set[str]) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") not in allowed_types:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return parts


def _extract_function_calls(output_items: list[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    tool_calls: list[dict[str, Any]] = []
    for position, item in enumerate(output_items):
        if item.get("type") != "function_call":
            continue
        tool_calls.extend(
            normalize_tool_call_candidates(
                tool_call_id=item.get("call_id") or item.get("id"),
                name=_function_call_name(item),
                arguments=_function_call_arguments(item),
                fallback_id=f"tool_call_{position}",
            )
        )
    return tool_calls or None


def _extract_reasoning_meta(
    response: Mapping[str, Any],
    output_items: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    reasoning_items = [dict(item) for item in output_items if item.get("type") == "reasoning"]
    encrypted_items = [item for item in reasoning_items if "encrypted_content" in item]
    meta: dict[str, Any] = {}
    response_id = response.get("id")
    if isinstance(response_id, str) and response_id:
        meta["response_id"] = response_id
    response_reasoning = response.get("reasoning")
    if isinstance(response_reasoning, Mapping):
        reasoning_context = response_reasoning.get("context")
        if isinstance(reasoning_context, str) and reasoning_context:
            meta["reasoning_context"] = reasoning_context
    if output_items:
        meta[RESPONSES_RESPONSE_OUTPUT_META_KEY] = [dict(item) for item in output_items]
    if reasoning_items:
        meta["reasoning_items"] = reasoning_items
    if encrypted_items:
        meta["encrypted_content"] = [item["encrypted_content"] for item in encrypted_items]
    return meta or None


def _assistant_phase_from_output(output_items: list[Mapping[str, Any]]) -> str | None:
    for item in output_items:
        if item.get("type") != "message" or item.get("role", "assistant") != "assistant":
            continue
        phase = item.get("phase")
        if isinstance(phase, str) and phase:
            return phase
    return None


def _extract_responses_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    normalized: dict[str, int] = {}
    for field_name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            normalized[field_name] = value
    if not normalized:
        return None
    cache_read_tokens = _responses_cached_input_tokens(usage)
    if cache_read_tokens is not None:
        normalized["cache_read_tokens"] = cache_read_tokens
    cache_write_tokens = _responses_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        normalized["cache_write_tokens"] = cache_write_tokens
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        normalized["reasoning_tokens"] = reasoning_tokens
    return normalized or None


def _responses_cached_input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Read ``input_tokens_details.cached_tokens`` when present.

    Cached tokens are a subset of ``input_tokens`` on the Responses
    wire, so no input-token adjustment is needed.
    """
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
    if not isinstance(details, Mapping):
        return None
    cached_tokens = details.get("cached_tokens")
    return (
        cached_tokens
        if isinstance(cached_tokens, int)
        and not isinstance(cached_tokens, bool)
        and cached_tokens >= 0
        else None
    )


def _responses_cache_write_tokens(usage: Mapping[str, Any]) -> int | None:
    """Read ``input_tokens_details.cache_write_tokens`` when present."""
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
    if not isinstance(details, Mapping):
        return None
    cache_write_tokens = details.get("cache_write_tokens")
    if isinstance(cache_write_tokens, bool):
        return None
    if isinstance(cache_write_tokens, int) and cache_write_tokens >= 0:
        return cache_write_tokens
    return None
