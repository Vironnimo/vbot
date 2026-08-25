"""Compaction domain public API."""

from core.compaction.compaction import (
    COMPACTION_POLICY_META_KEY,
    COMPACTION_TRIGGER_MANUAL,
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    TOOL_RESULT_COMPACTED_FIELD,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionPlan,
    CompactionService,
    CompactionSettings,
    CompactionStrategy,
    ContextRatioTrigger,
    ContinuationStrategy,
    InputTokensTrigger,
    SummarizationStrategy,
    find_tail_boundary,
    is_compacted_tool_result_content,
)

__all__ = [
    "CompactionError",
    "CompactionInsufficientReclaimError",
    "COMPACTION_POLICY_META_KEY",
    "COMPACTION_TRIGGER_MANUAL",
    "CompactionPlan",
    "CompactionService",
    "CompactionSettings",
    "CompactionStrategy",
    "ContinuationStrategy",
    "ContextRatioTrigger",
    "InputTokensTrigger",
    "MIN_AUTO_COMPACTION_RECLAIM_TOKENS",
    "SummarizationStrategy",
    "TOOL_RESULT_COMPACTED_FIELD",
    "find_tail_boundary",
    "is_compacted_tool_result_content",
]
