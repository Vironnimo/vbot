"""GitHub Copilot ``/v1/messages`` protocol helpers.

The helpers in this module intentionally implement a conservative,
Anthropic-like subset for Copilot's Messages endpoint. They build request
payloads from vBot's canonical chat dictionaries and normalize provider
responses/stream events back to the adapter delta contract consumed by chat.
Reasoning, the sampling-parameter rules and the accepted request fields come
from the Model's wire profile.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.providers._chat_completions_wire import _selected_thinking_effort
from core.providers._openai_constants import REASONING_PARAMETER_NAMES
from core.providers.adapter import (
    canonical_tool_result_is_error,
    normalize_tool_call_candidates,
    tool_result_content_blocks,
    tool_result_text,
)
from core.providers.anthropic_compatible import (
    AnthropicMessagesStreamDecoder,
    extract_anthropic_usage,
)
from core.providers.errors import ProviderError
from core.providers.openai_compatible import DEFAULT_MAX_OUTPUT_TOKENS
from core.providers.reasoning_dialects import dialect_request_fields, render_reasoning
from core.providers.tool_schema import render_tool_definitions
from core.providers.wire_profile import WireProfile
from core.utils.tokens import estimate_structured_tokens

TEXT_BLOCK_TYPE = "text"
IMAGE_BLOCK_TYPE = "image"
MEDIA_BLOCK_TYPE = "media"
TOOL_USE_BLOCK_TYPE = "tool_use"
TOOL_RESULT_BLOCK_TYPE = "tool_result"
THINKING_BLOCK_TYPE = "thinking"
REDACTED_THINKING_BLOCK_TYPE = "redacted_thinking"
REASONING_META_CONTENT_BLOCKS = "content_blocks"

SAFE_TOP_LEVEL_PARAMETERS = {
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
}
ACTIVE_THINKING_TYPES = frozenset({"adaptive", "enabled"})
SAFE_TOOL_CHOICE_TYPES = {"auto", "any", "tool"}


class CopilotMessagesStreamState(AnthropicMessagesStreamDecoder):
    """Copilot policy applied to the shared Anthropic Messages decoder."""

    def __init__(self) -> None:
        super().__init__(
            error_detail=_messages_error_detail,
            reasoning_block_normalizer=_safe_reasoning_block,
            text_delta_in_thinking=True,
            emit_usage_without_start=False,
            drop_stopped_reasoning_block=True,
        )


def build_copilot_messages_payload(
    messages: list[dict[str, Any]],
    *,
    model_id: str,
    profile: WireProfile,
    provider_label: str = "GitHub Copilot",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a conservative Copilot ``/v1/messages`` request payload.

    ``kwargs`` carry only the request features the Model takes. The caller's
    thinking effort is planned on ``profile`` against the resolved output
    allowance and spelled in its reasoning dialect; the profile's body rules are
    added and its parameter rules then shape the sampling parameters (for
    example, a parameter dropped while thinking).
    """

    request_kwargs = dict(kwargs)
    effort = _selected_thinking_effort(request_kwargs)
    for name in REASONING_PARAMETER_NAMES:
        request_kwargs.pop(name, None)
    system, conversation_messages = _split_system(messages)
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": _to_copilot_messages(conversation_messages),
    }
    if system is not None:
        payload["system"] = system

    _apply_safe_messages_tools(payload, request_kwargs)
    # A thinking budget is part of the Messages output allowance, so the plan
    # keeps it strictly under the resolved max tokens.
    max_tokens = _resolve_messages_max_tokens(request_kwargs)
    wire = profile.reasoning
    render_reasoning(
        wire,
        wire.plan(effort, output_allowance=max_tokens),
        payload,
        output_allowance=max_tokens,
    )
    payload["max_tokens"] = max_tokens
    for parameter_name in SAFE_TOP_LEVEL_PARAMETERS:
        if parameter_name in request_kwargs:
            payload[parameter_name] = request_kwargs[parameter_name]
    rules = profile.request
    rules.apply_body(payload)
    thinking = payload.get("thinking")
    rules.shape_parameters(
        payload,
        reasoning_active=isinstance(thinking, Mapping)
        and thinking.get("type") in ACTIVE_THINKING_TYPES,
        protected=dialect_request_fields(wire.dialect),
        provider_label=provider_label,
    )
    return payload


