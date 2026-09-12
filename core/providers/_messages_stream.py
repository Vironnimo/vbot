"""Stateful Messages stream decoding into canonical Provider deltas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.providers._messages_constants import (
    ANTHROPIC_ERROR_STOP_REASONS,
    ANTHROPIC_STOP_REASONS,
    ANTHROPIC_TOOL_STOP_REASONS,
    REASONING_META_CONTENT_BLOCKS,
    THINKING_BLOCK_TYPE,
    TOOL_USE_BLOCK_TYPE,
)
from core.providers._messages_wire import (
    _extract_anthropic_stream_input_usage,
    _is_supported_reasoning_block,
    apply_anthropic_reasoning_usage,
)
from core.providers.adapter import (
    TERMINAL_OUTCOME_CONTENT_FILTERED,
    TERMINAL_OUTCOME_ERROR,
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TerminalOutcome,
)
from core.providers.errors import ProviderError


class AnthropicMessagesStreamDecoder:
    """Normalize one Anthropic Messages SSE stream into vBot deltas.

    Anthropic owns the shared Messages wire protocol. Providers exposing a
    compatible endpoint can configure the few places where their contract
    differs without maintaining a second event state machine.
    """

    def __init__(
        self,
        *,
        error_detail: Callable[[dict[str, Any]], str] | None = None,
        reasoning_block_normalizer: Callable[[Any], dict[str, Any]] | None = None,
        text_delta_in_thinking: bool = False,
        emit_usage_without_start: bool = True,
        drop_stopped_reasoning_block: bool = False,
    ) -> None:
        self.content_blocks_by_index: dict[int, dict[str, Any]] = {}
        self.reasoning_meta_blocks: list[dict[str, Any]] = []
        self.usage_from_start: dict[str, Any] | None = None
        self._error_detail = error_detail or self._anthropic_error_detail
        self._reasoning_block_normalizer = reasoning_block_normalizer or self._copy_reasoning_block
        self._text_delta_in_thinking = text_delta_in_thinking
        self._emit_usage_without_start = emit_usage_without_start
        self._drop_stopped_reasoning_block = drop_stopped_reasoning_block

    def normalize(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize one parsed Messages event while retaining stream state."""

        event_type = event.get("type")
        if event_type == "error":
            raise ProviderError(self._error_detail(event), retryable=False)
        if event_type == "message_start":
            self._capture_message_start_usage(event)
            return []
        if event_type == "content_block_start":
            return self._normalize_content_block_start(event)
        if event_type == "content_block_delta":
            return self._normalize_content_block_delta(event)
        if event_type == "content_block_stop":
            return self._normalize_content_block_stop(event)
        if event_type == "message_delta":
            return self._normalize_message_delta(event)
        return []

    @staticmethod
    def _anthropic_error_detail(event: dict[str, Any]) -> str:
        error_info = event.get("error", {})
        message = (error_info.get("message") if isinstance(error_info, dict) else None) or str(
            event
        )
        return f"Provider stream error: {message}"

    @staticmethod
    def _copy_reasoning_block(block: Any) -> dict[str, Any]:
        return dict(block) if _is_supported_reasoning_block(block) else {}

    def _capture_message_start_usage(self, event: dict[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        usage_from_start = _extract_anthropic_stream_input_usage(usage)
        if usage_from_start is not None:
            self.usage_from_start = usage_from_start

    def _normalize_content_block_start(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        index = self._stream_index(event)
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
            self.content_blocks_by_index[index] = block_state
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

        reasoning_block = self._reasoning_block_normalizer(content_block)
        if reasoning_block:
            block_state["block"] = reasoning_block
        self.content_blocks_by_index[index] = block_state
        return []

    def _normalize_content_block_delta(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        index = self._stream_index(event)
        delta = event.get("delta")
        if index is None or not isinstance(delta, dict):
            return []

        block_state = self.content_blocks_by_index.get(index, {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return self._normalize_text_delta(delta, block_state)
        if delta_type == "thinking_delta":
            return self._normalize_thinking_delta(delta, block_state)
        if delta_type == "signature_delta":
            self._apply_signature_delta(delta, block_state)
            return []
        if delta_type == "input_json_delta":
            return self._normalize_tool_input_delta(delta, block_state)
        return []

    def _normalize_content_block_stop(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        index = self._stream_index(event)
        if index is None:
            return []
        block_state = self.content_blocks_by_index.get(index, {})
        reasoning_block = self._reasoning_block_normalizer(block_state.get("block"))
        if not reasoning_block:
            return []

        self.reasoning_meta_blocks.append(reasoning_block)
        if self._drop_stopped_reasoning_block:
            self.content_blocks_by_index.pop(index, None)
        return [
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    REASONING_META_CONTENT_BLOCKS: [
                        dict(meta_block) for meta_block in self.reasoning_meta_blocks
                    ]
                },
            }
        ]

    def _normalize_message_delta(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        normalized_deltas: list[dict[str, Any]] = []
        delta = event.get("delta")
        if isinstance(delta, dict):
            stop_reason = delta.get("stop_reason")
            if stop_reason is not None:
                normalized_deltas.append(
                    {
                        "type": "finish",
                        "reason": self._normalize_stop_reason(
                            stop_reason,
                            has_tool_calls=self._has_stream_tool_calls(),
                        ),
                    }
                )

        usage = event.get("usage")
        if isinstance(usage, dict):
            output_tokens = usage.get("output_tokens")
            terminal_input_usage = _extract_anthropic_stream_input_usage(usage)
            input_usage = terminal_input_usage or self.usage_from_start
            if (
                isinstance(output_tokens, int)
                and not isinstance(output_tokens, bool)
                and output_tokens >= 0
                and (input_usage is not None or self._emit_usage_without_start)
            ):
                normalized_usage = {"type": "usage", "output_tokens": output_tokens}
                if input_usage is not None:
                    normalized_usage.update(input_usage)
                apply_anthropic_reasoning_usage(normalized_usage, usage)
                normalized_deltas.append(normalized_usage)
        return normalized_deltas

    def _normalize_text_delta(
        self,
        delta: dict[str, Any],
        block_state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        if self._text_delta_in_thinking and block_state.get("type") == THINKING_BLOCK_TYPE:
            block = block_state.get("block")
            if isinstance(block, dict):
                block["text"] = f"{block.get('text', '')}{text}"
            return [{"type": "reasoning_delta", "text": text}]
        return [{"type": "content_delta", "text": text}]

    @staticmethod
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

    @staticmethod
    def _apply_signature_delta(
        delta: dict[str, Any],
        block_state: dict[str, Any],
    ) -> None:
        signature = delta.get("signature")
        block = block_state.get("block")
        if isinstance(signature, str) and signature and isinstance(block, dict):
            block["signature"] = signature

    @staticmethod
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

    @staticmethod
    def _normalize_stop_reason(
        stop_reason: Any,
        *,
        has_tool_calls: bool,
    ) -> TerminalOutcome:
        if stop_reason in ANTHROPIC_TOOL_STOP_REASONS:
            return TERMINAL_OUTCOME_TOOL_CALLS
        if stop_reason in ANTHROPIC_STOP_REASONS:
            return TERMINAL_OUTCOME_TOOL_CALLS if has_tool_calls else TERMINAL_OUTCOME_STOP
        if stop_reason == "max_tokens":
            return TERMINAL_OUTCOME_OUTPUT_TRUNCATED
        if stop_reason == "refusal":
            return TERMINAL_OUTCOME_CONTENT_FILTERED
        if stop_reason in ANTHROPIC_ERROR_STOP_REASONS:
            return TERMINAL_OUTCOME_ERROR
        return TERMINAL_OUTCOME_UNKNOWN

    @staticmethod
    def _stream_index(event: dict[str, Any]) -> int | None:
        index = event.get("index")
        return index if isinstance(index, int) else None

    def _has_stream_tool_calls(self) -> bool:
        return any(
            block.get("type") == TOOL_USE_BLOCK_TYPE
            for block in self.content_blocks_by_index.values()
        )
