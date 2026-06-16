"""GitHub Copilot ``/responses`` protocol helpers.

The functions in this module are intentionally adapter-independent.  Phase 3
will wire them into ``GitHubCopilotAdapter``; until then they provide the
request, response, and SSE normalization rules for Copilot's Responses-shaped
endpoint without changing generic OpenAI-compatible code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.providers.errors import ProviderError

RESPONSES_DONE_MARKER = "[DONE]"
REASONING_ENCRYPTED_CONTENT_INCLUDE = "reasoning.encrypted_content"
REASONING_SUMMARY_DELTA_EVENTS = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.output_item.reasoning_summary_text.delta",
}
RESPONSES_ERROR_EVENTS = {"error", "response.failed", "response.incomplete"}
_REASONING_META_KEYS = ("reasoning_items", "response_output")


class ResponsesRequestPolicy(Protocol):
    """Provider policy surface needed by the shared Responses payload builder."""

    @property
    def supports_tools(self) -> bool: ...

    @property
    def supports_parallel_tool_calls(self) -> bool: ...

    @property
    def supports_structured_outputs(self) -> bool: ...

    @property
    def allows_any_reasoning_controls(self) -> bool: ...

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]: ...

    def closest_reasoning_effort(self, effort: Any) -> str | None: ...

    def supports_request_parameter(self, parameter_name: str) -> bool: ...


@dataclass
class ResponsesStreamState:
    """State needed to normalize one Responses SSE stream."""

    tool_call_ids_by_output_index: dict[int, str] = field(default_factory=dict)
    item_id_to_call_id: dict[str, str] = field(default_factory=dict)
    emitted_tool_names: set[str] = field(default_factory=set)
    emitted_tool_arguments: dict[str, str] = field(default_factory=dict)
    emitted_reasoning_text: str = ""


def build_responses_payload(
    messages: list[dict[str, Any]],
    *,
    model_id: str,
    policy: ResponsesRequestPolicy,
    stream: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a Copilot ``/responses`` request payload from canonical messages."""

    request_kwargs = policy.filter_request_kwargs(kwargs)
    payload: dict[str, Any] = {
        "model": model_id,
        "input": _messages_to_responses_input(messages),
    }
    instructions = _system_instructions(messages)
    if instructions:
        payload["instructions"] = instructions
    if stream:
        payload["stream"] = True

    _apply_responses_tools(payload, request_kwargs, policy)
    _apply_responses_reasoning(payload, request_kwargs, policy)
    _apply_responses_text_format(payload, request_kwargs, policy)
    _apply_remaining_kwargs(payload, request_kwargs, policy)
    return payload


