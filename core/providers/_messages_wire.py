"""Messages serialization, response normalization and cache framing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers._messages_constants import (
    CACHE_BREAKPOINT_LIMIT,
    CACHE_CONTROL_EPHEMERAL,
    CACHE_UNMARKABLE_BLOCK_TYPES,
    MAX_HISTORY_CACHE_BREAKPOINTS,
    REASONING_META_CONTENT_BLOCKS,
    REDACTED_THINKING_BLOCK_TYPE,
    TEXT_BLOCK_TYPE,
    THINKING_BLOCK_TYPE,
)
from core.providers.adapter import (
    canonical_tool_result_is_error,
    normalize_tool_call_candidates,
    tool_result_content_blocks,
)
from core.providers.errors import ProviderError
from core.providers.reasoning import (
    reasoning_token_count,
)
from core.providers.tool_schema import render_tool_definitions


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
    *,
    include_thinking_blocks: bool = True,
) -> list[dict[str, Any]]:
    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "tool":
            pending_tool_results.append(_to_anthropic_tool_result_block(message))
            continue

        if pending_tool_results:
            anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))
            pending_tool_results = []
        anthropic_message = _to_anthropic_message(
            message,
            include_thinking_blocks=include_thinking_blocks,
        )
        if anthropic_message is not None:
            anthropic_messages.append(anthropic_message)

    if pending_tool_results:
        anthropic_messages.append(_to_anthropic_tool_result_message(pending_tool_results))

    return anthropic_messages


def _merge_anthropic_system_parts(
    parts: list[str | list[dict[str, Any]]],
) -> str | list[dict[str, Any]] | None:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if all(isinstance(part, str) for part in parts):
        return "\n\n".join(part for part in parts if isinstance(part, str))

    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        blocks.extend(
            dict(block) if isinstance(block, dict) else _text_block(block) for block in part
        )
    return blocks


def _to_anthropic_message(
    message: dict[str, Any],
    *,
    include_thinking_blocks: bool = True,
) -> dict[str, Any] | None:
    role = message.get("role")
    if role == "tool":
        return _to_anthropic_tool_result_message([_to_anthropic_tool_result_block(message)])
    if role == "assistant":
        content_blocks = _to_anthropic_assistant_content(
            message,
            include_thinking_blocks=include_thinking_blocks,
        )
        # The wire rejects empty content arrays — a replayed reasoning-only
        # turn whose thinking blocks were stripped has nothing left to send.
        if not content_blocks:
            return None
        return {
            "role": "assistant",
            "content": content_blocks,
        }
    if role == "user":
        return {
            "role": "user",
            "content": _to_anthropic_user_content(message.get("content", "")),
        }
    return {
        "role": role,
        "content": _to_anthropic_text_content(message.get("content", "")),
    }


def _to_anthropic_tool_result_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": blocks}


def _to_anthropic_tool_result_block(message: dict[str, Any]) -> dict[str, Any]:
    rich_content = tool_result_content_blocks(message)
    content: str | list[dict[str, Any]] = message["content"]
    if rich_content:
        content = [
            {"type": "text", "text": message["content"]},
            *[_to_anthropic_user_content_block(block) for block in rich_content],
        ]
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message["tool_call_id"],
        "content": content,
    }
    if canonical_tool_result_is_error(message):
        block["is_error"] = True
    return block


def _to_anthropic_user_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return _to_anthropic_text_content(content)

    return [_to_anthropic_user_content_block(block) for block in content]


def _to_anthropic_user_content_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"type": "text", "text": str(block)}

    block_type = block.get("type")
    if block_type == "media":
        base64_data = block.get("base64")
        media_type = block.get("media_type")
        if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
            raise ProviderError(
                "media content block requires string base64 and media_type fields",
                retryable=False,
            )
        if not media_type.startswith("image/"):
            raise ProviderError(
                f"Messages adapter supports only image media blocks; received {media_type}",
                retryable=False,
            )
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_data,
            },
        }
    if block_type == "document":
        return _to_anthropic_document_block(block)
    if block_type == "text":
        text = block.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}

    return dict(block)


def _to_anthropic_document_block(block: dict[str, Any]) -> dict[str, Any]:
    """Translate a canonical document block into an Anthropic ``document`` block.

    Wire shape verified against the Anthropic Messages API (base64 source).
    """
    base64_data = block.get("base64")
    media_type = block.get("media_type")
    if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
        raise ProviderError(
            "document content block requires string base64 and media_type fields",
            retryable=False,
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64_data,
        },
    }


def _to_anthropic_text_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return content
    return [_text_block(content)]


def _text_block(content: Any) -> dict[str, Any]:
    return {"type": "text", "text": "" if content is None else str(content)}


def _to_anthropic_assistant_content(
    message: dict[str, Any],
    *,
    include_thinking_blocks: bool = True,
) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    if include_thinking_blocks:
        content_blocks.extend(_reasoning_blocks_from_meta(message.get("reasoning_meta")))

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


def _apply_anthropic_tools(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    tools = kwargs.pop("tools", None)
    if not tools:
        return
    rendered = render_tool_definitions(
        tools,
        profile="omit_strict",
    )
    payload["tools"] = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        for tool in rendered
    ]


def _apply_prompt_caching(payload: dict[str, Any]) -> None:
    """Place ``cache_control`` breakpoints so Anthropic caches stable prefixes.

    One marker on the last system block caches tools + system together; up to
    :data:`MAX_HISTORY_CACHE_BREAKPOINTS` markers on the most recent message
    boundaries cache the growing conversation tail (the dominant cost at large
    context), giving the next request several read-points within the 20-block
    cache lookback. The total never exceeds :data:`CACHE_BREAKPOINT_LIMIT`.
    """

    remaining = CACHE_BREAKPOINT_LIMIT
    cached_system = _system_with_cache_control(payload.get("system"))
    if cached_system is not None:
        payload["system"] = cached_system
        remaining -= 1

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    history_budget = min(remaining, MAX_HISTORY_CACHE_BREAKPOINTS)
    marked = 0
    for index in range(len(messages) - 1, -1, -1):
        if marked >= history_budget:
            break
        message = messages[index]
        if not isinstance(message, dict):
            continue
        cached_message = _message_with_cache_control(message)
        if cached_message is not None:
            messages[index] = cached_message
            marked += 1


def _system_with_cache_control(system: Any) -> list[dict[str, Any]] | None:
    """Return the system field in block form with ``cache_control`` on its last
    cacheable block, or ``None`` when there is nothing to cache."""

    if isinstance(system, str):
        if not system.strip():
            return None
        return [_cached_text_block(system)]
    if isinstance(system, list) and system:
        return _blocks_with_cache_control(system)
    return None


def _message_with_cache_control(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of ``message`` with ``cache_control`` on its last cacheable
    content block, or ``None`` when the message has nothing to cache."""

    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return None
        marked = dict(message)
        marked["content"] = [_cached_text_block(content)]
        return marked
    if isinstance(content, list) and content:
        blocks = _blocks_with_cache_control(content)
        if blocks is None:
            return None
        marked = dict(message)
        marked["content"] = blocks
        return marked
    return None


