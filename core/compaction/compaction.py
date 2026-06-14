"""Compaction service and summarization strategy for chat history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from core.chat.chat import ChatMessage
from core.chat.content_blocks import content_block_to_dict
from core.utils.errors import VBotError
from core.utils.tokens import estimate_message_tokens

TOOL_RESULT_CONTENT_PLACEHOLDER = "[tool result content omitted during compaction]"


@dataclass(frozen=True)
class CompactionSettings:
    """Runtime settings that control automatic and manual compaction."""

    auto: bool = True
    threshold: float = 0.8
    tail_tokens: int = 15_000
    summary_model: str | None = None


class CompactionStrategy(Protocol):
    """Protocol for compaction implementations."""

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None = None,
    ) -> ChatMessage:
        """Produce and return a compaction checkpoint message."""


class CompactionError(VBotError):
    """Raised when compaction cannot be completed."""


def find_tail_boundary(messages: list[ChatMessage], tail_tokens: int) -> str:
    """Return the user-message id where the preserved tail should start."""
    if not messages:
        raise CompactionError("Cannot find tail boundary for an empty message list")
    if tail_tokens <= 0:
        raise CompactionError("tail_tokens must be positive")

    turn_ranges = _turn_ranges(messages)
    if not turn_ranges:
        raise CompactionError("Cannot compact history without at least one user message")

    boundary_index = turn_ranges[0][0]
    accumulated_tokens = 0
    for start_index, end_index in reversed(turn_ranges):
        accumulated_tokens += _estimate_token_span(messages[start_index:end_index])
        boundary_index = start_index
        if accumulated_tokens >= tail_tokens:
            break
    return messages[boundary_index].id


class SummarizationStrategy:
    """Compaction strategy that summarizes pre-tail history via an adapter call."""

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None = None,
    ) -> ChatMessage:
        """Summarize pre-tail history and return a compaction checkpoint.

        Compaction is incremental: when the history already contains a previous
        compaction checkpoint, only the messages added since that checkpoint's
        preserved tail boundary are summarized, and the previous summary is
        folded back in as seed context. This keeps each run proportional to the
        newly added history instead of re-summarizing the whole session every
        time (which would grow the summary prompt without bound and eventually
        overflow the summary model's own context window).
        """
        del agent

        if not messages:
            raise CompactionError("Cannot compact an empty message list")

        previous_summary, uncompacted_messages, previous_token_count = (
            _split_at_previous_checkpoint(messages)
        )

        boundary_id = find_tail_boundary(uncompacted_messages, settings.tail_tokens)
        boundary_index = _find_boundary_index(uncompacted_messages, boundary_id)
        pre_tail_messages = uncompacted_messages[:boundary_index]

        history_text = _render_history_for_prompt(pre_tail_messages)
        prompt_fragment = storage.read_prompt_fragment("compaction.md")
        prompt_text = _build_compaction_prompt(
            prompt_fragment, history_text, previous_summary, instruction
        )

        compacted_token_count = previous_token_count + _estimate_token_span(pre_tail_messages)
        response = await summary_adapter.send(
            [{"role": "user", "content": prompt_text}],
            model_id=summary_model_id,
            temperature=0.0,
            thinking_effort="",
        )
        normalized_response = _normalize_response(summary_adapter, response)
        summary_text = _extract_summary_text(normalized_response)

        return ChatMessage.compaction_checkpoint(
            summary=summary_text,
            tail_boundary_id=boundary_id,
            compacted_token_count=compacted_token_count,
        )


class CompactionService:
    """Service wrapper around a compaction strategy."""

    def __init__(self, strategy: CompactionStrategy) -> None:
        self._strategy = strategy

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None = None,
    ) -> ChatMessage:
        """Delegate compaction and wrap unexpected strategy failures."""
        try:
            checkpoint = await self._strategy.compact(
                messages,
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=storage,
                settings=settings,
                instruction=instruction,
            )
        except CompactionError:
            raise
        except Exception as exc:
            raise CompactionError(f"Compaction failed: {exc}") from exc

        if checkpoint.role != "compaction_checkpoint":
            raise CompactionError("Compaction strategy must return a compaction_checkpoint message")
        return checkpoint

    def should_auto_compact(
        self,
        input_tokens: int,
        context_window: int,
        threshold: float,
    ) -> bool:
        """Return True when usage ratio exceeds the configured threshold."""
        if context_window <= 0:
            return False
        return (input_tokens / context_window) >= threshold

    def estimate_messages_tokens(self, messages: list[dict]) -> int:
        """Estimate total tokens using provider-relevant message fields."""
        total_tokens = 0
        for message in messages:
            estimated_tokens, _ = estimate_message_tokens(message)
            total_tokens += estimated_tokens
        return total_tokens


def _turn_ranges(messages: list[ChatMessage]) -> list[tuple[int, int]]:
    user_indices = [index for index, message in enumerate(messages) if message.role == "user"]
    ranges: list[tuple[int, int]] = []
    for index, start_index in enumerate(user_indices):
        end_index = user_indices[index + 1] if index + 1 < len(user_indices) else len(messages)
        ranges.append((start_index, end_index))
    return ranges


def _estimate_token_span(messages: list[ChatMessage]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_message_tokens(message: ChatMessage) -> int:
    estimated_tokens, _ = estimate_message_tokens(message.to_dict())
    return estimated_tokens


def _find_boundary_index(messages: list[ChatMessage], boundary_id: str) -> int:
    for index, message in enumerate(messages):
        if message.id == boundary_id:
            return index
    raise CompactionError(f"Tail boundary id was not found in messages: {boundary_id}")


def _split_at_previous_checkpoint(
    messages: list[ChatMessage],
) -> tuple[str | None, list[ChatMessage], int]:
    """Split history at the most recent compaction checkpoint.

    Returns ``(previous_summary, uncompacted_messages, previous_token_count)``.
    Everything up to the previous checkpoint's preserved tail boundary is already
    represented by that checkpoint's summary, so only the messages from the
    boundary onward (with checkpoint markers removed) are candidates for a new
    summary. Without a previous checkpoint the entire history is uncompacted.
    """
    checkpoint = _latest_checkpoint(messages)
    if checkpoint is None or checkpoint.tail_boundary_id is None:
        return None, messages, 0

    boundary_index = _find_boundary_index(messages, checkpoint.tail_boundary_id)
    uncompacted_messages = [
        message for message in messages[boundary_index:] if message.role != "compaction_checkpoint"
    ]
    previous_summary = checkpoint.content if isinstance(checkpoint.content, str) else None
    return previous_summary, uncompacted_messages, _previous_compacted_token_count(checkpoint)


def _latest_checkpoint(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def _previous_compacted_token_count(checkpoint: ChatMessage) -> int:
    usage = checkpoint.usage
    if not isinstance(usage, dict):
        return 0
    count = usage.get("compacted_token_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _build_compaction_prompt(
    prompt_fragment: str,
    history_text: str,
    previous_summary: str | None = None,
    instruction: str | None = None,
) -> str:
    sections = [prompt_fragment.strip()]
    if instruction and instruction.strip():
        sections.append(f"<user_instruction>\n{instruction.strip()}\n</user_instruction>")
    if previous_summary and previous_summary.strip():
        sections.append(f"<previous_summary>\n{previous_summary.strip()}\n</previous_summary>")
    sections.append(f"<history>\n{history_text}\n</history>")
    return "\n\n".join(sections)


def _render_history_for_prompt(messages: list[ChatMessage]) -> str:
    if not messages:
        return "(no history before boundary)"
    return "\n\n".join(_render_message_entry(message) for message in messages)


def _render_message_entry(message: ChatMessage) -> str:
    lines = [f"role={message.role} id={message.id}"]

    content = _render_message_content(message)
    if content is not None:
        lines.append(f"content={content}")

    if message.tool_calls is not None:
        tool_calls = [tool_call.to_dict() for tool_call in message.tool_calls]
        lines.append(f"tool_calls={json.dumps(tool_calls, ensure_ascii=False)}")
    if message.tool_call_id is not None:
        lines.append(f"tool_call_id={message.tool_call_id}")
    if message.name is not None:
        lines.append(f"name={message.name}")
    if message.error_kind is not None:
        lines.append(f"error_kind={message.error_kind}")

    return "\n".join(lines)


def _render_message_content(message: ChatMessage) -> str | None:
    if message.role == "tool":
        return TOOL_RESULT_CONTENT_PLACEHOLDER

    content = message.content
    if content is None:
        return None
    if isinstance(content, str):
        return content
    return json.dumps([content_block_to_dict(block) for block in content], ensure_ascii=False)


def _normalize_response(summary_adapter: Any, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise CompactionError("Summary adapter returned a non-object response")

    normalize_response = getattr(summary_adapter, "normalize_response", None)
    if callable(normalize_response):
        normalized = normalize_response(response)
        if not isinstance(normalized, dict):
            raise CompactionError("Summary adapter normalize_response() must return an object")
        return cast("dict[str, Any]", normalized)

    return cast("dict[str, Any]", response)


def _extract_summary_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        summary = content.strip()
        if summary:
            return summary

    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_chunks.append(text)

        summary = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        if summary:
            return summary

    raise CompactionError("Summary response did not include text content")