def estimate_copilot_messages_input_tokens(
    messages: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Estimate one ``/v1/messages`` request's input as it is rendered.

    Counts the system text, the history with its signed thinking blocks and the
    rendered Tool definitions exactly as :func:`build_copilot_messages_payload`
    builds them, so readable reasoning the Messages wire never sends is not
    counted.
    """

    system, conversation_messages = _split_system([dict(message) for message in messages])
    payload: dict[str, Any] = {}
    if system is not None:
        payload["system"] = system
    _apply_safe_messages_tools(payload, {"tools": list(tools)} if tools else {})
    # Count the growing history apart from the stable System Prompt and Tools so
    # the shared per-item count cache also serves this wire.
    history_tokens = estimate_structured_tokens(
        _to_copilot_messages(conversation_messages), model_id=model_id
    )[0]
    return history_tokens + estimate_structured_tokens(payload, model_id=model_id)[0]


def normalize_copilot_messages_response(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Copilot Messages response, whose ``content`` is a list, to canonical fields."""

    content_blocks = response["content"]
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": _extract_messages_text(content_blocks),
        "reasoning": _extract_messages_reasoning(content_blocks),
        "reasoning_meta": _extract_messages_reasoning_meta(content_blocks),
        "tool_calls": _extract_messages_tool_calls(content_blocks),
    }
    usage = extract_anthropic_usage(response)
    if usage is not None:
        normalized["usage"] = usage
    return normalized


