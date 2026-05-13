"""GitHub Copilot ``/v1/messages`` protocol helpers.

The helpers in this module intentionally implement a conservative,
Anthropic-like subset for Copilot's Messages endpoint. They build request
payloads from vBot's canonical chat dictionaries and normalize provider
responses/stream events back to the adapter delta contract consumed by chat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.providers.errors import ProviderError
from core.providers.github_copilot_policy import GitHubCopilotModelPolicy
from core.providers.openai_compatible import DEFAULT_MAX_OUTPUT_TOKENS

SSE_DATA_PREFIX = "data: "
SSE_EVENT_PREFIX = "event: "
SSE_DONE_MARKER = "[DONE]"

TEXT_BLOCK_TYPE = "text"
TOOL_USE_BLOCK_TYPE = "tool_use"
TOOL_RESULT_BLOCK_TYPE = "tool_result"
THINKING_BLOCK_TYPE = "thinking"
REDACTED_THINKING_BLOCK_TYPE = "redacted_thinking"
REASONING_META_CONTENT_BLOCKS = "content_blocks"

MESSAGE_TOOL_STOP_REASONS = {"tool_use"}
MESSAGE_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "pause_turn",
    "refusal",
    "stop_sequence",
}

SAFE_TOP_LEVEL_PARAMETERS = {
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
}
SAFE_THINKING_TYPES = {"adaptive", "disabled", "enabled"}
SAFE_TOOL_CHOICE_TYPES = {"auto", "any", "tool"}


@dataclass
class CopilotMessagesStreamState:
    """Mutable state for normalizing one Copilot Messages SSE stream."""

    content_blocks_by_index: dict[int, dict[str, Any]] = field(default_factory=dict)
    reasoning_meta_blocks: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None


def build_copilot_messages_payload(
    messages: list[dict[str, Any]],
    *,
    model_id: str,
    policy: GitHubCopilotModelPolicy,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a conservative Copilot ``/v1/messages`` request payload."""

    request_kwargs = policy.filter_request_kwargs(kwargs)
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

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": _to_copilot_messages(conversation_messages),
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    _apply_safe_messages_tools(payload, request_kwargs, policy)
    _apply_safe_messages_thinking(payload, request_kwargs, policy)
    _apply_safe_top_level_parameters(payload, request_kwargs)
    return payload