def normalize_responses_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a non-streaming Responses result to canonical assistant fields."""

    output_items = _mapping_list(response.get("output"))
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": _joined_or_none(_extract_output_text_parts(output_items)),
        "reasoning": _joined_or_none(_extract_reasoning_parts(output_items)),
        "reasoning_meta": _extract_reasoning_meta(response, output_items),
        "tool_calls": _extract_function_calls(output_items),
    }
    usage = _extract_responses_usage(response.get("usage"))
    if usage is not None:
        normalized["usage"] = usage
    return normalized


def iter_responses_sse_deltas_with_state(
    lines: Iterable[str],
    state: ResponsesStreamState,
) -> Iterator[dict[str, Any]]:
    """Parse Responses SSE lines using caller-owned stream state."""

    for event_name, event_data in _iter_sse_events(lines):
        yield from normalize_responses_stream_event(event_name, event_data, state)


def normalize_responses_stream_event(
    event_name: str,
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    """Normalize one parsed Responses stream event.

    Unknown event names are ignored so Copilot can add new non-critical events
    without leaking raw provider chunks to the chat layer.
    """

    event_type = _event_type(event_name, event_data)
    if event_type in RESPONSES_ERROR_EVENTS:
        raise ProviderError(_responses_error_message(event_data), retryable=False)
    if event_type == "response.output_text.delta":
        return _text_delta(event_data, "content_delta")
    if event_type in REASONING_SUMMARY_DELTA_EVENTS:
        return _reasoning_delta(event_data, state)
    if event_type == "response.function_call_arguments.delta":
        return _function_arguments_delta(event_data, state)
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        return _output_item_event_deltas(event_data, state)
    if event_type in {"response.completed", "response.done"}:
        return _completed_event_deltas(event_data, state)
    return []


def _system_instructions(messages: list[dict[str, Any]]) -> str | None:
    parts = [message.get("content", "") for message in messages if message.get("role") == "system"]
    text_parts = [part for part in parts if isinstance(part, str) and part]
    return "\n\n".join(text_parts) or None


def _messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "assistant":
            input_items.extend(_assistant_message_to_input_items(message))
            continue
        if role == "tool":
            input_items.append(_tool_message_to_function_output(message))
            continue
        if role == "user":
            input_items.append(_user_message_to_input_item(message.get("content", "")))
    return input_items


def _assistant_message_to_input_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    input_items.extend(_reasoning_meta_input_items(message.get("reasoning_meta")))
    content = message.get("content")
    if isinstance(content, str) and content:
        input_items.append(_text_message_to_input_item("assistant", content))
    for tool_call in _mapping_list(message.get("tool_calls")):
        input_items.append(_tool_call_to_function_call(tool_call))
    return input_items


def _text_message_to_input_item(role: str, content: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else ""
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def _user_message_to_input_item(content: Any) -> dict[str, Any]:
    return {"role": "user", "content": _user_content_parts(content)}


def _user_content_parts(content: Any) -> list[dict[str, Any]]:
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
            "GitHub Copilot responses adapter supports only image media blocks; "
            f"received {media_type}",
            retryable=False,
        )
    return {
        "type": "input_image",
        "image_url": f"data:{media_type};base64,{base64_data}",
    }


def _tool_call_to_function_call(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": _string_or(tool_call.get("id"), ""),
        "name": _function_call_name(tool_call),
        "arguments": _serialize_tool_arguments(_function_call_arguments(tool_call)),
    }


def _tool_message_to_function_output(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": _string_or(message.get("tool_call_id"), ""),
        "output": _string_or(message.get("content"), ""),
    }


def _reasoning_meta_input_items(reasoning_meta: Any) -> list[dict[str, Any]]:
    if not isinstance(reasoning_meta, Mapping):
        return []
    for key in _REASONING_META_KEYS:
        items = reasoning_meta.get(key)
        if isinstance(items, list):
            return [dict(item) for item in items if _is_reasoning_item(item)]
    return []


def _is_reasoning_item(item: Any) -> bool:
    return isinstance(item, Mapping) and item.get("type") == "reasoning"


def _apply_responses_tools(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: ResponsesRequestPolicy,
) -> None:
    tools = request_kwargs.pop("tools", None)
    tool_choice = request_kwargs.pop("tool_choice", None)
    if not policy.supports_tools or not tools:
        return
    payload["tools"] = [
        _to_responses_function_tool(tool) for tool in tools if isinstance(tool, Mapping)
    ]
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
        }
    return {
        "type": "function",
        "name": _function_call_name(tool),
        "description": _function_description(tool),
        "parameters": _function_parameters(tool),
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
        if safe_effort is not None and safe_effort != "none":
            payload["reasoning"] = {"effort": safe_effort, "summary": "auto"}
    if payload.get("reasoning") or include_reasoning is True:
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
    request_kwargs.pop("prompt_cache_key", None)
    request_kwargs.pop("prompt_cache_retention", None)
    max_tokens = request_kwargs.pop("max_tokens", None)
    if max_tokens is not None and "max_output_tokens" not in request_kwargs:
        payload["max_output_tokens"] = max_tokens
    max_output_tokens = request_kwargs.pop("max_output_tokens", None)
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    if "temperature" in request_kwargs and _supports_responses_temperature(policy):
        payload["temperature"] = request_kwargs["temperature"]
    if "top_p" in request_kwargs:
        payload["top_p"] = request_kwargs["top_p"]
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
    for item in output_items:
        if item.get("type") != "function_call":
            continue
        arguments = _parse_tool_arguments(_function_call_arguments(item))
        if arguments is None:
            continue
        tool_calls.append(
            {
                "id": _function_call_id(item),
                "name": _function_call_name(item),
                "arguments": arguments,
            }
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
    if reasoning_items:
        meta["reasoning_items"] = reasoning_items
    if encrypted_items:
        meta["encrypted_content"] = [item["encrypted_content"] for item in encrypted_items]
    return meta or None


def _extract_responses_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(input_tokens, int) and not isinstance(output_tokens, int):
        return None
    normalized = {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }
    cache_read_tokens = _responses_cached_input_tokens(usage)
    if cache_read_tokens is not None:
        normalized["cache_read_tokens"] = cache_read_tokens
    return normalized


def _responses_cached_input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Read ``input_tokens_details.cached_tokens`` when present.

    Cached tokens are a subset of ``input_tokens`` on the Responses
    wire, so no input-token adjustment is needed.
    """
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
    if not isinstance(details, Mapping):
        return None
    cached_tokens = details.get("cached_tokens")
    return cached_tokens if isinstance(cached_tokens, int) else None


