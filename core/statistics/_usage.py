"""Model usage: call tokens by Model, Provider and day, plus prompt-cache accounting.

A call's token counts split by measurement: an estimated count never mixes
into a measured total, Reasoning counts only for a measured output with a
valid reported breakdown, and cache fields count only for a measured prompt
that reported them.
"""

from __future__ import annotations

from collections import Counter

from core.statistics._accumulators import ReportLedger, _ModelAcc, _ProviderAcc
from core.statistics._cache import CacheFacts, load_cache_facts
from core.statistics._projection import CALL_KIND_CHAT, CALL_KIND_COMPACTION
from core.statistics._units import UnitScan
from core.statistics.report import (
    CacheSection,
    ModelUsage,
    ProviderUsage,
    SuspectedCacheBreaks,
    UsageDailyPoint,
    UsageKind,
    UsageSection,
    UsageTotals,
)

TOP_CACHE_SESSIONS = 20


TOP_CACHE_BREAK_INCIDENTS = 20


# Token sums over ``stat_calls`` rows aliased ``c``, shared by every section
# that reports call tokens.
MEASURED_INPUT_SQL = (
    "SUM(CASE WHEN c.input_estimated = 1 THEN 0 ELSE COALESCE(c.input_tokens, 0) END)"
)
ESTIMATED_INPUT_SQL = (
    "SUM(CASE WHEN c.input_estimated = 1 THEN COALESCE(c.input_tokens, 0) ELSE 0 END)"
)
MEASURED_OUTPUT_SQL = (
    "SUM(CASE WHEN c.output_estimated = 1 THEN 0 ELSE COALESCE(c.output_tokens, 0) END)"
)
ESTIMATED_OUTPUT_SQL = (
    "SUM(CASE WHEN c.output_estimated = 1 THEN COALESCE(c.output_tokens, 0) ELSE 0 END)"
)
# Reasoning counts only for a measured output with a valid reported breakdown.
_REASONING = (
    "(c.output_estimated = 0 AND c.output_tokens IS NOT NULL AND c.reasoning_tokens IS NOT NULL)"
)
# Cache fields count only for a measured prompt that reported them.
_CACHE = "(c.input_estimated = 0 AND c.input_tokens IS NOT NULL AND c.has_cache = 1)"


class UsageAccumulator:
    """Report-wide call totals; per-Model, Provider and day tallies live on the ledger."""

    def __init__(self) -> None:
        self.assistant_messages = 0
        self.chat_calls = 0
        self.auxiliary_calls = 0
        self.compaction_calls = 0
        self.unreported_calls = 0
        self.measured_turns = 0
        self.estimated_turns = 0
        self.reasoning_tokens = 0
        self.reasoning_turns = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.cache_turns = 0
        self.cache_input_tokens = 0
        self.cache = CacheFacts()
        self.kinds: dict[str, Counter[str]] = {}

    def load(
        self,
        scan: UnitScan,
        ledger: ReportLedger,
        *,
        include_cache: bool = True,
        count_assistant: bool = True,
    ) -> None:
        """Aggregate every in-window call, then walk turns for the cache heuristics."""
        for (
            kind,
            purpose,
            model_key,
            day,
            calls,
            estimated_turns,
            measured_turns,
            measured_input,
            estimated_input,
            measured_output,
            estimated_output,
            reasoning_tokens,
            reasoning_turns,
            cache_turns,
            cache_input,
            cache_read,
            cache_write,
        ) in scan.execute(
            f"""
            SELECT c.kind, c.purpose, c.model_key, c.day, COUNT(*),
                SUM((c.input_estimated = 1 OR c.output_estimated = 1)
                    AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL),
                SUM(c.input_estimated = 0 AND c.output_estimated = 0
                    AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL),
                {MEASURED_INPUT_SQL}, {ESTIMATED_INPUT_SQL},
                {MEASURED_OUTPUT_SQL}, {ESTIMATED_OUTPUT_SQL},
                SUM(CASE WHEN {_REASONING} THEN c.reasoning_tokens ELSE 0 END),
                SUM({_REASONING}),
                SUM({_CACHE}),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.input_tokens, 0) ELSE 0 END),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.cache_read_tokens, 0) ELSE 0 END),
                SUM(CASE WHEN {_CACHE} THEN COALESCE(c.cache_write_tokens, 0) ELSE 0 END)
            FROM {scan.source("stat_calls", "c")}
            WHERE {scan.where("c")}
            GROUP BY c.kind, c.purpose, c.model_key, c.day
            """
        ):
            if kind == CALL_KIND_COMPACTION:
                self.compaction_calls += calls
            elif kind == CALL_KIND_CHAT:
                self.chat_calls += calls
                if count_assistant:
                    self.assistant_messages += calls
            else:
                self.auxiliary_calls += calls
            counts = self.kinds.setdefault(purpose, Counter())
            counts["calls"] += calls
            counts["measured"] += measured_turns
            counts["estimated"] += estimated_turns
            counts["unreported"] += calls - estimated_turns - measured_turns
            model = ledger.model(model_key)
            provider = ledger.provider(model.provider)
            self.estimated_turns += estimated_turns
            self.measured_turns += measured_turns
            self.unreported_calls += calls - estimated_turns - measured_turns
            self.reasoning_tokens += reasoning_tokens
            self.reasoning_turns += reasoning_turns
            self.cache_read_tokens += cache_read
            self.cache_write_tokens += cache_write
            self.cache_turns += cache_turns
            self.cache_input_tokens += cache_input
            for accumulator in (model, provider):
                accumulator.assistant_messages += calls
                accumulator.estimated_turns += estimated_turns
                accumulator.measured_input_tokens += measured_input
                accumulator.estimated_input_tokens += estimated_input
                accumulator.measured_output_tokens += measured_output
                accumulator.estimated_output_tokens += estimated_output
                accumulator.reasoning_tokens += reasoning_tokens
                accumulator.reasoning_turns += reasoning_turns
                accumulator.cache_turns += cache_turns
                accumulator.cache_input_tokens += cache_input
                accumulator.cache_read_tokens += cache_read
                accumulator.cache_write_tokens += cache_write
            daily = ledger.day(day)
            daily.measured_input_tokens += measured_input
            daily.estimated_input_tokens += estimated_input
            daily.measured_output_tokens += measured_output
            daily.estimated_output_tokens += estimated_output
            daily.reasoning_tokens += reasoning_tokens
            daily.reasoning_turns += reasoning_turns
            daily.cache_input_tokens += cache_input
            daily.cache_read_tokens += cache_read
            daily.cache_write_tokens += cache_write
        if include_cache:
            self.cache = load_cache_facts(scan, top_incidents=TOP_CACHE_BREAK_INCIDENTS)

    def build(self, ledger: ReportLedger) -> UsageSection:
        models = ledger.models.values()
        totals = UsageTotals(
            assistant_messages=self.assistant_messages,
            model_calls=self.chat_calls + self.compaction_calls + self.auxiliary_calls,
            chat_calls=self.chat_calls,
            auxiliary_calls=self.auxiliary_calls,
            compaction_calls=self.compaction_calls,
            unreported_calls=self.unreported_calls,
            measured_turns=self.measured_turns,
            estimated_turns=self.estimated_turns,
            measured_input_tokens=sum(model.measured_input_tokens for model in models),
            measured_output_tokens=sum(model.measured_output_tokens for model in models),
            reasoning_tokens=self.reasoning_tokens,
            reasoning_turns=self.reasoning_turns,
            estimated_input_tokens=sum(model.estimated_input_tokens for model in models),
            estimated_output_tokens=sum(model.estimated_output_tokens for model in models),
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_turns=self.cache_turns,
            cache_input_tokens=self.cache_input_tokens,
        )
        return UsageSection(
            totals=totals,
            providers=sorted(
                (_provider_usage(accumulator) for accumulator in ledger.providers.values()),
                key=lambda usage: (-usage.total_tokens, usage.provider),
            ),
            models=sorted(
                (_model_usage(accumulator) for accumulator in models),
                key=lambda usage: (-usage.total_tokens, usage.model),
            ),
            daily=[
                UsageDailyPoint(
                    date=date,
                    runs=bucket.runs,
                    errors=bucket.errors,
                    measured_input_tokens=bucket.measured_input_tokens,
                    measured_output_tokens=bucket.measured_output_tokens,
                    reasoning_tokens=bucket.reasoning_tokens,
                    reasoning_turns=bucket.reasoning_turns,
                    estimated_input_tokens=bucket.estimated_input_tokens,
                    estimated_output_tokens=bucket.estimated_output_tokens,
                    cache_read_tokens=bucket.cache_read_tokens,
                    cache_write_tokens=bucket.cache_write_tokens,
                    cache_input_tokens=bucket.cache_input_tokens,
                )
                for date, bucket in ledger.sorted_daily()
            ],
            cache=self._build_cache(),
            kinds=[
                UsageKind(
                    kind,
                    counts["calls"],
                    counts["measured"],
                    counts["estimated"],
                    counts["unreported"],
                )
                for kind, counts in sorted(self.kinds.items())
            ],
        )

    def _build_cache(self) -> CacheSection:
        # Worst hit rate first; equal rates surface the bigger session (more
        # tokens paid) before the smaller one.
        sessions = sorted(
            self.cache.sessions,
            key=lambda record: (record.hit_rate, -record.input_tokens, record.session_id),
        )[:TOP_CACHE_SESSIONS]
        return CacheSection(
            lowest_hit_rate_sessions=sessions,
            suspected_breaks=SuspectedCacheBreaks(
                evaluated_turns=self.cache.evaluated_turns,
                suspected_turns=self.cache.suspected_turns,
                incidents=self.cache.incidents,
            ),
        )


