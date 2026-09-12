"""Messages wire, Reasoning and prompt-cache constants."""

from __future__ import annotations

ANTHROPIC_OVERLOADED_STATUS = 529

MESSAGES_ENDPOINT = "/messages"

ANTHROPIC_VERSION = "2023-06-01"

ANTHROPIC_EFFORT_FLOOR = ("minimal", "low", "medium", "high", "xhigh", "max")

ANTHROPIC_MINIMAL_EFFORT = "minimal"

ANTHROPIC_REASONING_PARAMETER_NAMES = {
    "thinking",
    "thinking_budget",
    "output_config",
    "reasoning_effort",
    "reasoning",
    "include_reasoning",
}

TEXT_BLOCK_TYPE = "text"

TOOL_USE_BLOCK_TYPE = "tool_use"

THINKING_BLOCK_TYPE = "thinking"

REDACTED_THINKING_BLOCK_TYPE = "redacted_thinking"

REASONING_META_CONTENT_BLOCKS = "content_blocks"

ANTHROPIC_METADATA_KEY = "anthropic"

REQUIRES_ADAPTIVE_THINKING_METADATA_KEY = "requires_adaptive_thinking"

CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}

CACHE_BREAKPOINT_LIMIT = 4

MAX_HISTORY_CACHE_BREAKPOINTS = 3

CACHE_UNMARKABLE_BLOCK_TYPES = frozenset({THINKING_BLOCK_TYPE, REDACTED_THINKING_BLOCK_TYPE})

ANTHROPIC_TOOL_STOP_REASONS = {"tool_use"}

ANTHROPIC_STOP_REASONS = {"end_turn", "stop_sequence"}

ANTHROPIC_ERROR_STOP_REASONS = {"error", "pause_turn"}

ANTHROPIC_SAMPLING_PARAMETER_NAMES = ("temperature", "top_p", "top_k")
