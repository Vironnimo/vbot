"""Tools: calls, outcomes and approximate latency percentiles per Tool, rejection codes, Agents.

Latency percentiles come from the hourly duration histogram, whose bucket
``b`` holds durations ``d`` with ``floor(4 * log2(d + 1)) == b`` (about 19%
wide). The nearest-rank sample is placed inside its bucket by geometric
interpolation, then clamped to the bucket's whole milliseconds and to the
Tool's longest call, so the figures are approximate.
"""

from __future__ import annotations

import math
from collections import Counter

from core.statistics._measurements import _nearest_rank_index
from core.statistics._sections.common import JsonObject, ReportContext, ratio

TOP_CODES = 5


def build(context: ReportContext) -> JsonObject:
    tools = context.query(
        f"""
        SELECT t.name, SUM(t.calls), SUM(t.accepted), SUM(t.rejected), SUM(t.unknown),
            SUM(t.duration_ms), MAX(t.max_ms)
        FROM agg_tools t WHERE {context.hour_condition("t")}
        GROUP BY t.name
        """
    )
    histograms: dict[str, list[tuple[int, int]]] = {}
    for name, bucket, count in context.query(
        f"""
        SELECT l.name, l.bucket, SUM(l.count) FROM agg_tool_latency l
        WHERE {context.hour_condition("l")}
        GROUP BY l.name, l.bucket ORDER BY l.name, l.bucket
        """
    ):
        histograms.setdefault(name, []).append((bucket, count))
    codes: dict[str, Counter[str]] = {}
    code_tools: dict[str, Counter[str]] = {}
    for name, code, count in context.query(
        f"""
        SELECT t.name, t.error_code, COUNT(*) FROM stat_tools t
        WHERE t.outcome = 0 AND {context.instant_condition("t.instant")}
        GROUP BY t.name, t.error_code
        """
    ):
        code = code or "unknown"
        codes.setdefault(name, Counter())[code] += count
        code_tools.setdefault(code, Counter())[name] += count
    total_ms = sum(row[5] or 0 for row in tools)
    rows = []
    for name, calls, accepted, rejected, _unknown, duration_ms, max_ms in tools:
        histogram = histograms.get(name, [])
        rows.append(
            {
                "name": name,
                "calls": calls,
                "accepted": accepted,
                "rejected": rejected,
                "rejection_rate": ratio(rejected, calls),
                "p50_ms": histogram_percentile(histogram, 50, max_ms),
                "p95_ms": histogram_percentile(histogram, 95, max_ms),
                "max_ms": max_ms,
                "total_ms": duration_ms,
                "time_share": ratio(duration_ms, total_ms),
                "top_codes": [
                    {"code": code, "count": count}
                    for code, count in _ranked(codes.get(name, Counter()))[:TOP_CODES]
                ],
            }
        )
    rows.sort(key=lambda row: (-row["calls"], row["name"]))
    return {
        "totals": {
            "calls": sum(row[1] for row in tools),
            "accepted": sum(row[2] for row in tools),
            "rejected": sum(row[3] for row in tools),
            "unknown": sum(row[4] for row in tools),
            "tool_ms": total_ms,
            "tools": len(tools),
        },
        "tools": rows,
        "rejection_codes": [
            {"code": code, "count": count, "tools": [name for name, _ in _ranked(code_tools[code])]}
            for code, count in _ranked(
                Counter({code: sum(names.values()) for code, names in code_tools.items()})
            )
        ],
        "by_agent": [
            {"agent_id": actor, "calls": calls, "rejected": rejected, "tool_ms": tool_ms}
            for actor, calls, rejected, tool_ms in context.query(
                f"""
                SELECT s.actor, SUM(t.calls) AS calls, SUM(t.rejected), SUM(t.duration_ms)
                FROM agg_tools t JOIN {context.sessions_table} s ON s.session_key = t.session_key
                WHERE {context.hour_condition("t")}
                GROUP BY s.actor ORDER BY calls DESC, s.actor
                """
            )
        ],
    }


def histogram_percentile(
    histogram: list[tuple[int, int]], percent: float, max_ms: int | None
) -> int | None:
    """The approximate nearest-rank percentile of ascending ``(bucket, count)`` pairs."""
    total = sum(count for _bucket, count in histogram)
    if not total:
        return None
    rank = _nearest_rank_index(total, percent) + 1
    seen = 0
    for bucket, count in histogram:
        if seen + count >= rank:
            fraction = (rank - seen - 0.5) / count
            value = 2 ** ((bucket + fraction) / 4) - 1
            low = math.ceil(2 ** (bucket / 4) - 1)
            high = max(low, math.ceil(2 ** ((bucket + 1) / 4) - 1) - 1)
            value = min(max(value, low), high)
            if max_ms is not None:
                value = min(value, max_ms)
            return round(value)
        seen += count
    return None


def _ranked(counter: Counter[str]) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))
