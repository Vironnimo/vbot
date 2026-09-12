"""Shared stateless ``/responses`` protocol helpers.

The helpers preserve the complete Responses item stream so OpenAI, OpenRouter,
and GitHub Copilot adapters can replay provider-owned reasoning, phase, and
future item kinds without reconstructing a lossy approximation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.providers._responses_output import (
    normalize_responses_response,
)
from core.providers._responses_stream import (
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
    normalize_responses_stream_event,
)
from core.providers._responses_values import (
    _REASONING_META_KEYS,
    REASONING_ENCRYPTED_CONTENT_INCLUDE,
    REASONING_SUMMARY_DELTA_EVENTS,
    RESPONSES_DONE_MARKER,
    RESPONSES_ERROR_EVENTS,
    RESPONSES_INCOMPLETE_EVENTS,
    RESPONSES_RESPONSE_OUTPUT_META_KEY,
    ResponsesRequestPolicy,
    _function_call_arguments,
    _function_call_name,
    _function_description,
    _function_parameters,
    _is_reasoning_item,
    _mapping_list,
    _response_output_from_meta,
    _serialize_tool_arguments,
    _string_or,
)
from core.providers.adapter import (
    RESPONSES_TOOL_CALL_ID_PROFILE,
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
    normalize_tool_call_ids,
    tool_result_content_blocks,
)
from core.providers.errors import (
    ProviderError,
)
from core.providers.tool_schema import render_tool_definitions
from core.utils.tokens import estimate_structured_tokens, estimate_tokens

__all__ = [
    "REASONING_ENCRYPTED_CONTENT_INCLUDE",
    "REASONING_SUMMARY_DELTA_EVENTS",
    "RESPONSES_DONE_MARKER",
    "RESPONSES_ERROR_EVENTS",
    "RESPONSES_INCOMPLETE_EVENTS",
    "RESPONSES_RESPONSE_OUTPUT_META_KEY",
    "ResponsesRequestPolicy",
    "ResponsesStreamState",
    "build_responses_payload",
    "estimate_responses_input_tokens",
    "iter_responses_sse_deltas_with_state",
    "normalize_responses_response",
    "normalize_responses_stream_event",
]


def build_responses_payload(
    messages: list[dict[str, Any]],
    *,
    model_id: str,
    policy: ResponsesRequestPolicy,
    stream: bool = False,
    document_media_types: frozenset[str] = frozenset(),
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a stateless ``/responses`` request payload from canonical messages."""

    wire_messages = normalize_tool_call_ids(messages, RESPONSES_TOOL_CALL_ID_PROFILE)
    request_kwargs = policy.filter_request_kwargs(kwargs)
    payload: dict[str, Any] = {
        "model": model_id,
        "input": _messages_to_responses_input(
            wire_messages,
            document_media_types=document_media_types,
        ),
    }
    instructions = _system_instructions(wire_messages)
    if instructions:
        payload["instructions"] = instructions
    if stream:
        payload["stream"] = True

    _apply_responses_tools(
        payload,
        request_kwargs,
        policy,
    )
    _apply_responses_reasoning(payload, request_kwargs, policy)
    _apply_responses_text_format(payload, request_kwargs, policy)
    _apply_remaining_kwargs(payload, request_kwargs, policy)
    return payload


