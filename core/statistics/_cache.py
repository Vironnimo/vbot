"""Per-Session measured prompt-cache accounting and cache-break detection."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.statistics._rollups import judged_cache_turns_sql
from core.statistics._units import UnitScan, max_timestamp_sql
from core.statistics.report import (
    CacheBreakIncident,
    CacheSection,
    SessionCacheUsage,
    SuspectedCacheBreaks,
)

MIN_CACHE_SESSION_TURNS = 2


"""A session needs two cache-reporting turns before its hit rate means anything."""


TOP_CACHE_SESSIONS = 20
TOP_CACHE_BREAK_INCIDENTS = 20


@dataclass
class CacheFacts:
    """Cache accounting of one report scan, before top-N selection."""

    sessions: list[SessionCacheUsage] = field(default_factory=list)
    evaluated_turns: int = 0
    suspected_turns: int = 0
    incidents: list[CacheBreakIncident] = field(default_factory=list)


def load_cache_facts(scan: UnitScan, *, top_incidents: int) -> CacheFacts:
    """Judge each unit's in-window turns in order (``judged_cache_turns_sql``)."""
    connection = scan.connection
    connection.execute("DROP TABLE IF EXISTS temp.cache_turns")
    scan.execute(
        "CREATE TEMP TABLE cache_turns AS "
        + judged_cache_turns_sql(scan.source("stat_records", "r"), scan.where("r"), "u.unit")
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
                session_title=report_unit.title,
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
                session_title=report_unit.title,
            )
        )
    return facts


def cache_section(facts: CacheFacts) -> CacheSection:
    """The cache view: the lowest hit rates and the suspected breaks of ``facts``."""
    # Worst hit rate first; equal rates surface the bigger session (more
    # tokens paid) before the smaller one.
    sessions = sorted(
        facts.sessions,
        key=lambda record: (record.hit_rate, -record.input_tokens, record.session_id),
    )[:TOP_CACHE_SESSIONS]
    return CacheSection(
        lowest_hit_rate_sessions=sessions,
        suspected_breaks=SuspectedCacheBreaks(
            evaluated_turns=facts.evaluated_turns,
            suspected_turns=facts.suspected_turns,
            incidents=facts.incidents,
        ),
    )
