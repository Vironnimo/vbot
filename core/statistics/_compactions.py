"""Context effectiveness and recurrence of persisted Compaction checkpoints."""

from __future__ import annotations

from collections import Counter

from core.statistics._measurements import _mean, _nearest_rank_percentile
from core.statistics._units import UnitScan
from core.statistics.report import (
    CompactionContextStats,
    CompactionObservation,
    CompactionReclaimStats,
    CompactionSessionStat,
    CompactionsSection,
    CompactionStrategyCount,
)
from core.utils.timestamps import parse_canonical_timestamp


class CompactionAccumulator:
    def __init__(self) -> None:
        self.observations: list[CompactionObservation] = []

    def load(self, scan: UnitScan, titles: list[str | None]) -> None:
        """Observe every in-window checkpoint of the scanned units in processing order.

        ``steps_since_previous`` counts the Assistant Model steps since the unit's
        previous checkpoint, in or out of the window, and stays empty for its
        first. ``next_input_tokens`` is the measured prompt of the very first
        Assistant step after the checkpoint, when that step precedes any later
        checkpoint; only that request is comparable to the fresh context.
        """
        for unit, timestamp, strategy, before, after, duration, steps, next_input in scan.execute(
            f"""
            SELECT o.unit, o.timestamp, o.strategy, o.context_before, o.context_after,
                o.duration_ms,
                CASE WHEN o.previous_checkpoint IS NULL THEN NULL ELSE (
                    SELECT COUNT(*) FROM stat_calls c
                    WHERE c.session_key = o.session_key AND c.kind = 0
                        AND c.seq > o.previous_checkpoint AND c.seq < o.seq
                ) END,
                CASE WHEN o.next_call IS NOT NULL
                    AND (o.next_checkpoint IS NULL OR o.next_call < o.next_checkpoint)
                THEN (
                    SELECT c.input_tokens FROM stat_calls c
                    WHERE c.session_key = o.session_key AND c.seq = o.next_call
                        AND c.input_estimated = 0
                ) END
            FROM (
                SELECT u.unit, x.session_key, x.seq, r.timestamp, x.strategy,
                    x.context_before, x.context_after, x.duration_ms,
                    (SELECT MAX(p.seq) FROM stat_checkpoints p
                        WHERE p.session_key = x.session_key AND p.seq < x.seq
                    ) AS previous_checkpoint,
                    (SELECT MIN(p.seq) FROM stat_checkpoints p
                        WHERE p.session_key = x.session_key AND p.seq > x.seq
                    ) AS next_checkpoint,
                    (SELECT MIN(c.seq) FROM stat_calls c
                        WHERE c.session_key = x.session_key AND c.seq > x.seq AND c.kind = 0
                    ) AS next_call
                FROM {scan.source("stat_checkpoints", "x")}
                JOIN stat_records r ON r.session_key = x.session_key AND r.seq = x.seq
                WHERE {scan.where("x")}
            ) o
            ORDER BY o.unit, o.seq
            """
        ):
            report_unit = scan.units[unit]
            self.observations.append(
                CompactionObservation(
                    report_unit.display_key,
                    report_unit.session_id,
                    titles[unit],
                    timestamp,
                    strategy,
                    before,
                    after,
                    duration,
                    steps,
                    next_input,
                )
            )

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
                key=lambda row: parse_canonical_timestamp(row.timestamp),
                reverse=True,
            )[:50],
        )