def _iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    event_name = ""
    data_parts: list[str] = []
    for raw_chunk in lines:
        for raw_line in raw_chunk.splitlines():
            line = raw_line.rstrip("\r\n")
            if not line:
                yield from _flush_sse_event(event_name, data_parts)
                event_name = ""
                data_parts = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                data_parts.append(line[len("data:") :].strip())
    yield from _flush_sse_event(event_name, data_parts)


def _flush_sse_event(
    event_name: str,
    data_parts: list[str],
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not data_parts:
        return
    data = "\n".join(data_parts).strip()
    if not data or data == RESPONSES_DONE_MARKER:
        return
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"GitHub Copilot Responses provider sent malformed JSON in stream: {exc.msg}",
            retryable=False,
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProviderError(
            "GitHub Copilot Responses provider sent non-object JSON in stream",
            retryable=False,
        )
    yield event_name, parsed


def _event_type(event_name: str, event_data: Mapping[str, Any]) -> str:
    event_type = event_data.get("type")
    if isinstance(event_type, str) and event_type:
        return event_type
    return event_name


def _text_delta(event_data: Mapping[str, Any], delta_type: str) -> list[dict[str, Any]]:
    delta = event_data.get("delta")
    if not isinstance(delta, str) or not delta:
        return []
    return [{"type": delta_type, "text": delta}]


def _reasoning_delta(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    deltas = _text_delta(event_data, "reasoning_delta")
    if deltas:
        state.emitted_reasoning_text += deltas[0]["text"]
    return deltas


def _function_arguments_delta(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    tool_call_id = _stream_tool_call_id(event_data, state)
    delta = event_data.get("delta")
    if not isinstance(delta, str) or not delta:
        return []
    arguments_delta = _record_tool_argument_delta(tool_call_id, delta, state)
    if arguments_delta is None:
        return []
    return [
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "name_delta": "",
            "arguments_delta": arguments_delta,
        }
    ]


def _output_item_event_deltas(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    item = event_data.get("item")
    if not isinstance(item, Mapping):
        return []
    if item.get("type") == "reasoning":
        reasoning_deltas: list[dict[str, Any]] = []
        reasoning = _joined_or_none(_extract_reasoning_parts([item]))
        reasoning_delta = _reasoning_backfill_delta(reasoning, state)
        if reasoning_delta is not None:
            reasoning_deltas.append({"type": "reasoning_delta", "text": reasoning_delta})
        reasoning_deltas.append(
            {"type": "reasoning_meta", "reasoning_meta": {"reasoning_items": [dict(item)]}}
        )
        return reasoning_deltas
    if item.get("type") != "function_call":
        return []
    tool_call_id = _function_call_id(item)
    _remember_stream_tool_call_id(event_data, tool_call_id, state)
    item_own_id = _non_empty_string_or_none(item.get("id"))
    if item_own_id is not None and item_own_id != tool_call_id:
        state.item_id_to_call_id[item_own_id] = tool_call_id
    deltas: list[dict[str, Any]] = []
    name = _function_call_name(item)
    arguments = item.get("arguments")
    name_delta = ""
    arguments_delta: str | None = None
    if isinstance(name, str) and name and tool_call_id not in state.emitted_tool_names:
        name_delta = name
        state.emitted_tool_names.add(tool_call_id)
    if isinstance(arguments, str) and arguments:
        arguments_delta = _record_tool_argument_delta(tool_call_id, arguments, state)
    nested_arguments = _function_call_arguments(item)
    if arguments_delta is None and isinstance(nested_arguments, str) and nested_arguments:
        arguments_delta = _record_tool_argument_delta(tool_call_id, nested_arguments, state)
    if name_delta or arguments_delta:
        deltas.append(
            {
                "type": "tool_call_delta",
                "id": tool_call_id,
                "name_delta": name_delta,
                "arguments_delta": arguments_delta or "",
            }
        )
    return deltas


def _record_tool_argument_delta(
    tool_call_id: str,
    delta: str,
    state: ResponsesStreamState,
) -> str | None:
    emitted_arguments = state.emitted_tool_arguments.get(tool_call_id, "")
    if not emitted_arguments:
        state.emitted_tool_arguments[tool_call_id] = delta
        return delta
    if delta == emitted_arguments:
        return None
    if delta.startswith(emitted_arguments):
        suffix = delta[len(emitted_arguments) :]
        state.emitted_tool_arguments[tool_call_id] = delta
        return suffix or None

    state.emitted_tool_arguments[tool_call_id] = emitted_arguments + delta
    return delta


def _completed_event_deltas(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    response = event_data.get("response")
    if not isinstance(response, Mapping):
        response = event_data
    deltas: list[dict[str, Any]] = []
    output_items = _mapping_list(response.get("output"))
    reasoning = _joined_or_none(_extract_reasoning_parts(output_items))
    reasoning_backfill = _reasoning_backfill_delta(reasoning, state)
    if reasoning_backfill is not None:
        deltas.append({"type": "reasoning_delta", "text": reasoning_backfill})
    reasoning_meta = _extract_reasoning_meta(response, output_items)
    if reasoning_meta is not None:
        deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})
    usage = _extract_responses_usage(response.get("usage"))
    if usage is not None:
        deltas.append({"type": "usage", **usage})
    deltas.append({"type": "finish", "reason": _responses_finish_reason(response, state)})
    return deltas


def _reasoning_backfill_delta(
    reasoning: str | None,
    state: ResponsesStreamState,
) -> str | None:
    if reasoning is None:
        return None
    emitted_reasoning = state.emitted_reasoning_text
    if not emitted_reasoning:
        state.emitted_reasoning_text = reasoning
        return reasoning
    if reasoning == emitted_reasoning or emitted_reasoning.endswith(reasoning):
        return None
    if reasoning.startswith(emitted_reasoning):
        backfill = reasoning[len(emitted_reasoning) :]
        state.emitted_reasoning_text = reasoning
        return backfill or None

    overlap = _suffix_prefix_overlap(emitted_reasoning, reasoning)
    if overlap > 0:
        backfill = reasoning[overlap:]
        state.emitted_reasoning_text += backfill
        return backfill or None
    return None


def _suffix_prefix_overlap(left: str, right: str) -> int:
    max_overlap = min(len(left), len(right))
    for overlap in range(max_overlap, 0, -1):
        if left.endswith(right[:overlap]):
            return overlap
    return 0


def _responses_finish_reason(
    response: Mapping[str, Any],
    state: ResponsesStreamState | None = None,
) -> str:
    output_items = _mapping_list(response.get("output"))
    if any(item.get("type") == "function_call" for item in output_items):
        return "tool_calls"
    if state is not None and _stream_has_tool_calls(state):
        return "tool_calls"
    status = response.get("status")
    if status == "completed":
        return "stop"
    return "stop"


def _stream_has_tool_calls(state: ResponsesStreamState) -> bool:
    return bool(
        state.tool_call_ids_by_output_index
        or state.emitted_tool_names
        or state.emitted_tool_arguments
    )


def _responses_error_message(event_data: Mapping[str, Any]) -> str:
    error = event_data.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    message = event_data.get("message")
    if isinstance(message, str) and message:
        return message
    return "GitHub Copilot Responses request failed"


def _stream_tool_call_id(event_data: Mapping[str, Any], state: ResponsesStreamState) -> str:
    call_id = _non_empty_string_or_none(event_data.get("call_id"))
    if call_id is not None:
        return call_id
    item_id = _non_empty_string_or_none(event_data.get("item_id"))
    if item_id is not None:
        canonical_id = state.item_id_to_call_id.get(item_id)
        if canonical_id:
            return canonical_id
    output_index = event_data.get("output_index")
    if isinstance(output_index, int):
        existing_id = state.tool_call_ids_by_output_index.get(output_index)
        if existing_id:
            return existing_id
        generated_id = f"tool_call_{output_index}"
        state.tool_call_ids_by_output_index[output_index] = generated_id
        return generated_id
    if item_id is not None:
        return item_id
    return "tool_call_0"


def _remember_stream_tool_call_id(
    event_data: Mapping[str, Any],
    tool_call_id: str,
    state: ResponsesStreamState,
) -> None:
    output_index = event_data.get("output_index")
    if isinstance(output_index, int):
        state.tool_call_ids_by_output_index[output_index] = tool_call_id


def _function_call_id(item: Mapping[str, Any]) -> str:
    call_id = _non_empty_string_or_none(item.get("call_id"))
    if call_id is not None:
        return call_id
    item_id = _non_empty_string_or_none(item.get("id"))
    return item_id if item_id is not None else "tool_call_0"


def _function_mapping(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    function = item.get("function")
    return function if isinstance(function, Mapping) else None


def _function_call_name(item: Mapping[str, Any]) -> str:
    function = _function_mapping(item)
    if function is not None:
        nested_name = _non_empty_string_or_none(function.get("name"))
        if nested_name is not None:
            return nested_name
    name = _non_empty_string_or_none(item.get("name"))
    return name if name is not None else ""


def _function_description(item: Mapping[str, Any]) -> str:
    description = item.get("description")
    if isinstance(description, str):
        return description
    function = _function_mapping(item)
    if function is None:
        return ""
    return _string_or(function.get("description"), "")


def _function_parameters(item: Mapping[str, Any]) -> Any:
    parameters = item.get("parameters")
    if isinstance(parameters, Mapping):
        return parameters
    function = _function_mapping(item)
    if function is None:
        return {}
    nested_parameters = function.get("parameters")
    return nested_parameters if isinstance(nested_parameters, Mapping) else {}


def _function_call_arguments(item: Mapping[str, Any]) -> Any:
    arguments = item.get("arguments")
    if _has_function_arguments(arguments):
        return arguments
    function = _function_mapping(item)
    if function is None:
        return None
    return function.get("arguments")


def _has_function_arguments(arguments: Any) -> bool:
    if isinstance(arguments, Mapping):
        return True
    return isinstance(arguments, str) and bool(arguments)


def _serialize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments if arguments is not None else {}, separators=(",", ":"))


def _parse_tool_arguments(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, Mapping):
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
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _joined_or_none(parts: list[str]) -> str | None:
    return "".join(parts) if parts else None


def _string_or(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _non_empty_string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