def _provider_usage(accumulator: _ProviderAcc) -> ProviderUsage:
    total_tokens = (
        accumulator.measured_input_tokens
        + accumulator.measured_output_tokens
        + accumulator.estimated_input_tokens
        + accumulator.estimated_output_tokens
    )
    return ProviderUsage(
        provider=accumulator.provider,
        runs=accumulator.runs,
        assistant_messages=accumulator.assistant_messages,
        measured_input_tokens=accumulator.measured_input_tokens,
        measured_output_tokens=accumulator.measured_output_tokens,
        reasoning_tokens=accumulator.reasoning_tokens,
        reasoning_turns=accumulator.reasoning_turns,
        estimated_input_tokens=accumulator.estimated_input_tokens,
        estimated_output_tokens=accumulator.estimated_output_tokens,
        estimated_turns=accumulator.estimated_turns,
        errors=accumulator.errors,
        cache_read_tokens=accumulator.cache_read_tokens,
        cache_write_tokens=accumulator.cache_write_tokens,
        cache_turns=accumulator.cache_turns,
        cache_input_tokens=accumulator.cache_input_tokens,
        total_tokens=total_tokens,
    )


def _model_usage(accumulator: _ModelAcc) -> ModelUsage:
    total_tokens = (
        accumulator.measured_input_tokens
        + accumulator.measured_output_tokens
        + accumulator.estimated_input_tokens
        + accumulator.estimated_output_tokens
    )
    average = (
        accumulator.run_duration_total_ms / accumulator.run_duration_count
        if accumulator.run_duration_count
        else None
    )
    return ModelUsage(
        provider=accumulator.provider,
        model=accumulator.model,
        runs=accumulator.runs,
        assistant_messages=accumulator.assistant_messages,
        measured_input_tokens=accumulator.measured_input_tokens,
        measured_output_tokens=accumulator.measured_output_tokens,
        reasoning_tokens=accumulator.reasoning_tokens,
        reasoning_turns=accumulator.reasoning_turns,
        estimated_input_tokens=accumulator.estimated_input_tokens,
        estimated_output_tokens=accumulator.estimated_output_tokens,
        estimated_turns=accumulator.estimated_turns,
        errors=accumulator.errors,
        cache_read_tokens=accumulator.cache_read_tokens,
        cache_write_tokens=accumulator.cache_write_tokens,
        cache_turns=accumulator.cache_turns,
        cache_input_tokens=accumulator.cache_input_tokens,
        total_tokens=total_tokens,
        average_run_duration_ms=average,
    )
