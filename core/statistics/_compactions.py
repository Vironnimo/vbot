"""Context effectiveness and recurrence of persisted Compaction checkpoints."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from core.chat.messages import ChatMessage, usage_token_is_estimated
from core.statistics._measurements import _mean, _nearest_rank_percentile, _usage_nonnegative_int
from core.statistics.report import (
    CompactionContextStats,
    CompactionObservation,
    CompactionReclaimStats,
    CompactionSessionStat,
    CompactionsSection,
    CompactionStrategyCount,
)
from core.statistics.timestamps import parse_timestamp


class CompactionAccumulator:
    def __init__(self) -> None:
        self.observations: list[CompactionObservation] = []

    def observe_session(
        self,
        agent_id: str,
        session_id: str,
        title: str | None,
        messages: list[ChatMessage],
        in_window: Callable[[str], bool],
    ) -> None:
        steps: int | None = None
        pending: int | None = None
        for message in messages:
            if message.role == "assistant":
                if steps is not None:
                    steps += 1
                if pending is not None:
                    measured = _usage_nonnegative_int(message.usage, "input_tokens")
                    if measured is not None and not usage_token_is_estimated(
                        message.usage or {}, "input_tokens"
                    ):
                        self.observations[pending] = replace(
                            self.observations[pending], next_input_tokens=measured
                        )
                    # Only the very first request is comparable to a fresh checkpoint.
                    pending = None
            elif message.role == "compaction_checkpoint":
                pending = None
                if in_window(message.timestamp):
                    self.observations.append(
                        CompactionObservation(
                            agent_id,
                            session_id,
                            title,
                            message.timestamp,
                            message.compaction_strategy or "unknown",
                            _usage_nonnegative_int(message.usage, "context_tokens_before"),
                            _usage_nonnegative_int(message.usage, "context_tokens_after"),
                            _usage_nonnegative_int(message.usage, "compaction_duration_ms"),
                            steps,
                            None,
                        )
                    )
                    pending = len(self.observations) - 1
                steps = 0

    def build(self) -> CompactionsSection:
        rows = self.observations
        paired = [r for r in rows if r.before_tokens is not None and r.after_tokens is not None]
        before = [r.before_tokens for r in paired if r.before_tokens is not None]
        after = sorted(r.after_tokens for r in paired if r.after_tokens is not None)
        reclaim = sorted(
            max(0, r.before_tokens - r.after_tokens)
            for r in paired
            if r.before_tokens is not None and r.after_tokens is not None
        )
        durations = sorted(r.duration_ms for r in rows if r.duration_ms is not None)
        intervals = [r.steps_since_previous for r in rows if r.steps_since_previous is not None]
        next_input = [r.next_input_tokens for r in rows if r.next_input_tokens is not None]
        groups: dict[tuple[str, str], list[CompactionObservation]] = {}
        for row in rows:
            groups.setdefault((row.agent_id, row.session_id), []).append(row)
        counts = sorted(len(group) for group in groups.values())
        strategies = []
        for strategy, count in Counter(r.strategy for r in rows).most_common():
            samples = [r for r in paired if r.strategy == strategy]
            bs = [r.before_tokens for r in samples if r.before_tokens is not None]
            ats = [r.after_tokens for r in samples if r.after_tokens is not None]
            strategies.append(
                CompactionStrategyCount(
                    strategy,
                    count,
                    _mean(bs),
                    _mean(ats),
                    (sum(bs) - sum(ats)) / sum(bs) if sum(bs) else None,
                )
            )
        return CompactionsSection(
            total_compactions=len(rows),
            sessions_with_compactions=len(groups),
            average_per_compacted_session=_mean(counts),
            p50_per_compacted_session=_nearest_rank_percentile(counts, 50),
            p95_per_compacted_session=_nearest_rank_percentile(counts, 95),
            max_per_session=max(counts, default=0),
            by_strategy=strategies,
            reclaim=CompactionReclaimStats(
                len(reclaim),
                sum(reclaim),
                _mean(reclaim),
                _nearest_rank_percentile(reclaim, 50),
                _nearest_rank_percentile(reclaim, 95),
            ),
            top_sessions=[
                CompactionSessionStat(
                    key[0],
                    key[1],
                    len(group),
                    sum(
                        max(0, r.before_tokens - r.after_tokens)
                        for r in group
                        if r.before_tokens is not None and r.after_tokens is not None
                    ),
                    group[-1].timestamp,
                )
                for key, group in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))[
                    :20
                ]
            ],
            context=CompactionContextStats(
                len(paired),
                _mean(before),
                _mean(after),
                _nearest_rank_percentile(after, 50),
                _nearest_rank_percentile(after, 95),
                (sum(before) - sum(after)) / sum(before) if sum(before) else None,
                sum(
                    r.after_tokens >= r.before_tokens
                    for r in paired
                    if r.after_tokens is not None and r.before_tokens is not None
                ),
                _mean(durations),
                _nearest_rank_percentile(durations, 95),
                len(durations),
                _mean(intervals),
                len(intervals),
                sum(steps <= 2 for steps in intervals),
                _mean(next_input),
                len(next_input),
            ),
            recent=sorted(
                rows,
                key=lambda row: parse_timestamp(row.timestamp) or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )[:50],
        )