def estimate_responses_input_tokens(
    messages: list[dict[str, Any]],
    *,
    model_id: str | None = None,
    document_media_types: frozenset[str] = frozenset(),
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Estimate one stateless ``/responses`` request's input footprint.

    Counts the input items exactly as :func:`build_responses_payload` renders
    them — including provider-owned reasoning items with their encrypted
    continuity blobs, which the shared chat-message estimator would miss — plus
    the system instructions and the rendered Tool definitions. Native media is
    normalized like the chat estimator so transport encoding does not
    masquerade as prose tokens.
    """

    wire_messages = normalize_tool_call_ids(messages, RESPONSES_TOOL_CALL_ID_PROFILE)
    input_items = _messages_to_responses_input(
        wire_messages,
        document_media_types=document_media_types,
    )
    total_tokens, _ = estimate_structured_tokens(input_items, model_id=model_id)
    instructions = _system_instructions(wire_messages)
    if instructions:
        instruction_tokens, _ = estimate_tokens(instructions, model_id=model_id)
        total_tokens += instruction_tokens
    if tools:
        tool_tokens, _ = estimate_structured_tokens(
            [
                _to_responses_function_tool(tool)
                for tool in render_tool_definitions(
                    [tool for tool in tools if isinstance(tool, Mapping)],
                    profile="explicit_non_strict",
                )
            ],
            model_id=model_id,
        )
        total_tokens += tool_tokens
    return total_tokens


def _system_instructions(messages: list[dict[str, Any]]) -> str | None:
    parts = [message.get("content", "") for message in messages if message.get("role") == "system"]
    text_parts = [part for part in parts if isinstance(part, str) and part]
    return "\n\n".join(text_parts) or None


def _messages_to_responses_input(
    messages: list[dict[str, Any]],
    *,
    document_media_types: frozenset[str],
) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "assistant":
            input_items.extend(_assistant_message_to_input_items(message))
            continue
        if role == "tool":
            input_items.append(
                _tool_message_to_function_output(
                    message,
                    document_media_types=document_media_types,
                )
            )
            continue
        if role == "user":
            input_items.append(
                _user_message_to_input_item(
                    message.get("content", ""),
                    document_media_types=document_media_types,
                )
            )
    return input_items


def _assistant_message_to_input_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    response_output = _response_output_from_meta(message.get("reasoning_meta"))
    if response_output:
        # Stateless Responses continuation is an item protocol, not a message
        # reconstruction protocol. Preserve every output item verbatim so opaque
        # reasoning, assistant phase, program items, and future item kinds keep
        # their original ordering and identifiers.
        return _safe_response_output_items(response_output, message.get("tool_calls"))

    input_items: list[dict[str, Any]] = []
    input_items.extend(_reasoning_meta_input_items(message.get("reasoning_meta")))
    content = message.get("content")
    if isinstance(content, str) and content:
        input_items.append(
            _text_message_to_input_item("assistant", content, phase=message.get("phase"))
        )
    for tool_call in _mapping_list(message.get("tool_calls")):
        input_items.append(_tool_call_to_function_call(tool_call))
    return input_items


def _safe_response_output_items(
    response_output: list[Mapping[str, Any]],
    raw_tool_calls: Any,
) -> list[dict[str, Any]]:
    """Replace raw function items when canonical calls cannot replay them verbatim."""

    tool_calls = _mapping_list(raw_tool_calls)
    if not any(
        TOOL_CALL_REJECTION_FIELD in tool_call
        or TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD in tool_call
        for tool_call in tool_calls
    ):
        return [dict(item) for item in response_output]

    safe_items: list[dict[str, Any]] = []
    tool_call_index = 0
    for item in response_output:
        if item.get("type") != "function_call" or tool_call_index >= len(tool_calls):
            safe_items.append(dict(item))
            continue
        tool_call = tool_calls[tool_call_index]
        argument_sequence = _argument_sequence_at(tool_calls, tool_call_index)
        if argument_sequence:
            safe_items.extend(_tool_call_to_function_call(call) for call in argument_sequence)
            tool_call_index += len(argument_sequence)
            continue
        tool_call_index += 1
        safe_items.append(
            _tool_call_to_function_call(tool_call)
            if TOOL_CALL_REJECTION_FIELD in tool_call
            else dict(item)
        )
    safe_items.extend(
        _tool_call_to_function_call(tool_call) for tool_call in tool_calls[tool_call_index:]
    )
    return safe_items


def _argument_sequence_at(
    tool_calls: list[Mapping[str, Any]],
    start: int,
) -> list[Mapping[str, Any]]:
    first = tool_calls[start]
    sequence_index = first.get(TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD)
    sequence_length = first.get(TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD)
    if sequence_index != 0 or not isinstance(sequence_length, int) or sequence_length <= 1:
        return []
    sequence = tool_calls[start : start + sequence_length]
    if len(sequence) != sequence_length:
        return []
    if any(
        call.get(TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD) != index
        or call.get(TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD) != sequence_length
        for index, call in enumerate(sequence)
    ):
        return []
    return sequence


def _text_message_to_input_item(
    role: str,
    content: Any,
    *,
    phase: Any = None,
) -> dict[str, Any]:
    text = content if isinstance(content, str) else ""
    content_type = "output_text" if role == "assistant" else "input_text"
    item = {"role": role, "content": [{"type": content_type, "text": text}]}
    if role == "assistant" and isinstance(phase, str) and phase:
        item["phase"] = phase
    return item


def _user_message_to_input_item(
    content: Any,
    *,
    document_media_types: frozenset[str],
) -> dict[str, Any]:
    return {
        "role": "user",
        "content": _user_content_parts(
            content,
            document_media_types=document_media_types,
        ),
    }


def _user_content_parts(
    content: Any,
    *,
    document_media_types: frozenset[str],
) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        text = content if isinstance(content, str) else ""
        return [{"type": "input_text", "text": text}]

    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            block_text = block.get("text")
            if isinstance(block_text, str):
                parts.append({"type": "input_text", "text": block_text})
        elif block_type == "media":
            parts.append(_input_image_from_media(block))
        elif block_type == "document":
            parts.append(
                _input_file_from_document(
                    block,
                    document_media_types=document_media_types,
                )
            )
    return parts


def _input_image_from_media(block: Mapping[str, Any]) -> dict[str, Any]:
    # The Responses endpoint takes images as an `input_image` part with a data
    # URI. Reject non-image media loudly instead of silently dropping the whole
    # turn, which previously collapsed any list content to an empty string.
    base64_data = block.get("base64")
    media_type = block.get("media_type")
    if not isinstance(base64_data, str) or not isinstance(media_type, str) or not media_type:
        raise ProviderError(
            "media content block requires string base64 and media_type fields",
            retryable=False,
        )
    if not media_type.startswith("image/"):
        raise ProviderError(
            f"Responses adapter supports only image media blocks; received {media_type}",
            retryable=False,
        )
    return {
        "type": "input_image",
        "image_url": f"data:{media_type};base64,{base64_data}",
    }


def _input_file_from_document(
    block: Mapping[str, Any],
    *,
    document_media_types: frozenset[str],
) -> dict[str, Any]:
    base64_data = block.get("base64")
    media_type = block.get("media_type")
    filename = block.get("filename")
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
    if media_type not in document_media_types:
        raise ProviderError(
            f"Responses adapter does not support document media type {media_type}",
            retryable=False,
        )
    return {
        "type": "input_file",
        "filename": filename,
        "file_data": f"data:{media_type};base64,{base64_data}",
    }


def _tool_call_to_function_call(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": _string_or(tool_call.get("id"), ""),
        "name": _function_call_name(tool_call),
        "arguments": _serialize_tool_arguments(_function_call_arguments(tool_call)),
    }


def _tool_message_to_function_output(
    message: Mapping[str, Any],
    *,
    document_media_types: frozenset[str],
) -> dict[str, Any]:
    output: str | list[dict[str, Any]] = _string_or(message.get("content"), "")
    rich_content = tool_result_content_blocks(message)
    if rich_content:
        output = [
            {"type": "input_text", "text": output},
            *_user_content_parts(
                rich_content,
                document_media_types=document_media_types,
            ),
        ]
    return {
        "type": "function_call_output",
        "call_id": _string_or(message.get("tool_call_id"), ""),
        "output": output,
    }


def _reasoning_meta_input_items(reasoning_meta: Any) -> list[dict[str, Any]]:
    if not isinstance(reasoning_meta, Mapping):
        return []
    for key in _REASONING_META_KEYS:
        items = reasoning_meta.get(key)
        if isinstance(items, list):
            return [dict(item) for item in items if _is_reasoning_item(item)]
    return []


def _apply_responses_tools(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: ResponsesRequestPolicy,
) -> None:
    tools = request_kwargs.pop("tools", None)
    tool_choice = request_kwargs.pop("tool_choice", None)
    if not policy.supports_tools or not tools:
        return
    rendered = render_tool_definitions(
        [tool for tool in tools if isinstance(tool, Mapping)],
        profile="explicit_non_strict",
    )
    payload["tools"] = [_to_responses_function_tool(tool) for tool in rendered]
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice


def _to_responses_function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    if isinstance(function, Mapping):
        return {
            "type": "function",
            "name": _function_call_name(tool),
            "description": _function_description(tool),
            "parameters": _function_parameters(tool),
            **({"strict": tool["strict"]} if isinstance(tool.get("strict"), bool) else {}),
        }
    return {
        "type": "function",
        "name": _function_call_name(tool),
        "description": _function_description(tool),
        "parameters": _function_parameters(tool),
        **({"strict": tool["strict"]} if isinstance(tool.get("strict"), bool) else {}),
    }


def _apply_responses_reasoning(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: ResponsesRequestPolicy,
) -> None:
    reasoning = request_kwargs.pop("reasoning", None)
    effort = request_kwargs.pop("reasoning_effort", None) or request_kwargs.pop(
        "thinking_effort", None
    )
    include_reasoning = request_kwargs.pop("include_reasoning", None)
    if not policy.allows_any_reasoning_controls:
        return
    if isinstance(reasoning, Mapping):
        payload["reasoning"] = dict(reasoning)
    else:
        safe_effort = policy.closest_reasoning_effort(effort)
        if safe_effort is not None and (
            safe_effort != "none" or policy.supports_explicit_none_effort
        ):
            payload["reasoning"] = {"effort": safe_effort, "summary": "auto"}
    # Reasoning-capable Responses wires always need encrypted continuity bytes on
    # the next turn, including always-on Models that omit an effort object and
    # tool-loop turns that only replay prior items. Gate only on capability.
    if payload.get("reasoning") or include_reasoning is not False:
        _append_include(payload, REASONING_ENCRYPTED_CONTENT_INCLUDE)


def _apply_responses_text_format(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: ResponsesRequestPolicy,
) -> None:
    response_format = request_kwargs.pop("response_format", None)
    text = request_kwargs.pop("text", None)
    request_kwargs.pop("structured_outputs", None)
    request_kwargs.pop("json_mode", None)
    if not policy.supports_structured_outputs:
        return
    if isinstance(text, Mapping):
        payload["text"] = dict(text)
    if response_format is not None:
        current_text = payload.get("text")
        existing_text = current_text if isinstance(current_text, dict) else {}
        payload["text"] = {**existing_text, "format": response_format}


def _apply_remaining_kwargs(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: ResponsesRequestPolicy,
) -> None:
    request_kwargs.pop("include", None)
    request_kwargs.pop("cache_control", None)
    prompt_cache_key = request_kwargs.pop("prompt_cache_key", None)
    request_kwargs.pop("prompt_cache_retention", None)
    service_tier = request_kwargs.pop("service_tier", None)
    max_tokens = request_kwargs.pop("max_tokens", None)
    if max_tokens is not None and "max_output_tokens" not in request_kwargs:
        payload["max_output_tokens"] = max_tokens
    max_output_tokens = request_kwargs.pop("max_output_tokens", None)
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    temperature = request_kwargs.pop("temperature", None)
    if temperature is not None and _supports_responses_temperature(policy):
        payload["temperature"] = temperature
    top_p = request_kwargs.pop("top_p", None)
    if top_p is not None:
        payload["top_p"] = top_p
    if policy.supports_request_parameter("prompt_cache_key") and isinstance(prompt_cache_key, str):
        normalized_cache_key = prompt_cache_key.strip()
        if normalized_cache_key:
            payload["prompt_cache_key"] = normalized_cache_key
    if policy.supports_request_parameter("service_tier") and service_tier in {
        "default",
        "priority",
    }:
        payload["service_tier"] = service_tier
    parallel_tool_calls = request_kwargs.pop("parallel_tool_calls", None)
    if policy.supports_parallel_tool_calls and isinstance(parallel_tool_calls, bool):
        payload["parallel_tool_calls"] = parallel_tool_calls


def _supports_responses_temperature(policy: ResponsesRequestPolicy) -> bool:
    """Return whether this Copilot Responses route should forward ``temperature``.

    GPT-5 reasoning models on the Responses API reject temperature changes, so
    Copilot should omit that field unless the routed model is known to support
    it. The current runtime policy does not expose positive temperature support
    for any Responses-routed Copilot model, so this helper stays conservative
    and omits the field for that endpoint family.
    """

    return policy.supports_request_parameter("temperature")


def _append_include(payload: dict[str, Any], include_item: str) -> None:
    include = payload.setdefault("include", [])
    if isinstance(include, list) and include_item not in include:
        include.append(include_item)
