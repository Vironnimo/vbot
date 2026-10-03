"""Diagnostics: Compactions, prompt-cache health, data quality, failed attempts and outliers."""

from __future__ import annotations

from dataclasses import asdict

from core.statistics._compactions import (
    CHECKPOINT_SQL,
    CompactionAccumulator,
    CompactionObservation,
    CompactionsSection,
)
from core.statistics._sections.common import (
    RUN_COST_ORDER,
    JsonObject,
    ReportContext,
    failed_calls_sql,
    percentile,
    usd,
)
from core.statistics._sections.overview import RUNAWAY_ITERATIONS
from core.statistics._sections.window import hour_timestamp, instant_timestamp

TOP_FAILED_HOURS = 10
# A Session needs two cache-reporting turns before its hit rate means anything.
MIN_CACHE_SESSION_TURNS = 2
TOP_CACHE_SESSIONS = 20
TOP_CACHE_BREAK_INCIDENTS = 20
# A Run is a cost outlier at this multiple of the median positive Run cost.
RUNAWAY_COST_FACTOR = 20
MAX_RUNAWAY_RUNS = 50

# Chat messages are the visible conversation: every user record, and Assistant
# steps only when they carry non-blank text. Session records count every
# persisted record by role.
SESSION_RECORD_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
    "run_summary",
    "agent_takeover",
    "history_edit",
)


def build(context: ReportContext) -> JsonObject:
    return {
        "compactions": asdict(_compactions(context)),
        "cache": _cache(context),
        "data_quality": _data_quality(context),
        "failed_attempts": _failed_attempts(context),
        "runaway_runs": _runaway_runs(context),
        "open_runs": int(
            context.scalar(
                "SELECT COUNT(*) FROM agg_runs r WHERE r.status = 'running' AND "
                + context.instant_condition("r.start_instant")
            )
        ),
        "roles": _roles(context),
    }


def _compactions(context: ReportContext) -> CompactionsSection:
    """Every in-window Compaction checkpoint of the listed Sessions."""
    observations = []
    for session_key, *values in context.query(
        CHECKPOINT_SQL.format(
            sessions=context.sessions_table, window=context.instant_condition("r.instant")
        )
    ):
        session = context.by_key[session_key]
        observations.append(
            CompactionObservation(
                session.display_key, session.address.session_id, session.title, *values
            )
        )
    return CompactionAccumulator(observations).build()


def _cache(context: ReportContext) -> JsonObject:
    """Prompt-cache health of listed Sessions from the cache cubes.

    The lowest hit rates need at least two cache-reporting turns with a
    prompt; suspected breaks are the largest read shortfalls first.
    """
    sessions: list[JsonObject] = []
    evaluated = suspected = 0
    for key, turns, input_tokens, read, write, judged, flagged, last_instant in context.query(
        f"""
        SELECT c.session_key, SUM(c.turns), SUM(c.input_tokens), SUM(c.cache_read_tokens),
            SUM(c.cache_write_tokens), SUM(c.evaluated_turns), SUM(c.suspected_turns),
            MAX(c.last_instant)
        FROM agg_cache c WHERE {context.hour_condition("c")}
        GROUP BY c.session_key
        """
    ):
        session = context.by_key.get(key)
        if session is None:
            continue
        evaluated += judged
        suspected += flagged
        if turns < MIN_CACHE_SESSION_TURNS or input_tokens <= 0:
            continue
        sessions.append(
            {
                "agent_id": session.display_key,
                "session_id": session.address.session_id,
                "cache_turns": turns,
                "input_tokens": input_tokens,
                "cache_read_tokens": read,
                "cache_write_tokens": write,
                "hit_rate": read / input_tokens,
                "last_activity": instant_timestamp(last_instant),
                "session_title": session.title,
            }
        )
    # Worst hit rate first; equal rates surface the bigger Session first.
    sessions.sort(key=lambda row: (row["hit_rate"], -row["input_tokens"], row["session_id"]))
    # Largest read shortfall first, then Session id, time and position.
    breaks: list[tuple[tuple[int, str, int, int, int], JsonObject]] = []
    for key, seq, instant, model, previous_input, read in context.query(
        f"""
        SELECT b.session_key, b.seq, b.instant, b.model_key, b.previous_input_tokens,
            b.cache_read_tokens
        FROM agg_cache_breaks b WHERE {context.instant_condition("b.instant")}
        """
    ):
        session = context.by_key.get(key)
        if session is None:
            continue
        incident = {
            "agent_id": session.display_key,
            "session_id": session.address.session_id,
            "timestamp": instant_timestamp(instant),
            "model": model,
            "previous_input_tokens": previous_input,
            "cache_read_tokens": read,
            "session_title": session.title,
        }
        order = (read - previous_input, session.address.session_id, instant, key, seq)
        breaks.append((order, incident))
    breaks.sort(key=lambda item: item[0])
    return {
        "lowest_hit_rate_sessions": sessions[:TOP_CACHE_SESSIONS],
        "suspected_breaks": {
            "evaluated_turns": evaluated,
            "suspected_turns": suspected,
            "incidents": [incident for _order, incident in breaks[:TOP_CACHE_BREAK_INCIDENTS]],
        },
    }


