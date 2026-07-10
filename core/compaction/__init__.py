"""Compaction domain public API."""

from core.compaction.compaction import (
    COMPACTION_POLICY_META_KEY,
    TOOL_RESULT_CONTENT_PLACEHOLDER,
    CompactionError,
    CompactionPlan,
    CompactionService,
    CompactionSettings,
    CompactionStrategy,
    ContextRatioTrigger,
    ContinuationStrategy,
    InputTokensTrigger,
    SummarizationStrategy,
    find_tail_boundary,
)

__all__ = [
    "CompactionError",
    "COMPACTION_POLICY_META_KEY",
    "CompactionPlan",
    "CompactionService",
    "CompactionSettings",
    "CompactionStrategy",
    "ContinuationStrategy",
    "ContextRatioTrigger",
    "InputTokensTrigger",
    "SummarizationStrategy",
    "TOOL_RESULT_CONTENT_PLACEHOLDER",
    "find_tail_boundary",
]