def normalize_copilot_messages_response(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Copilot Messages response to canonical assistant fields."""

    content_blocks = response.get("content", [])
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": _extract_messages_text(content_blocks),
        "reasoning": _extract_messages_reasoning(content_blocks),
        "reasoning_meta": _extract_messages_reasoning_meta(content_blocks),
        "tool_calls": _extract_messages_tool_calls(content_blocks),
    }
    usage = _extract_messages_usage(response)
    if usage is not None:
        normalized["usage"] = usage
    return normalized


def normalize_copilot_messages_sse_line(
    line: str,
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    """Normalize one Anthropic-like Copilot SSE line into vBot deltas."""

    if line.startswith(SSE_EVENT_PREFIX):
        return []
    if not line.startswith(SSE_DATA_PREFIX):
        return []

    data = line[len(SSE_DATA_PREFIX) :]
    if not data.strip() or data.strip() == SSE_DONE_MARKER:
        return []
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []
    return normalize_copilot_messages_stream_event(event, state)


def normalize_copilot_messages_stream_event(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    """Normalize one parsed Copilot Messages stream event."""

    event_type = event.get("type")
    if event_type == "error":
        raise ProviderError(_messages_error_detail(event), retryable=False)
    if event_type == "message_start":
        _capture_message_start_usage(event, state)
        return []
    if event_type == "content_block_start":
        return _normalize_content_block_start(event, state)
    if event_type == "content_block_delta":
        return _normalize_content_block_delta(event, state)
    if event_type == "content_block_stop":
        return _normalize_content_block_stop(event, state)
    if event_type == "message_delta":
        return _normalize_message_delta(event, state)
    return []


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
    return {
        "type": TOOL_RESULT_BLOCK_TYPE,
        "tool_use_id": str(message.get("tool_call_id", "")),
        "content": _text_from_content(message.get("content", "")),
    }


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
        if block.get("type") != TEXT_BLOCK_TYPE:
            continue
        text = block.get("text")
        if isinstance(text, str):
            blocks.append({"type": TEXT_BLOCK_TYPE, "text": text})
    return blocks


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
        signature = block.get("signature")
        if isinstance(thinking, str):
            safe_block["thinking"] = thinking
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
    policy: GitHubCopilotModelPolicy,
) -> None:
    tools = kwargs.pop("tools", None)
    tool_choice = kwargs.pop("tool_choice", None)
    kwargs.pop("parallel_tool_calls", None)
    if not policy.supports_tools or not isinstance(tools, list) or not tools:
        return

    payload["tools"] = [tool for tool in (_safe_tool(tool) for tool in tools) if tool]
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


def _apply_safe_messages_thinking(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
    policy: GitHubCopilotModelPolicy,
) -> None:
    thinking = kwargs.pop("thinking", None)
    output_config = kwargs.pop("output_config", None)
    thinking_budget = kwargs.pop("thinking_budget", None)
    thinking_effort = kwargs.pop("thinking_effort", "")
    kwargs.pop("reasoning_effort", None)
    kwargs.pop("reasoning", None)
    kwargs.pop("include_reasoning", None)

    if not policy.allows_any_reasoning_controls:
        return

    safe_thinking = _safe_explicit_thinking(thinking, policy)
    if safe_thinking is None:
        safe_thinking = _thinking_from_budget(thinking_budget, policy)
    if safe_thinking is None:
        safe_thinking = _thinking_from_effort(thinking_effort, policy)
    if safe_thinking is not None:
        payload["thinking"] = safe_thinking

    safe_output_config = _safe_output_config(output_config, policy)
    if safe_output_config is None and isinstance(thinking_effort, str):
        safe_output_config = _output_config_from_effort(thinking_effort, policy)
    if safe_output_config is not None:
        payload["output_config"] = safe_output_config


def _safe_explicit_thinking(
    thinking: Any,
    policy: GitHubCopilotModelPolicy,
) -> dict[str, Any] | None:
    if not isinstance(thinking, dict):
        return None
    thinking_type = thinking.get("type")
    if thinking_type not in SAFE_THINKING_TYPES:
        return None
    if thinking_type == "adaptive" and not policy.supports_adaptive_thinking:
        return None
    if thinking_type == "enabled":
        budget = thinking.get("budget_tokens")
        if not _budget_allowed(budget, policy):
            return None
        return {"type": "enabled", "budget_tokens": budget}
    safe_thinking = {"type": thinking_type}
    display = thinking.get("display")
    if thinking_type == "adaptive" and display in {"summarized", "omitted"}:
        safe_thinking["display"] = display
    return safe_thinking


def _thinking_from_budget(
    thinking_budget: Any,
    policy: GitHubCopilotModelPolicy,
) -> dict[str, Any] | None:
    if not _budget_allowed(thinking_budget, policy):
        return None
    return {"type": "enabled", "budget_tokens": thinking_budget}


def _thinking_from_effort(
    thinking_effort: Any,
    policy: GitHubCopilotModelPolicy,
) -> dict[str, Any] | None:
    if not isinstance(thinking_effort, str) or not thinking_effort:
        return None
    if thinking_effort == "none":
        return {"type": "disabled"}
    if not policy.supports_adaptive_thinking or not policy.allows_reasoning_effort(thinking_effort):
        return None
    return {"type": "adaptive", "display": "summarized"}


def _safe_output_config(
    output_config: Any,
    policy: GitHubCopilotModelPolicy,
) -> dict[str, Any] | None:
    if not isinstance(output_config, dict):
        return None
    effort = output_config.get("effort")
    if not isinstance(effort, str) or not policy.allows_reasoning_effort(effort):
        return None
    return {"effort": effort}


def _output_config_from_effort(
    thinking_effort: str,
    policy: GitHubCopilotModelPolicy,
) -> dict[str, Any] | None:
    if thinking_effort in {"", "none", "minimal"}:
        return None
    if not policy.allows_reasoning_effort(thinking_effort):
        return None
    return {"effort": thinking_effort}


def _budget_allowed(value: Any, policy: GitHubCopilotModelPolicy) -> bool:
    if not policy.supports_thinking_budget or isinstance(value, bool) or not isinstance(value, int):
        return False
    min_budget = policy.facts.min_thinking_budget
    max_budget = policy.facts.max_thinking_budget
    if min_budget is not None and value < min_budget:
        return False
    return not (max_budget is not None and value > max_budget)


def _apply_safe_top_level_parameters(
    payload: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    payload["max_tokens"] = _resolve_messages_max_tokens(kwargs)
    for parameter_name in SAFE_TOP_LEVEL_PARAMETERS:
        if parameter_name in kwargs:
            payload[parameter_name] = kwargs[parameter_name]


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


def _extract_messages_text(content_blocks: Any) -> str | None:
    text_parts = [
        block["text"]
        for block in _content_blocks(content_blocks)
        if block.get("type") == TEXT_BLOCK_TYPE and isinstance(block.get("text"), str)
    ]
    return "".join(text_parts) if text_parts else None


def _extract_messages_reasoning(content_blocks: Any) -> str | None:
    reasoning_parts = [
        block["thinking"]
        for block in _content_blocks(content_blocks)
        if block.get("type") == THINKING_BLOCK_TYPE and isinstance(block.get("thinking"), str)
    ]
    return "".join(reasoning_parts) if reasoning_parts else None


def _extract_messages_reasoning_meta(content_blocks: Any) -> dict[str, Any] | None:
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
    tool_calls: list[dict[str, Any]] = []
    for block in _content_blocks(content_blocks):
        if block.get("type") != TOOL_USE_BLOCK_TYPE:
            continue
        tool_id = block.get("id")
        name = block.get("name")
        if not isinstance(tool_id, str) or not isinstance(name, str):
            continue
        tool_input = block.get("input")
        tool_calls.append(
            {
                "id": tool_id,
                "name": name,
                "arguments": dict(tool_input) if isinstance(tool_input, dict) else {},
            }
        )
    return tool_calls or None


def _extract_messages_usage(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }


def _content_blocks(content_blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(content_blocks, list):
        return []
    return [block for block in content_blocks if isinstance(block, dict)]


def _capture_message_start_usage(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> None:
    message = event.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("input_tokens")
    if isinstance(input_tokens, int):
        state.input_tokens = input_tokens


def _normalize_content_block_start(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    index = _stream_index(event)
    content_block = event.get("content_block")
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
        state.content_blocks_by_index[index] = block_state
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

    safe_reasoning_block = _safe_reasoning_block(content_block)
    if safe_reasoning_block:
        block_state["block"] = safe_reasoning_block
    state.content_blocks_by_index[index] = block_state
    return []


def _normalize_content_block_delta(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    index = _stream_index(event)
    delta = event.get("delta")
    if index is None or not isinstance(delta, dict):
        return []
    block_state = state.content_blocks_by_index.get(index, {})
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        text = delta.get("text")
        return [{"type": "content_delta", "text": text}] if isinstance(text, str) and text else []
    if delta_type == "thinking_delta":
        return _normalize_thinking_delta(delta, block_state)
    if delta_type == "signature_delta":
        _apply_signature_delta(delta, block_state)
        return []
    if delta_type == "input_json_delta":
        return _normalize_tool_input_delta(delta, block_state)
    return []


def _normalize_content_block_stop(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    index = _stream_index(event)
    if index is None:
        return []
    block_state = state.content_blocks_by_index.get(index, {})
    block = block_state.get("block")
    safe_block = _safe_reasoning_block(block)
    if not safe_block:
        return []

    state.reasoning_meta_blocks.append(safe_block)
    return [
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                REASONING_META_CONTENT_BLOCKS: [
                    dict(meta_block) for meta_block in state.reasoning_meta_blocks
                ]
            },
        }
    ]


def _normalize_message_delta(
    event: dict[str, Any],
    state: CopilotMessagesStreamState,
) -> list[dict[str, Any]]:
    normalized_deltas: list[dict[str, Any]] = []
    delta = event.get("delta")
    if isinstance(delta, dict):
        stop_reason = delta.get("stop_reason")
        if stop_reason is not None:
            normalized_deltas.append(
                {
                    "type": "finish",
                    "reason": _normalize_stop_reason(
                        stop_reason,
                        has_tool_calls=_has_stream_tool_calls(state),
                    ),
                }
            )
    usage = event.get("usage")
    if isinstance(usage, dict):
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int) and state.input_tokens is not None:
            normalized_deltas.append(
                {
                    "type": "usage",
                    "input_tokens": state.input_tokens,
                    "output_tokens": output_tokens,
                }
            )
    return normalized_deltas


def _normalize_thinking_delta(
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


def _apply_signature_delta(delta: dict[str, Any], block_state: dict[str, Any]) -> None:
    signature = delta.get("signature")
    block = block_state.get("block")
    if isinstance(signature, str) and signature and isinstance(block, dict):
        block["signature"] = signature


def _normalize_tool_input_delta(
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


def _normalize_stop_reason(stop_reason: Any, *, has_tool_calls: bool) -> str:
    if stop_reason in MESSAGE_TOOL_STOP_REASONS:
        return "tool_calls"
    if stop_reason in MESSAGE_STOP_REASONS:
        return "stop"
    return "tool_calls" if has_tool_calls else "stop"


def _stream_index(event: dict[str, Any]) -> int | None:
    index = event.get("index")
    return index if isinstance(index, int) else None


def _has_stream_tool_calls(state: CopilotMessagesStreamState) -> bool:
    return any(
        block.get("type") == TOOL_USE_BLOCK_TYPE for block in state.content_blocks_by_index.values()
    )


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
