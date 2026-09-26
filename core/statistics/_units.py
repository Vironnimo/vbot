"""Report units: the Session slices one Statistics read aggregates, as SQL row sources.

A unit is one surviving Session in report processing order, or one owned Run
of a Session for Extension group usage. Units are loaded into ``temp.units``
so every section aggregates with plain joins against the typed index tables;
unit order is the processing order that breaks ties exactly like a sequential
scan would.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.statistics._measurements import _nearest_rank_index
from core.statistics._projection import datetime_instant
from core.statistics.index import SESSION_FACT_TABLES

if TYPE_CHECKING:
    from core.sessions import SessionAddress
    from core.statistics._extensions import ExtensionSliceKey

_MIN_INSTANT = -(2**63)
_MAX_INSTANT = 2**63 - 1


@dataclass(frozen=True)
class ReportUnit:
    """One aggregated Session, or one owned Run slice of it."""

    display_key: str
    session_key: int
    session_id: str
    title: str | None = None
    extension: ExtensionSliceKey | None = None
    address: SessionAddress | None = None
    run_id: str | None = None


class UnitScan:
    """Units in processing order loaded as ``temp.units``, plus the time window.

    ``temp.units`` columns: ``unit`` (processing position), ``session_key``,
    ``session_id``, ``scan`` (the Session can hold in-window facts) and
    ``extension`` (the unit fills an Extension participant slice).
    """

    call_status_sql = "NULL"

    def __init__(
        self,
        connection: sqlite3.Connection,
        units: Sequence[ReportUnit],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> None:
        self.connection = connection
        self.units = tuple(units)
        self.windowed = since is not None or until is not None
        self.params: dict[str, Any] = {
            "since": _MIN_INSTANT if since is None else datetime_instant(since),
            "until": _MAX_INSTANT if until is None else datetime_instant(until),
        }
        scanned = self._scanned_sessions()
        connection.execute("DROP TABLE IF EXISTS temp.units")
        connection.execute(
            """
            CREATE TEMP TABLE units (
                unit INTEGER PRIMARY KEY,
                session_key INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                scan INTEGER NOT NULL,
                extension INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO temp.units VALUES (?, ?, ?, ?, ?)",
            [
                (
                    index,
                    unit.session_key,
                    unit.session_id,
                    int(unit.session_key in scanned),
                    int(unit.extension is not None),
                )
                for index, unit in enumerate(self.units)
            ],
        )
        connection.execute("CREATE INDEX temp.units_session ON units(session_key)")

    def source(self, table: str, alias: str) -> str:
        """Return a FROM clause yielding ``table`` rows per unit, units outermost."""
        return (
            f"temp.units u CROSS JOIN {self.table(table)} {alias} "
            f"ON {alias}.session_key = u.session_key"
        )

    def table(self, table: str) -> str:
        """Resolve a fact table for this scan's source."""
        return table

    def where(self, alias: str) -> str:
        """Return the scanned-unit and time-window condition for ``alias`` rows."""
        return f"u.scan = 1 AND {self.in_window(alias)}"

    def in_window(self, alias: str) -> str:
        """Return the report window rule for ``alias`` rows."""
        if not self.windowed:
            return "1"
        return f"{alias}.instant BETWEEN :since AND :until"

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> sqlite3.Cursor:
        return self.connection.execute(
            sql, self.params if params is None else {**self.params, **params}
        )

    def nearest_rank(
        self, values_sql: str, counts: Mapping[Hashable, int], percentile: float
    ) -> dict[Hashable, float]:
        """Return the nearest-rank percentile per key of ``values_sql`` rows.

        ``values_sql`` selects ``(key, value)`` rows with non-null values;
        ``counts`` holds each key's row count, so ranks follow the report's
        nearest-rank rule exactly while SQL does the ordering.
        """
        self.connection.execute("DROP TABLE IF EXISTS temp.ranks")
        self.connection.execute("CREATE TEMP TABLE ranks (key PRIMARY KEY, position INTEGER)")
        self.connection.executemany(
            "INSERT INTO temp.ranks VALUES (?, ?)",
            [(key, _nearest_rank_index(count, percentile)) for key, count in counts.items()],
        )
        rows = self.execute(
            f"""
            SELECT ranked.key, ranked.value
            FROM (
                SELECT key, value,
                    ROW_NUMBER() OVER (PARTITION BY key ORDER BY value) - 1 AS position
                FROM ({values_sql})
            ) ranked
            JOIN temp.ranks r ON r.key = ranked.key AND r.position = ranked.position
            """
        )
        return {key: float(value) for key, value in rows}

    def _scanned_sessions(self) -> set[int]:
        """Return Sessions that can contain in-window records.

        Without a window every unit is scanned; with one, a Session whose
        recorded instants lie wholly outside it contributes no windowed facts
        and is skipped.
        """
        keys = {unit.session_key for unit in self.units}
        if not self.windowed:
            return keys
        low, high = self.params["since"], self.params["until"]
        scanned: set[int] = set()
        for session_key, min_instant, max_instant in self.connection.execute(
            "SELECT session_key, min_instant, max_instant FROM stat_sessions"
        ):
            if session_key not in keys:
                continue
            # A Session without records has no instants and nothing to scan.
            if (
                min_instant is not None
                and max_instant is not None
                and max_instant >= low
                and min_instant <= high
            ):
                scanned.add(int(session_key))
        return scanned