def _data_quality(context: ReportContext) -> list[JsonObject]:
    return [
        {
            "model": model,
            "calls": calls,
            "unreported_calls": unreported,
            "estimated_token_calls": estimated_tokens,
            "uncached_calls": uncached_calls,
            "uncached_cost_usd": usd(uncached_nusd) if estimated_calls else None,
            "unpriced_calls": unpriced,
            "retrospective_calls": retrospective,
        }
        for (
            model,
            calls,
            unreported,
            estimated_tokens,
            uncached_calls,
            uncached_nusd,
            estimated_calls,
            unpriced,
            retrospective,
        ) in context.query(
            f"""
            SELECT a.model_key, SUM(a.calls) AS calls, SUM(a.unreported_calls),
                SUM(a.estimated_token_calls), SUM(a.uncached_calls), SUM(a.uncached_nusd),
                SUM(a.estimated_calls), SUM(a.unpriced_calls), SUM(a.retrospective_calls)
            FROM agg_usage a WHERE {context.hour_condition("a")}
            GROUP BY a.model_key ORDER BY calls DESC, a.model_key
            """
        )
    ]


def _failed_attempts(context: ReportContext) -> JsonObject:
    """Failed ledger attempts in the window and the worst hours with their Models and actors."""
    failed = failed_calls_sql()
    hours = context.query(
        f"""
        SELECT a.hour, SUM(a.calls), SUM({failed}) AS failed FROM agg_usage a
        WHERE {context.hour_condition("a")}
        GROUP BY a.hour HAVING failed > 0
        ORDER BY failed DESC, a.hour DESC LIMIT {TOP_FAILED_HOURS}
        """
    )
    total = context.scalar(
        f"SELECT COALESCE(SUM({failed}), 0) FROM agg_usage a WHERE {context.hour_condition('a')}"
    )
    selected = ", ".join(str(row[0]) for row in hours)
    models: dict[int, list[JsonObject]] = {}
    agents: dict[int, list[JsonObject]] = {}
    if hours:
        for target, key_sql, joins in (
            (models, "a.model_key", ""),
            (agents, "u.actor", f"JOIN {context.units_table} u ON u.unit_key = a.unit_key"),
        ):
            for hour, key, count in context.query(
                f"""
                SELECT a.hour, {key_sql} AS key, SUM({failed}) AS failed FROM agg_usage a {joins}
                WHERE a.hour IN ({selected})
                GROUP BY a.hour, key HAVING failed > 0
                ORDER BY a.hour, failed DESC, key
                """
            ):
                target.setdefault(hour, []).append({"key": key, "count": count})
    return {
        "total": int(total),
        "hours": [
            {
                "hour_start": hour_timestamp(hour),
                "calls": calls,
                "failed": failed_count,
                "models": models.get(hour, []),
                "agents": agents.get(hour, []),
            }
            for hour, calls, failed_count in hours
        ],
    }


def _runaway_runs(context: ReportContext) -> list[JsonObject]:
    """Runs with many iterations or a cost far above the median positive Run cost."""
    cost = "r.reported_nusd + r.estimated_nusd"
    costs = [
        row[0]
        for row in context.query(
            f"SELECT {cost} AS cost FROM agg_runs r "
            f"WHERE {context.instant_condition('r.start_instant')} AND cost > 0 ORDER BY cost"
        )
    ]
    median = percentile(costs, 50)
    condition = f"r.iterations >= {RUNAWAY_ITERATIONS}"
    if median is not None:
        condition += f" OR {cost} >= {RUNAWAY_COST_FACTOR * median}"
    return context.run_rows(condition, RUN_COST_ORDER, MAX_RUNAWAY_RUNS)


def _roles(context: ReportContext) -> JsonObject:
    records: dict[str, int] = {}
    visible = 0
    for role, count, steps in context.query(
        f"SELECT r.role, SUM(r.records), SUM(r.visible_steps) FROM agg_records r "
        f"WHERE {context.hour_condition('r')} GROUP BY r.role"
    ):
        records[role] = count
        visible += steps
    return {
        "chat_messages_by_role": {"user": records.get("user", 0), "assistant": visible},
        "session_records_by_role": {role: records.get(role, 0) for role in SESSION_RECORD_ROLES},
    }
