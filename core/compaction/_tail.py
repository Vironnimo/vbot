"""Provider-safe chronological tail selection for Compaction plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.chat import note_request_messages
from core.chat.messages import (
    COMPACTION_SKILL_NOTE_PREFIX,
    COMPACTION_SUMMARY_NOTE_PREFIX,
    ChatMessage,
    JsonObject,
)
from core.compaction._cycles import ToolCycleSuffix
from core.compaction._tail_types import (
    COMPACTION_USER_QUOTE_PREFIX,
    TAIL_FLOOR_PERCENT,
    TAIL_SOFT_LIMIT_PERCENT,
    RequestTokenEstimator,
)
from core.compaction.errors import (
    CompactionError,
)
from core.utils.tokens import estimate_request_input_tokens


@dataclass(frozen=True)
class _TailPlan:
    """One working-Tail projection, its canonical start and its request start."""

    boundary_id: str
    boundary_index: int
    request_start: int | None
    projected_suffix: tuple[ChatMessage, ...]

    @property
    def retained_messages(self) -> tuple[ChatMessage, ...]:
        return self.projected_suffix


@dataclass(frozen=True)
class _TailCandidate:
    """One provider-safe Tail start with its request-side size."""

    start_index: int
    request_start: int | None
    tokens: int


def _plan_working_tail(
    messages: list[ChatMessage],
    tail_tokens: int,
    *,
    request_messages: tuple[JsonObject, ...] | None = None,
    estimate_tail_tokens: RequestTokenEstimator | None = None,
) -> _TailPlan:
    """Choose a chronological suffix of whole steps that reaches ``tail_tokens``.

    The first cut at or above ``tail_tokens`` wins while it stays within the
    soft limit; otherwise the largest cut down to the floor; otherwise the first
    cut past the soft limit (for example one oversized step). Notes immediately
    before a step belong to that step, so the input a response reacted to stays
    with the response. Retained steps are never edited. Live request slices
    include replayed reasoning and request-only Tool media; the selected Adapter
    counts the representation it will actually serialize.
    """
    if not messages:
        raise CompactionError("Cannot find tail boundary for an empty message list")
    if tail_tokens <= 0:
        raise CompactionError("tail_tokens must be positive")
    safe_boundaries = _safe_tail_boundary_indices(messages)
    if not safe_boundaries:
        raise CompactionError("Cannot find a provider-safe tail boundary")

    request_indices = (
        {message.get("id"): index for index, message in enumerate(request_messages)}
        if request_messages is not None
        else {}
    )
    candidates: list[_TailCandidate] = []
    for boundary_index in reversed(safe_boundaries):
        start_index, request_start = _tail_step_start(
            messages, boundary_index, request_messages, request_indices
        )
        if request_messages is None or request_start is None:
            candidate = [message.to_dict() for message in messages[start_index:]]
        else:
            candidate = list(request_messages[request_start:])
        tokens = (
            estimate_tail_tokens(candidate)
            if estimate_tail_tokens is not None
            else estimate_request_input_tokens(candidate)[0]
        )
        candidates.append(
            _TailCandidate(start_index=start_index, request_start=request_start, tokens=tokens)
        )
        # Older starts only grow the Tail: the first one reaching the target
        # is either the choice or, past the soft limit, the upward fallback.
        if tokens >= tail_tokens:
            break

    selected = _select_tail_candidate(candidates, tail_tokens)
    return _TailPlan(
        boundary_id=messages[selected.start_index].id,
        boundary_index=selected.start_index,
        request_start=selected.request_start,
        projected_suffix=tuple(messages[selected.start_index :]),
    )


def _select_tail_candidate(candidates: list[_TailCandidate], tail_tokens: int) -> _TailCandidate:
    """Pick from newest-first candidates ending at the first one reaching the target.

    That last candidate is also the largest. It wins within the soft limit (or
    when the whole history stays below the target); past the soft limit, the
    largest earlier candidate down to the floor; without one, the last after all.
    """

    last = candidates[-1]
    if last.tokens <= _tail_soft_limit(tail_tokens):
        return last
    floor = tail_tokens * TAIL_FLOOR_PERCENT // 100
    below = [candidate for candidate in candidates[:-1] if candidate.tokens >= floor]
    return max(below, key=lambda candidate: candidate.tokens) if below else last


def _tail_soft_limit(tail_tokens: int) -> int:
    return (tail_tokens * TAIL_SOFT_LIMIT_PERCENT + 99) // 100


def _tail_step_start(
    messages: list[ChatMessage],
    boundary_index: int,
    request_messages: tuple[JsonObject, ...] | None,
    request_indices: Mapping[Any, int],
) -> tuple[int, int | None]:
    """Return the canonical and request start of the step at ``boundary_index``.

    The step includes the notes directly before its User or Assistant message.
    Against a live request that holds only when those notes render as exactly
    the request messages before the boundary; otherwise (for example notes
    deferred past a Tool batch and merged with earlier ones) the step starts at
    the boundary message so the request slice and the canonical suffix agree.
    """

    lead_in_start = boundary_index
    while lead_in_start > 0 and _is_tail_lead_in_note(messages[lead_in_start - 1]):
        lead_in_start -= 1
    if request_messages is None:
        return lead_in_start, None
    request_index = request_indices.get(messages[boundary_index].id)
    if request_index is None:
        raise CompactionError("Tail boundary was not found in the active request Context")
    if lead_in_start == boundary_index:
        return boundary_index, request_index
    rendered = note_request_messages(list(messages[lead_in_start:boundary_index]))
    lead_in_request_start = request_index - len(rendered)
    if lead_in_request_start >= 0 and (
        list(request_messages[lead_in_request_start:request_index]) == rendered
    ):
        return lead_in_start, lead_in_request_start
    return boundary_index, request_index


def _is_tail_lead_in_note(message: ChatMessage) -> bool:
    return message.role == "note" and not _is_compaction_checkpoint_note(message)


def _safe_tail_boundary_indices(messages: list[ChatMessage]) -> list[int]:
    """Find every valid suffix in one reverse pass through messages and Tool Calls.

    This is ``_validate_projection`` read backwards: Results accumulate until
    their immediately preceding Assistant declares exactly those Call ids.
    An invalid completed block cannot be repaired by extending the suffix to
    the left, but boundaries already found to its right remain valid.
    """
    boundaries: list[int] = []
    cycles = ToolCycleSuffix()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role in {"note", "run_summary", "agent_takeover", "error"}:
            continue
        if not cycles.accepts(
            message.role,
            call_ids=(call.id for call in message.tool_calls or ()),
            result_id=message.tool_call_id,
        ):
            break
        if _can_start_tail(message):
            boundaries.append(index)
    boundaries.reverse()
    return boundaries


def _is_compaction_user_quote(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(COMPACTION_USER_QUOTE_PREFIX)
    )


def _is_compaction_summary_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(COMPACTION_SUMMARY_NOTE_PREFIX)
    )


def _is_compaction_checkpoint_note(message: ChatMessage) -> bool:
    return (
        _is_compaction_summary_note(message)
        or _is_compaction_user_quote(message)
        or (
            message.role == "note"
            and isinstance(message.content, str)
            and message.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
        )
    )


def _can_start_tail(message: ChatMessage) -> bool:
    """Return whether a canonical message has a stable provider-visible boundary."""

    if message.role == "user":
        return True
    return message.role == "assistant" and (message.content is not None or bool(message.tool_calls))