def max_timestamp_sql(alias: str) -> str:
    """Order rows so the first is the report's latest-timestamp pick.

    Mirrors the sequential rule: the earliest record carrying the latest
    instant wins.
    """
    return f"{alias}.instant DESC, {alias}.seq"


def materialize_run_slices(
    connection: sqlite3.Connection, slices: Iterable[tuple[int, str]]
) -> set[int]:
    """Shadow the fact tables with owned Run slices re-keyed as pseudo-Sessions.

    ``slices`` pairs a Session key with an owned Run id; each pair's rows are
    selected by that explicit Run id through the Run index and copied into
    same-named temp tables whose ``session_key`` is the pair's position, so
    group sections run the ordinary per-Session aggregation unchanged.
    Returns the positions whose slice holds at least one record.
    """
    connection.execute("DROP TABLE IF EXISTS temp.run_slices")
    connection.execute(
        """
        CREATE TEMP TABLE run_slices (
            slice INTEGER PRIMARY KEY,
            session_key INTEGER NOT NULL,
            run_id TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO temp.run_slices VALUES (?, ?, ?)",
        [(index, session_key, run_id) for index, (session_key, run_id) in enumerate(slices)],
    )
    for table in SESSION_FACT_TABLES:
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA main.table_info({table})")]
        selected = ", ".join(
            "s.slice AS session_key" if name == "session_key" else f"f.{name}" for name in columns
        )
        connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
        connection.execute(
            f"CREATE TEMP TABLE {table} AS SELECT {selected} "
            "FROM temp.run_slices s CROSS JOIN main.stat_records r "
            "ON r.session_key = s.session_key AND r.run_id = s.run_id "
            f"CROSS JOIN main.{table} f ON f.session_key = r.session_key AND f.seq = r.seq "
            "ORDER BY s.slice, f.seq"
        )
        connection.execute(f"CREATE UNIQUE INDEX temp.{table}_key ON {table}(session_key, seq)")
    connection.execute(
        "CREATE INDEX temp.stat_records_run "
        "ON stat_records(session_key, run_id, seq, role, instant)"
    )
    connection.execute("CREATE INDEX temp.stat_runs_run ON stat_runs(session_key, run_id)")
    return {
        int(row[0])
        for row in connection.execute("SELECT DISTINCT session_key FROM temp.stat_records")
    }
