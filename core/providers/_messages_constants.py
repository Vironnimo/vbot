"""Messages wire, Reasoning and prompt-cache constants."""

from __future__ import annotations

ANTHROPIC_OVERLOADED_STATUS = 529

# Anthropic's monthly spend cap: HTTP 429 typed ``rate_limit_error`` without
# ``retry-after``, told apart from a rate limit only by this
# ``error.details.error_code``. Access resumes at 00:00 UTC on the first day of
# the next month, which the message states
# (https://platform.claude.com/docs/en/api/rate-limits, read 2026-10-19).
ANTHROPIC_SPEND_CAP_ERROR_CODE = "enforced_spend_limit_reached"

MESSAGES_ENDPOINT = "/messages"

ANTHROPIC_VERSION = "2023-06-01"

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

CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}

CACHE_BREAKPOINT_LIMIT = 4

MAX_HISTORY_CACHE_BREAKPOINTS = 3

CACHE_UNMARKABLE_BLOCK_TYPES = frozenset({THINKING_BLOCK_TYPE, REDACTED_THINKING_BLOCK_TYPE})

ANTHROPIC_TOOL_STOP_REASONS = {"tool_use"}

ANTHROPIC_STOP_REASONS = {"end_turn", "stop_sequence"}

# ``model_context_window_exceeded`` ends a turn that filled the Context window
# before ``max_tokens``; Anthropic documents it as a valid, truncated response.
ANTHROPIC_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})

ANTHROPIC_ERROR_STOP_REASONS = {"error", "pause_turn"}
