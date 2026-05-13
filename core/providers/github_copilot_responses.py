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
from typing import Any

from core.providers.errors import ProviderError
from core.providers.github_copilot_policy import GitHubCopilotModelPolicy

RESPONSES_DONE_MARKER = "[DONE]"
REASONING_ENCRYPTED_CONTENT_INCLUDE = "reasoning.encrypted_content"
REASONING_SUMMARY_DELTA_EVENTS = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.output_item.reasoning_summary_text.delta",
}
RESPONSES_ERROR_EVENTS = {"error", "response.failed", "response.incomplete"}
_REASONING_META_KEYS = ("reasoning_items", "response_output")


@dataclass
class ResponsesStreamState:
    """State needed to normalize one Responses SSE stream."""

    tool_call_ids_by_output_index: dict[int, str] = field(default_factory=dict)
    emitted_tool_names: set[str] = field(default_factory=set)
    emitted_tool_arguments: set[str] = field(default_factory=set)


def build_responses_payload(
    messages: list[dict[str, Any]],
    *,
    model_id: str,
    policy: GitHubCopilotModelPolicy,
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


def iter_responses_sse_deltas(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Parse Responses SSE lines and yield normalized vBot stream deltas."""

    yield from iter_responses_sse_deltas_with_state(lines, ResponsesStreamState())


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
        return _text_delta(event_data, "reasoning_delta")
    if event_type == "response.function_call_arguments.delta":
        return _function_arguments_delta(event_data, state)
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        return _output_item_event_deltas(event_data, state)
    if event_type in {"response.completed", "response.done"}:
        return _completed_event_deltas(event_data)
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
            input_items.append(_text_message_to_input_item("user", message.get("content", "")))
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


def _tool_call_to_function_call(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": _string_or(tool_call.get("id"), ""),
        "name": _string_or(tool_call.get("name"), ""),
        "arguments": json.dumps(tool_call.get("arguments", {}), separators=(",", ":")),
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
    policy: GitHubCopilotModelPolicy,
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
    if tool.get("type") == "function" and "name" in tool:
        return dict(tool)
    function = tool.get("function")
    if isinstance(function, Mapping):
        return {
            "type": "function",
            "name": _string_or(function.get("name"), ""),
            "description": _string_or(function.get("description"), ""),
            "parameters": function.get("parameters", {}),
        }
    return {
        "type": "function",
        "name": _string_or(tool.get("name"), ""),
        "description": _string_or(tool.get("description"), ""),
        "parameters": tool.get("parameters", {}),
    }


def _apply_responses_reasoning(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: GitHubCopilotModelPolicy,
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
    elif (
        isinstance(effort, str)
        and effort
        and effort != "none"
        and policy.allows_reasoning_effort(effort)
    ):
        payload["reasoning"] = {"effort": effort}
    if payload.get("reasoning") or include_reasoning is True:
        _append_include(payload, REASONING_ENCRYPTED_CONTENT_INCLUDE)


def _apply_responses_text_format(
    payload: dict[str, Any],
    request_kwargs: dict[str, Any],
    policy: GitHubCopilotModelPolicy,
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
    policy: GitHubCopilotModelPolicy,
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


def _supports_responses_temperature(policy: GitHubCopilotModelPolicy) -> bool:
    """Return whether this Copilot Responses route should forward ``temperature``.

    GPT-5 reasoning models on the Responses API reject temperature changes, so
    Copilot should omit that field unless the routed model is known to support
    it. The current runtime policy does not expose positive temperature support
    for any Responses-routed Copilot model, so this helper stays conservative
    and omits the field for that endpoint family.
    """

    return policy.endpoint_path != "/responses"


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
        tool_calls.append(
            {
                "id": _function_call_id(item),
                "name": _string_or(item.get("name"), ""),
                "arguments": _parse_tool_arguments(item.get("arguments")),
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
    return {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }


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
    parsed = json.loads(data)
    if isinstance(parsed, Mapping):
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


def _function_arguments_delta(
    event_data: Mapping[str, Any],
    state: ResponsesStreamState,
) -> list[dict[str, Any]]:
    tool_call_id = _stream_tool_call_id(event_data, state)
    delta = event_data.get("delta")
    if not isinstance(delta, str) or not delta:
        return []
    state.emitted_tool_arguments.add(tool_call_id)
    return [
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "name_delta": "",
            "arguments_delta": delta,
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
        return [{"type": "reasoning_meta", "reasoning_meta": {"reasoning_items": [dict(item)]}}]
    if item.get("type") != "function_call":
        return []
    tool_call_id = _function_call_id(item)
    _remember_stream_tool_call_id(event_data, tool_call_id, state)
    deltas: list[dict[str, Any]] = []
    name = item.get("name")
    if isinstance(name, str) and name and tool_call_id not in state.emitted_tool_names:
        deltas.append(
            {
                "type": "tool_call_delta",
                "id": tool_call_id,
                "name_delta": name,
                "arguments_delta": "",
            }
        )
        state.emitted_tool_names.add(tool_call_id)
    arguments = item.get("arguments")
    if (
        isinstance(arguments, str)
        and arguments
        and tool_call_id not in state.emitted_tool_arguments
    ):
        deltas.append(
            {
                "type": "tool_call_delta",
                "id": tool_call_id,
                "name_delta": "",
                "arguments_delta": arguments,
            }
        )
        state.emitted_tool_arguments.add(tool_call_id)
    return deltas


def _completed_event_deltas(event_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    response = event_data.get("response")
    if not isinstance(response, Mapping):
        response = event_data
    deltas: list[dict[str, Any]] = []
    output_items = _mapping_list(response.get("output"))
    reasoning_meta = _extract_reasoning_meta(response, output_items)
    if reasoning_meta is not None:
        deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})
    usage = _extract_responses_usage(response.get("usage"))
    if usage is not None:
        deltas.append({"type": "usage", **usage})
    deltas.append({"type": "finish", "reason": _responses_finish_reason(response)})
    return deltas


def _responses_finish_reason(response: Mapping[str, Any]) -> str:
    status = response.get("status")
    if status == "completed":
        return "stop"
    output_items = _mapping_list(response.get("output"))
    if any(item.get("type") == "function_call" for item in output_items):
        return "tool_calls"
    return "stop"


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
    item_id = event_data.get("item_id") or event_data.get("call_id")
    if isinstance(item_id, str) and item_id:
        return item_id
    output_index = event_data.get("output_index")
    if isinstance(output_index, int):
        existing_id = state.tool_call_ids_by_output_index.get(output_index)
        if existing_id:
            return existing_id
        generated_id = f"tool_call_{output_index}"
        state.tool_call_ids_by_output_index[output_index] = generated_id
        return generated_id
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
    call_id = item.get("call_id") or item.get("id")
    return call_id if isinstance(call_id, str) and call_id else "tool_call_0"


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _joined_or_none(parts: list[str]) -> str | None:
    return "".join(parts) if parts else None


def _string_or(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback
