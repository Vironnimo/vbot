"""Canonical ingestion of normalized Provider responses and Usage."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from core.chat.errors import ChatMessageValidationError
from core.chat.messages import (
    USAGE_INPUT_TOKENS_ESTIMATED_FIELD,
    USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD,
    ChatMessage,
    JsonObject,
    ToolCall,
    usage_token_is_estimated,
)
from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
    normalize_tool_call_candidates,
)
from core.tools import registry_tool_name
from core.utils.tokens import estimate_output_tokens, estimate_request_input_tokens


def _assistant_message_from_response(
    model: str,
    response: JsonObject,
    *,
    reasoning_scope: str | None = None,
    reasoning_timing: JsonObject | None = None,
    interrupted: bool = False,
    interruption_cause: str | None = None,
    message_id: str | None = None,
) -> ChatMessage:
    tool_calls = _parse_response_tool_calls(response.get("tool_calls"))
    reasoning = _nullable_response_string(response, "reasoning")
    reasoning_meta = _response_reasoning_meta(response)
    content = _nullable_response_string(response, "content")
    return ChatMessage.assistant(
        model=model,
        content=_sanitize_unpaired_surrogates(content) if content else content,
        reasoning=_sanitize_unpaired_surrogates(reasoning) if reasoning else reasoning,
        reasoning_meta=reasoning_meta,
        reasoning_summary=(
            [_sanitize_unpaired_surrogates(part) for part in response["reasoning_summary"]]
            if isinstance(response.get("reasoning_summary"), list)
            and all(isinstance(part, str) for part in response["reasoning_summary"])
            else None
        ),
        reasoning_scope=(
            reasoning_scope if reasoning is not None or reasoning_meta is not None else None
        ),
        reasoning_timing=reasoning_timing if reasoning is not None else None,
        phase=_response_phase(response),
        usage=response.get("usage"),
        tool_calls=tool_calls,
        interrupted=interrupted,
        interruption_cause=interruption_cause,
        message_id=message_id,
    )


def _complete_usage_with_estimates(
    message: ChatMessage,
    request_messages: list[JsonObject],
    *,
    estimated_input_tokens: int | None = None,
) -> ChatMessage:
    """Fill only missing usage counters, mark each as estimated, and summarize in ``estimated``."""

    usage = dict(message.usage or {})
    reported_input_tokens = _optional_usage_token_count(usage.get("input_tokens"))
    if reported_input_tokens is None or (reported_input_tokens == 0 and request_messages):
        estimated_input = estimated_input_tokens
        if estimated_input is None:
            estimated_input, _ = estimate_request_input_tokens(request_messages)
        usage["input_tokens"] = estimated_input
        usage[USAGE_INPUT_TOKENS_ESTIMATED_FIELD] = True

    if _optional_usage_token_count(usage.get("output_tokens")) is None:
        # A reported reasoning count is part of the output; only the rest is estimated.
        usage["output_tokens"] = estimate_output_tokens(
            message.to_dict(),
            reasoning_tokens=_optional_usage_token_count(usage.get("reasoning_tokens")),
        )
        usage[USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD] = True

    if usage_token_is_estimated(usage, "input_tokens") or usage_token_is_estimated(
        usage, "output_tokens"
    ):
        usage["estimated"] = True
    else:
        usage.pop("estimated", None)
    return replace(message, usage=usage)


def _optional_usage_token_count(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _nullable_response_string(response: JsonObject, key: str) -> str | None:
    value = response.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ChatMessageValidationError(f"assistant response {key} must be a string or null")


def _response_reasoning_meta(response: JsonObject) -> JsonObject | None:
    reasoning_meta = response.get("reasoning_meta")
    if reasoning_meta is None:
        return None
    if not isinstance(reasoning_meta, dict):
        raise ChatMessageValidationError("assistant response reasoning_meta must be an object")
    return dict(reasoning_meta)


def _response_phase(response: JsonObject) -> str | None:
    phase = response.get("phase")
    if phase is not None:
        if not isinstance(phase, str):
            raise ChatMessageValidationError("assistant response phase must be a string or null")
        return phase
    reasoning_meta = response.get("reasoning_meta")
    if not isinstance(reasoning_meta, dict):
        return None
    response_output = reasoning_meta.get("response_output")
    if not isinstance(response_output, list):
        return None
    for item in response_output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        item_phase = item.get("phase")
        if isinstance(item_phase, str) and item_phase:
            return item_phase
    return None


def _parse_response_tool_calls(value: Any) -> list[ToolCall] | None:
    """Normalize every recognizable Provider call, including malformed attempts."""

    if value is None:
        return None

    if isinstance(value, list):
        raw_calls = value
    elif isinstance(value, dict):
        # A Provider occasionally collapses a one-element Tool Call array into
        # an object. It is still addressable, so preserve it as one attempt.
        raw_calls = [value]
    else:
        # Provider response-shape corruption must not turn a Tool-level problem
        # into a Run-level failure. Preserve one synthetic rejected attempt so
        # the Model receives a correlated failure Result and can recover.
        raw_calls = [{"arguments": value}]

    tool_calls: list[ToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        call = raw_call if isinstance(raw_call, dict) else {}
        candidates = normalize_tool_call_candidates(
            tool_call_id=call.get("id"),
            name=call.get("name"),
            arguments=call.get("arguments"),
            fallback_id=f"tool_call_{index}",
            rejection=call.get(TOOL_CALL_REJECTION_FIELD),
            argument_sequence_index=call.get(TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD),
            argument_sequence_length=call.get(TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD),
        )
        for candidate in candidates:
            name = candidate.get("name")
            if isinstance(name, str):
                # The Model may know a Tool under its host-specific name.
                candidate = {**candidate, "name": registry_tool_name(name)}
            tool_calls.append(ToolCall.from_dict(candidate))
    return tool_calls or None


def _sanitize_unpaired_surrogates(text: str) -> str:
    """Replace unpaired UTF-16 surrogate code points a Model may emit.

    Models served via Ollama (Kimi, GLM, Qwen) can return lone surrogates
    (U+D800–U+DFFF) in their output; those crash ``json.dumps`` with
    ``ensure_ascii=False`` at the session persistence boundary. A Python
    ``str`` never carries paired surrogates — an astral character is one code
    point — so every ``U+D800–U+DFFF`` code point in a str is lone by
    definition and becomes exactly one replacement character.
    """

    return _UNPAIRED_SURROGATE_PATTERN.sub("\ufffd", text)


# Lone surrogate code points: impossible to encode to UTF-8 and illegal in JSON
# written with ensure_ascii=False.
_UNPAIRED_SURROGATE_PATTERN = re.compile("[\ud800-\udfff]")
