"""Compaction service and summarization strategy for chat history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from core.chat.chat import ChatMessage
from core.chat.content_blocks import content_block_to_dict
from core.utils.errors import VBotError
from core.utils.tokens import estimate_tokens

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
    ) -> ChatMessage:
        """Summarize pre-tail history and return a compaction checkpoint."""
        del agent  # Agent-specific behavior is deferred to later phases.

        if not messages:
            raise CompactionError("Cannot compact an empty message list")

        boundary_id = find_tail_boundary(messages, settings.tail_tokens)
        boundary_index = _find_boundary_index(messages, boundary_id)
        pre_tail_messages = messages[:boundary_index]

        history_text = _render_history_for_prompt(pre_tail_messages)
        prompt_fragment = storage.read_prompt_fragment("compaction.md")
        prompt_text = _build_compaction_prompt(prompt_fragment, history_text)

        compacted_token_count = _estimate_token_span(pre_tail_messages)
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
        """Estimate total tokens using message content text representations."""
        total_tokens = 0
        for message in messages:
            estimated_tokens, _ = estimate_tokens(str(message.get("content")))
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
    estimated_tokens, _ = estimate_tokens(str(message.content))
    return estimated_tokens


def _find_boundary_index(messages: list[ChatMessage], boundary_id: str) -> int:
    for index, message in enumerate(messages):
        if message.id == boundary_id:
            return index
    raise CompactionError(f"Tail boundary id was not found in messages: {boundary_id}")


def _build_compaction_prompt(prompt_fragment: str, history_text: str) -> str:
    return f"{prompt_fragment.strip()}\n\n<history>\n{history_text}\n</history>"


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
