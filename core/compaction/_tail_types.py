"""Shared types and framing used by Compaction tail selection."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

COMPACTION_USER_QUOTE_PREFIX = (
    "Latest User message in the summarized history "
    "(JSON-quoted; already received, not a new request):\n"
)

TAIL_SOFT_LIMIT_PERCENT = 150

TAIL_FLOOR_PERCENT = 70

RequestTokenEstimator = Callable[[Sequence[Mapping[str, Any]]], int]
