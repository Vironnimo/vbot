"""Per-Session measured prompt-cache accounting and cache-break detection."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.statistics._units import UnitScan, max_timestamp_sql
from core.statistics.report import (
    CacheBreakIncident,
    SessionCacheUsage,
)

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


_MICROSECONDS_PER_SECOND = 1_000_000


@dataclass
class CacheFacts:
    """Cache accounting of one report scan, before top-N selection."""

    sessions: list[SessionCacheUsage] = field(default_factory=list)
    evaluated_turns: int = 0
    suspected_turns: int = 0
    incidents: list[CacheBreakIncident] = field(default_factory=list)


def load_cache_facts(scan: UnitScan, *, top_incidents: int) -> CacheFacts:
    """Walk each unit's in-window turns in order and apply the cache heuristics.

    Only measured Assistant turns set an expectation baseline; a Compaction
    checkpoint, an Agent takeover, a turn without Usage or an estimated turn
    clears it. A cache-reporting turn is evaluated against the immediately
    preceding measured turn when that turn also reported cache fields, used the
    same Model, sent a prompt of at least the minimum cacheable size and ran
    within the cache lifetime; a read below the break ratio of the previous
    prompt is a suspected break.
    """
    connection = scan.connection
    connection.execute("DROP TABLE IF EXISTS temp.cache_turns")
    scan.execute(
        f"""
        CREATE TEMP TABLE cache_turns AS
        WITH stream AS (
            SELECT u.unit, r.seq, r.timestamp, r.instant, c.model_key, c.has_cache,
                COALESCE(c.input_tokens, 0) AS input_tokens,
                COALESCE(c.cache_read_tokens, 0) AS cache_read_tokens,
                COALESCE(c.cache_write_tokens, 0) AS cache_write_tokens,
                (r.role = 'assistant' AND c.has_usage = 1
                    AND c.input_estimated = 0 AND c.output_estimated = 0) AS measured
            FROM {scan.source("stat_records", "r")}
            LEFT JOIN stat_calls c
                ON c.session_key = r.session_key AND c.seq = r.seq AND c.kind = 0
            WHERE {scan.where("r")}
                AND r.role IN ('assistant', 'compaction_checkpoint', 'agent_takeover')
        ),
        turns AS (
            SELECT unit, seq, timestamp, instant, model_key, has_cache, input_tokens,
                cache_read_tokens, cache_write_tokens, measured,
                LAG(measured) OVER turn_order AS previous_measured,
                LAG(has_cache) OVER turn_order AS previous_has_cache,
                LAG(model_key) OVER turn_order AS previous_model_key,
                LAG(input_tokens) OVER turn_order AS previous_input_tokens,
                LAG(instant) OVER turn_order AS previous_instant
            FROM stream
            WINDOW turn_order AS (PARTITION BY unit ORDER BY seq)
        ),
        judged AS (
            SELECT unit, seq, timestamp, instant, model_key, input_tokens,
                cache_read_tokens, cache_write_tokens, previous_input_tokens, COALESCE(
                previous_measured = 1
                AND previous_has_cache = 1
                AND previous_model_key = model_key
                AND previous_input_tokens >= {CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS}
                AND instant IS NOT NULL AND previous_instant IS NOT NULL
                AND instant - previous_instant
                    BETWEEN 0 AND {CACHE_BREAK_MAX_GAP_SECONDS * _MICROSECONDS_PER_SECOND},
                0
            ) AS evaluated
            FROM turns
            WHERE measured = 1 AND has_cache = 1
        )
        SELECT unit, seq, timestamp, instant, model_key, input_tokens, cache_read_tokens,
            cache_write_tokens, previous_input_tokens, evaluated,
            (evaluated = 1
                AND cache_read_tokens < previous_input_tokens * {CACHE_BREAK_READ_RATIO}
            ) AS incident
        FROM judged
        """
    )
    facts = CacheFacts()
    units = scan.units
    last_activity = {
        int(unit): timestamp
        for unit, timestamp in connection.execute(
            f"""
            SELECT unit, timestamp FROM (
                SELECT t.unit, t.timestamp, ROW_NUMBER() OVER (
                    PARTITION BY t.unit ORDER BY {max_timestamp_sql("t")}
                ) AS position
                FROM temp.cache_turns t
            ) WHERE position = 1
            """
        )
    }
    for (
        unit,
        turns,
        input_tokens,
        read_tokens,
        write_tokens,
        evaluated,
        incidents,
    ) in connection.execute(
        """
            SELECT unit, COUNT(*), SUM(input_tokens), SUM(cache_read_tokens),
                SUM(cache_write_tokens), SUM(evaluated), SUM(incident)
            FROM temp.cache_turns
            GROUP BY unit
            ORDER BY unit
            """
    ):
        facts.evaluated_turns += int(evaluated)
        facts.suspected_turns += int(incidents)
        if turns < MIN_CACHE_SESSION_TURNS or input_tokens <= 0:
            continue
        report_unit = units[unit]
        facts.sessions.append(
            SessionCacheUsage(
                agent_id=report_unit.display_key,
                session_id=report_unit.session_id,
                cache_turns=int(turns),
                input_tokens=int(input_tokens),
                cache_read_tokens=int(read_tokens),
                cache_write_tokens=int(write_tokens),
                hit_rate=read_tokens / input_tokens,
                last_activity=last_activity.get(int(unit)),
            )
        )
    # Largest shortfall first, then Session id and timestamp; equal keys keep
    # processing order.
    for unit, timestamp, model_key, previous_input, read_tokens in connection.execute(
        """
        SELECT t.unit, t.timestamp, t.model_key, t.previous_input_tokens, t.cache_read_tokens
        FROM temp.cache_turns t JOIN temp.units u ON u.unit = t.unit
        WHERE t.incident = 1
        ORDER BY t.previous_input_tokens - t.cache_read_tokens DESC, u.session_id,
            t.timestamp, t.unit, t.seq
        LIMIT ?
        """,
        (top_incidents,),
    ):
        report_unit = units[unit]
        facts.incidents.append(
            CacheBreakIncident(
                agent_id=report_unit.display_key,
                session_id=report_unit.session_id,
                timestamp=timestamp,
                model=model_key,
                previous_input_tokens=int(previous_input),
                cache_read_tokens=int(read_tokens),
            )
        )
    return facts
