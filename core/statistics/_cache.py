"""Per-Session measured prompt-cache accounting and cache-break detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.chat.messages import ChatMessage
from core.statistics._measurements import (
    _max_timestamp,
    _provider_model_key,
    _read_usage,
    _UsageFacts,
)
from core.statistics.report import (
    CacheBreakIncident,
    SessionCacheUsage,
)
from core.statistics.timestamps import parse_timestamp

# Prompt-cache-break heuristic (best-effort, derived — the cache-side sibling of
# ``derived_fallback_runs``). A measured turn is evaluated against its
# predecessor only when no legitimate prefix change explains a cache miss; the
# thresholds below keep false positives low rather than catching every break.
CACHE_BREAK_READ_RATIO = 0.5


"""A cache read below this share of the previous turn's prompt is a suspected break."""


CACHE_BREAK_MAX_GAP_SECONDS = 300


"""Provider prompt caches expire after ~5 idle minutes; longer gaps are expected misses."""


CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS = 2048


"""Below provider minimum cacheable prompt sizes an empty cache read is legitimate."""


MIN_CACHE_SESSION_TURNS = 2


"""A session needs two cache-reporting turns before its hit rate means anything."""


@dataclass
class _PreviousCacheTurn:
    """The break heuristic's expectation baseline: the last measured turn."""

    input_tokens: int
    timestamp: datetime | None
    model_key: str
    has_cache_data: bool


class _SessionCacheTracker:
    """Per-session prompt-cache accumulation and break detection.

    Walks one session's in-window messages in order. Cache totals cover only
    measured turns that reported cache fields; the break heuristic compares
    each such turn's cache read against the previous turn's prompt size and
    skips every turn with a legitimate reason for a miss — first turn,
    compaction checkpoint, agent takeover, model switch, expired-cache idle
    gap, or a previous prompt below provider minimum cacheable sizes.
    """

    def __init__(self, *, agent_id: str, session_id: str) -> None:
        self._agent_id = agent_id
        self._session_id = session_id
        self.cache_turns = 0
        self.input_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.evaluated_turns = 0
        self.incidents: list[CacheBreakIncident] = []
        self._last_activity: str | None = None
        self._previous: _PreviousCacheTurn | None = None

    def observe(self, message: ChatMessage) -> None:
        if message.role in ("compaction_checkpoint", "agent_takeover"):
            # Both legitimately rebuild the prompt prefix — no expectation
            # carries across the boundary.
            self._previous = None
            return
        if message.role != "assistant":
            return
        facts = _read_usage(message.usage)
        if message.usage is None or facts.estimated:
            # Estimated turns give no reliable expectation baseline.
            self._previous = None
            return

        timestamp = parse_timestamp(message.timestamp)
        model_key = _provider_model_key(message.model)
        if facts.has_cache_data:
            self.cache_turns += 1
            self.input_tokens += facts.input_tokens
            self.cache_read_tokens += facts.cache_read
            self.cache_write_tokens += facts.cache_write
            self._last_activity = _max_timestamp(self._last_activity, message.timestamp)
            self._evaluate_break(facts, timestamp, model_key, message.timestamp)
        self._previous = _PreviousCacheTurn(
            input_tokens=facts.input_tokens,
            timestamp=timestamp,
            model_key=model_key,
            has_cache_data=facts.has_cache_data,
        )

    def _evaluate_break(
        self,
        facts: _UsageFacts,
        timestamp: datetime | None,
        model_key: str,
        raw_timestamp: str,
    ) -> None:
        previous = self._previous
        if (
            previous is None
            or not previous.has_cache_data
            or previous.model_key != model_key
            or previous.input_tokens < CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS
            or not _within_cache_gap(previous.timestamp, timestamp)
        ):
            return
        self.evaluated_turns += 1
        if facts.cache_read < previous.input_tokens * CACHE_BREAK_READ_RATIO:
            self.incidents.append(
                CacheBreakIncident(
                    agent_id=self._agent_id,
                    session_id=self._session_id,
                    timestamp=raw_timestamp,
                    model=model_key,
                    previous_input_tokens=previous.input_tokens,
                    cache_read_tokens=facts.cache_read,
                )
            )

    def session_record(self) -> SessionCacheUsage | None:
        if self.cache_turns < MIN_CACHE_SESSION_TURNS or self.input_tokens <= 0:
            return None
        return SessionCacheUsage(
            agent_id=self._agent_id,
            session_id=self._session_id,
            cache_turns=self.cache_turns,
            input_tokens=self.input_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            hit_rate=self.cache_read_tokens / self.input_tokens,
            last_activity=self._last_activity,
        )


def _within_cache_gap(previous: datetime | None, current: datetime | None) -> bool:
    """Whether two turns are close enough for the cache to still be warm."""
    if previous is None or current is None:
        return False
    gap_seconds = (current - previous).total_seconds()
    return 0 <= gap_seconds <= CACHE_BREAK_MAX_GAP_SECONDS