def _blocks_with_cache_control(blocks: list[Any]) -> list[dict[str, Any]] | None:
    """Copy ``blocks`` and add ``cache_control`` to the last cacheable block.

    Returns ``None`` when no block can carry the marker (every block is a
    reasoning block, see :data:`CACHE_UNMARKABLE_BLOCK_TYPES`)."""

    copied = [dict(block) if isinstance(block, dict) else block for block in blocks]
    for index in range(len(copied) - 1, -1, -1):
        block = copied[index]
        if isinstance(block, dict) and block.get("type") not in CACHE_UNMARKABLE_BLOCK_TYPES:
            block["cache_control"] = dict(CACHE_CONTROL_EPHEMERAL)
            return copied
    return None


def _cached_text_block(text: str) -> dict[str, Any]:
    return {
        "type": TEXT_BLOCK_TYPE,
        "text": text,
        "cache_control": dict(CACHE_CONTROL_EPHEMERAL),
    }


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
    blocks = (
        content_blocks
        if isinstance(content_blocks, list)
        else [content_blocks]
        if isinstance(content_blocks, Mapping)
        else []
    )
    tool_calls: list[dict[str, Any]] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        tool_calls.extend(
            normalize_tool_call_candidates(
                tool_call_id=block.get("id"),
                name=block.get("name"),
                arguments=block.get("input"),
                fallback_id=f"tool_call_{position}",
            )
        )
    return tool_calls or None


def _extract_anthropic_usage(response: dict[str, Any]) -> dict[str, Any] | None:
    """Extract token usage from an Anthropic Messages response.

    Returns ``{"input_tokens": N, "output_tokens": N}`` when usage data
    is available, defaulting ``output_tokens`` to ``0`` when only
    ``input_tokens`` is provided.  Returns ``None`` when usage data is
    absent or incomplete.
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        return None
    output_tokens = usage.get("output_tokens")
    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens if output_tokens is not None else 0,
    }
    apply_anthropic_cache_usage(normalized, usage)
    apply_anthropic_reasoning_usage(normalized, usage)
    return normalized


def _extract_anthropic_stream_input_usage(usage: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a usable input snapshot from one Messages stream event.

    Native Anthropic streams report input/cache counters at ``message_start``.
    Compatible gateways may instead repeat a complete, more authoritative
    snapshot in the terminal ``message_delta``. A zero total is not usable for
    vBot's non-empty Chat requests and remains absent so Chat can estimate it.
    """

    input_tokens = usage.get("input_tokens")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        return None
    normalized: dict[str, Any] = {"input_tokens": input_tokens}
    apply_anthropic_cache_usage(normalized, usage)
    return normalized if normalized["input_tokens"] > 0 else None


def apply_anthropic_reasoning_usage(normalized: dict[str, Any], usage: dict[str, Any]) -> None:
    """Preserve Anthropic's optional Thinking-token output subset."""
    reasoning_tokens = reasoning_token_count(usage)
    if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0:
        normalized["reasoning_tokens"] = reasoning_tokens


def apply_anthropic_cache_usage(normalized: dict[str, Any], usage: dict[str, Any]) -> None:
    """Fold Anthropic cache token counts into canonical usage fields.

    Anthropic reports ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` separately from ``input_tokens``;
    canonical ``input_tokens`` means the total prompt including cached
    tokens, so both counts are added on top.
    """
    cache_read = usage.get("cache_read_input_tokens")
    cache_write = usage.get("cache_creation_input_tokens")
    input_tokens = normalized["input_tokens"]
    if isinstance(cache_read, int) and isinstance(input_tokens, int):
        normalized["cache_read_tokens"] = cache_read
        input_tokens += cache_read
    if isinstance(cache_write, int) and isinstance(input_tokens, int):
        normalized["cache_write_tokens"] = cache_write
        input_tokens += cache_write
    normalized["input_tokens"] = input_tokens


def _content_blocks(content_blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(content_blocks, list):
        return []
    return [block for block in content_blocks if isinstance(block, dict)]
