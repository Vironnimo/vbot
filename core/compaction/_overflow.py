"""Smaller summary-request views after a confirmed Provider Context rejection.

Only request-local copies change. Tool identities and their correlated records
remain intact; older complete exchanges are the last resort after clipping Tool
observations. The latest real User input, prior checkpoint context and Compaction
instruction are never removed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.chat.wire_shaping import system_reminder_request_message
from core.compaction._cycles import ToolCycleSuffix
from core.providers.adapter import is_request_context_message

OVERFLOW_TOOL_OMISSION = (
    "[... {omitted} characters omitted from this Tool result for Compaction ...]"
)
OVERFLOW_INPUT_NOTICE = (
    "Some older conversation exchanges were omitted because the Provider rejected the "
    "Compaction request's size. Summarize only the remaining evidence; "
    "do not infer omitted details."
)
OVERFLOW_SUMMARY_NOTICE = (
    "Some older conversation exchanges were omitted from this summary's input after the "
    "Provider rejected its size. The original Session history is unchanged."
)
# Exhaust observation reductions before dropping any User exchange. Three
# altered requests are the absolute maximum; the last aims at half the input.
OVERFLOW_TOOL_CHARACTERS = (4_000, 1_000, 256)
OVERFLOW_TARGET_RATIO = 0.50


@dataclass(frozen=True)
class OverflowProjection:
    messages: list[dict[str, Any]]
    omitted_exchanges: int = 0


def smaller_compaction_request(
    messages: Sequence[dict[str, Any]],
    *,
    tool_characters: int,
    target_tokens: int,
    omit_old_exchanges: bool,
    estimate: Callable[[Sequence[dict[str, Any]]], int],
) -> OverflowProjection:
    """Reduce Tool observations before dropping oldest complete User exchanges.

    All candidates derive from the original request, not an already clipped
    view. Retaining the last exchange includes its Assistant and Tool records,
    though its Tool observations can be shortened just like older ones.
    """
    projected = [_bounded_tool_message(message, tool_characters) for message in messages]
    starts = _safe_exchange_starts(projected)
    if not omit_old_exchanges or len(starts) < 2 or estimate(projected) <= target_tokens:
        return OverflowProjection(projected)

    first = starts[0]
    notice = system_reminder_request_message(OVERFLOW_INPUT_NOTICE)
    candidate = projected
    omitted = 0
    for start in starts[1:]:
        omitted += 1
        # A later User boundary never divides a Tool batch. Keep system messages
        # even in unusual imported histories that place one inside an exchange.
        candidate = [
            *projected[:first],
            *(message for message in projected[first:start] if message.get("role") == "system"),
            *projected[start:-1],
            notice,
            projected[-1],
        ]
        if estimate(candidate) <= target_tokens:
            break
    return OverflowProjection(candidate, omitted)


def _bounded_tool_message(message: dict[str, Any], characters: int) -> dict[str, Any]:
    if message.get("role") != "tool":
        return message
    content = message.get("content")
    if isinstance(content, str):
        bounded = _clip(content, characters)
        return message if bounded is content else {**message, "content": bounded}
    if not isinstance(content, list):
        return message
    bounded_blocks = [
        {**block, "text": _clip(block["text"], characters)}
        if isinstance(block, dict) and isinstance(block.get("text"), str)
        else block
        for block in content
    ]
    return message if bounded_blocks == content else {**message, "content": bounded_blocks}


def _clip(text: str, characters: int) -> str:
    if len(text) <= characters:
        return text
    # Keep both the outcome near the start and diagnostic detail at the end.
    half = characters // 2
    omitted = len(text) - characters
    bounded = (
        f"{text[:half]}\n{OVERFLOW_TOOL_OMISSION.format(omitted=omitted)}\n"
        f"{text[-(characters - half) :]}"
    )
    return bounded if len(bounded) < len(text) else text


def _safe_exchange_starts(messages: Sequence[dict[str, Any]]) -> list[int]:
    """User boundaries whose remaining native Tool cycles are complete.

    Walk the request projection backwards like canonical Tail validation; only
    these boundaries can discard older exchanges without orphaning a Result.
    """
    starts: list[int] = []
    cycles = ToolCycleSuffix()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        role = message.get("role")
        call_id = message.get("tool_call_id")
        if role == "tool" and not isinstance(call_id, str):
            break
        if not cycles.accepts(
            role,
            call_ids=(call.get("id") for call in message.get("tool_calls") or ())
            if role == "assistant"
            else (),
            result_id=call_id,
        ):
            break
        if role == "user" and not is_request_context_message(message):
            starts.append(index)
    return list(reversed(starts))