def normalize_copilot_messages_stream_event(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    """Normalize one parsed Copilot Messages stream event."""

    return state.normalize(event)


def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return the joined system text and the conversation the Messages wire carries."""

    system_parts: list[str] = []
    conversation_messages: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_text = _text_from_content(message.get("content"))
            if system_text:
                system_parts.append(system_text)
            continue
        if role in {"user", "assistant", "tool"}:
            conversation_messages.append(message)
    return ("\n\n".join(system_parts) if system_parts else None), conversation_messages


def _to_copilot_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copilot_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "tool":
            pending_tool_results.append(_to_tool_result_block(message))
            continue

        if pending_tool_results:
            copilot_messages.append(_tool_result_message(pending_tool_results))
            pending_tool_results = []
        copilot_messages.append(_to_copilot_message(message))

    if pending_tool_results:
        copilot_messages.append(_tool_result_message(pending_tool_results))

    return copilot_messages


def _to_copilot_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "assistant":
        return {"role": "assistant", "content": _assistant_content_blocks(message)}
    return {
        "role": "user",
        "content": _text_content_blocks(message.get("content", "")),
    }


def _tool_result_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": blocks}


def _to_tool_result_block(message: dict[str, Any]) -> dict[str, Any]:
    rich_content = tool_result_content_blocks(message)
    content: str | list[dict[str, Any]] = _text_from_content(
        tool_result_text(message.get("content", ""))
    )
    if rich_content:
        content = [
            {"type": TEXT_BLOCK_TYPE, "text": content},
            *_safe_content_blocks(rich_content),
        ]
    block: dict[str, Any] = {
        "type": TOOL_RESULT_BLOCK_TYPE,
        "tool_use_id": str(message.get("tool_call_id", "")),
        "content": content,
    }
    if canonical_tool_result_is_error(message):
        block["is_error"] = True
    return block


def _assistant_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    content_blocks.extend(_reasoning_blocks_from_meta(message.get("reasoning_meta")))

    content = message.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": TEXT_BLOCK_TYPE, "text": content})
    elif isinstance(content, list):
        content_blocks.extend(_safe_content_blocks(content))

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        tool_id = tool_call.get("id")
        name = tool_call.get("name")
        if not isinstance(tool_id, str) or not isinstance(name, str):
            continue
        arguments = tool_call.get("arguments")
        content_blocks.append(
            {
                "type": TOOL_USE_BLOCK_TYPE,
                "id": tool_id,
                "name": name,
                "input": dict(arguments) if isinstance(arguments, dict) else {},
            }
        )
    return content_blocks


def _text_content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        blocks = _safe_content_blocks(content)
        if blocks:
            return blocks
    return [{"type": TEXT_BLOCK_TYPE, "text": _text_from_content(content)}]


def _safe_content_blocks(content: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == TEXT_BLOCK_TYPE:
            text = block.get("text")
            if isinstance(text, str):
                blocks.append({"type": TEXT_BLOCK_TYPE, "text": text})
        elif block_type == MEDIA_BLOCK_TYPE:
            blocks.append(_image_block_from_media(block))
    return blocks


def _image_block_from_media(block: dict[str, Any]) -> dict[str, Any]:
    # The Messages endpoint mirrors the Anthropic image block shape. Reject
    # anything that is not a usable image loudly instead of silently dropping
    # it, so an unsupported attachment surfaces as an error rather than a
    # quietly text-only request.
    base64_data = block.get("base64")
    media_type = block.get("media_type")
    if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
        raise ProviderError(
            "media content block requires string base64 and media_type fields",
            retryable=False,
        )
    if not media_type.startswith("image/"):
        raise ProviderError(
            "GitHub Copilot messages adapter supports only image media blocks; "
            f"received {media_type}",
            retryable=False,
        )
    return {
        "type": IMAGE_BLOCK_TYPE,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64_data,
        },
    }


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == TEXT_BLOCK_TYPE
            and isinstance(block.get("text"), str)
        )
    return str(content)


def _reasoning_blocks_from_meta(reasoning_meta: Any) -> list[dict[str, Any]]:
    if not isinstance(reasoning_meta, dict):
        return []
    blocks = reasoning_meta.get(REASONING_META_CONTENT_BLOCKS)
    if not isinstance(blocks, list):
        return []
    return [_safe_reasoning_block(block) for block in blocks if _safe_reasoning_block(block)]


def _safe_reasoning_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {}
    block_type = block.get("type")
    if block_type == THINKING_BLOCK_TYPE:
        safe_block: dict[str, Any] = {"type": THINKING_BLOCK_TYPE}
        thinking = block.get("thinking")
        text = block.get("text")
        signature = block.get("signature")
        if isinstance(thinking, str):
            safe_block["thinking"] = thinking
        elif isinstance(text, str):
            safe_block["text"] = text
        if isinstance(signature, str):
            safe_block["signature"] = signature
        return safe_block
    if block_type == REDACTED_THINKING_BLOCK_TYPE:
        safe_block = {"type": REDACTED_THINKING_BLOCK_TYPE}
        data = block.get("data")
        if isinstance(data, str):
            safe_block["data"] = data
        return safe_block
    return {}


def _apply_safe_messages_tools(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    tools = kwargs.pop("tools", None)
    tool_choice = kwargs.pop("tool_choice", None)
    kwargs.pop("parallel_tool_calls", None)
    if not isinstance(tools, list) or not tools:
        return

    rendered = render_tool_definitions(tools, profile="omit_strict")
    payload["tools"] = [tool for tool in (_safe_tool(tool) for tool in rendered) if tool]
    if not payload["tools"]:
        payload.pop("tools")
        return

    safe_tool_choice = _safe_tool_choice(tool_choice)
    if safe_tool_choice is not None:
        payload["tool_choice"] = safe_tool_choice


def _safe_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    name = tool.get("name")
    description = tool.get("description")
    parameters = tool.get("parameters")
    if not isinstance(name, str) or not name or not isinstance(parameters, dict):
        return {}
    return {
        "name": name,
        "description": description if isinstance(description, str) else "",
        "input_schema": parameters,
    }


def _safe_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    if isinstance(tool_choice, str) and tool_choice in {"auto", "any"}:
        return {"type": tool_choice}
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type not in SAFE_TOOL_CHOICE_TYPES:
        return None
    safe_choice = {"type": choice_type}
    name = tool_choice.get("name")
    if choice_type == "tool" and isinstance(name, str) and name:
        safe_choice["name"] = name
        return safe_choice
    if choice_type != "tool":
        return safe_choice
    return None


def _resolve_messages_max_tokens(kwargs: dict[str, Any]) -> int:
    explicit_max_output_tokens = _safe_max_tokens_value(kwargs.pop("max_output_tokens", None))
    explicit_max_completion_tokens = _safe_max_tokens_value(
        kwargs.pop("max_completion_tokens", None)
    )
    explicit_max_tokens = _safe_max_tokens_value(kwargs.pop("max_tokens", None))

    for max_tokens in (
        explicit_max_output_tokens,
        explicit_max_completion_tokens,
        explicit_max_tokens,
    ):
        if max_tokens is not None:
            return max_tokens
    return DEFAULT_MAX_OUTPUT_TOKENS


def _safe_max_tokens_value(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed_value = int(value)
        return parsed_value if parsed_value > 0 else None
    return None


def _extract_messages_text(content_blocks: list[Any]) -> str | None:
    text_parts = [
        block["text"]
        for block in _content_blocks(content_blocks)
        if block.get("type") == TEXT_BLOCK_TYPE and isinstance(block.get("text"), str)
    ]
    return "".join(text_parts) if text_parts else None


def _extract_messages_reasoning(content_blocks: list[Any]) -> str | None:
    reasoning_parts = [
        reasoning_text
        for block in _content_blocks(content_blocks)
        if block.get("type") == THINKING_BLOCK_TYPE
        for reasoning_text in [_reasoning_text_from_block(block)]
        if reasoning_text is not None
    ]
    return "".join(reasoning_parts) if reasoning_parts else None


def _extract_messages_reasoning_meta(content_blocks: list[Any]) -> dict[str, Any] | None:
    reasoning_blocks = [
        safe_block
        for safe_block in (
            _safe_reasoning_block(block) for block in _content_blocks(content_blocks)
        )
        if safe_block
    ]
    if not reasoning_blocks:
        return None
    return {REASONING_META_CONTENT_BLOCKS: reasoning_blocks}


def _extract_messages_tool_calls(content_blocks: Any) -> list[dict[str, Any]] | None:
    blocks = (
        content_blocks
        if isinstance(content_blocks, list)
        else [content_blocks]
        if isinstance(content_blocks, dict)
        else []
    )
    tool_calls: list[dict[str, Any]] = []
    for position, raw_block in enumerate(blocks):
        if not isinstance(raw_block, dict):
            continue
        block = raw_block
        if block.get("type") != TOOL_USE_BLOCK_TYPE:
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


def _content_blocks(content_blocks: list[Any]) -> list[dict[str, Any]]:
    return [block for block in content_blocks if isinstance(block, dict)]


def _reasoning_text_from_block(block: dict[str, Any]) -> str | None:
    thinking = block.get("thinking")
    if isinstance(thinking, str):
        return thinking
    text = block.get("text")
    if isinstance(text, str):
        return text
    return None


def _messages_error_detail(event: dict[str, Any]) -> str:
    error = event.get("error")
    if not isinstance(error, dict):
        return "Copilot Messages stream error"
    message = error.get("message")
    error_type = error.get("type")
    if isinstance(error_type, str) and isinstance(message, str):
        return f"Copilot Messages stream error ({error_type}): {message}"
    if isinstance(message, str):
        return f"Copilot Messages stream error: {message}"
    return "Copilot Messages stream error"
