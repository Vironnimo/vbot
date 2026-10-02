"""Shared report context, Totals, Run rows and ranking rules of the report sections.

Money is summed as integer nano-USD in SQL and converted to USD once, so a
total never depends on summation order. A cost is ``None`` (unknown) when
calls exist and none of them is priced; a Run's cost is unknown when every
one of its requests is unpriced.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, NamedTuple

from core.projects.address import format_agent_address
from core.sessions import SessionAddress
from core.statistics._extensions import ExtensionSliceKey, extension_actor_key
from core.statistics._measurements import _nearest_rank_index
from core.statistics._projection import MICROSECONDS_PER_HOUR
from core.statistics._sections.window import LocalCalendar, ReportWindow, instant_timestamp
from core.statistics.skills import _ResolvedInventory

JsonObject = dict[str, Any]

NANO_USD = 1_000_000_000

# One Session address as the index stores it: project id ("" for none), Agent, Session.
_AddressKey = tuple[str, str, str]


@dataclass(frozen=True)
class LiveSession:
    """One listed Session that survived reconciliation, in report listing order."""

    session_key: int
    display_key: str
    address: SessionAddress
    title: str | None
    created_at: str | None
    offered_skills: tuple[str, ...]
    extension: ExtensionSliceKey | None = None
    # False for a background Session (``core.statistics.skills.counts_as_skill_use``).
    skill_use: bool = True


def usd(nusd: int) -> float:
    return nusd / NANO_USD


def cost_usd(calls: int, priced_calls: int, nusd: int) -> float | None:
    """The known cost of ``calls``; ``None`` when calls exist and none is priced."""
    if calls and not priced_calls:
        return None
    return usd(nusd)


def run_cost_usd(calls: int, unpriced_calls: int, nusd: int) -> float | None:
    """A Run's known cost; ``None`` when it has requests and none is priced."""
    if calls and unpriced_calls >= calls:
        return None
    return usd(nusd)


def percentile(sorted_values: Sequence[Any], percent: float) -> Any:
    """Nearest-rank percentile of an ascending sequence; ``None`` when empty."""
    if not sorted_values:
        return None
    return sorted_values[_nearest_rank_index(len(sorted_values), percent)]


def ratio(part: float, total: float) -> float | None:
    return part / total if total else None


def count_entries(counter: Mapping[str, int]) -> list[JsonObject]:
    """``[{key, count}]`` by count, then key."""
    return [
        {"key": key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if count
    ]


def cost_order(cost: float | None, calls: int, key: str) -> tuple[Any, ...]:
    """Sort key: highest known cost first, unknown costs last, then calls and key."""
    return (cost is None, -(cost or 0.0), -calls, key)


def provider_sql(column: str) -> str:
    """The Provider part of a bare Model key column: the text before its first ``/``."""
    return (
        f"CASE WHEN instr({column}, '/') > 0 "
        f"THEN substr({column}, 1, instr({column}, '/') - 1) ELSE {column} END"
    )


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

# Integer measures of usage cube rows, in ``totals_sql`` order.
TOTALS_FIELDS = (
    "calls",
    "failed_calls",
    "input_tokens",
    "estimated_input_tokens",
    "output_tokens",
    "estimated_output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_input_tokens",
    "cache_calls",
    "unreported_calls",
    "reported_nusd",
    "reported_calls",
    "estimated_nusd",
    "estimated_calls",
    "unpriced_calls",
    "retrospective_calls",
    "uncached_nusd",
    "uncached_calls",
    "estimated_token_calls",
)
_FIELD = {name: position for position, name in enumerate(TOTALS_FIELDS)}


# A failed attempt: a finished ledger request that did not complete.
def failed_calls_sql(alias: str = "a") -> str:
    """Failed attempts of ``agg_usage`` rows aliased ``alias``."""
    return f"CASE WHEN {alias}.status NOT IN ('completed', 'started') THEN {alias}.calls ELSE 0 END"


def totals_sql(alias: str = "a") -> str:
    """Integer sums of every Totals measure over ``agg_usage`` rows aliased ``alias``."""
    return ", ".join(
        f"COALESCE(SUM({failed_calls_sql(alias)}), 0)"
        if name == "failed_calls"
        else f"COALESCE(SUM({alias}.{name}), 0)"
        for name in TOTALS_FIELDS
    )


class Totals:
    """Integer usage measures; ``json`` applies the money and unknown-cost rules."""

    __slots__ = ("values",)

    def __init__(self, values: Iterable[int | None] | None = None) -> None:
        self.values = [0] * len(TOTALS_FIELDS) if values is None else [v or 0 for v in values]

    def add(self, values: Sequence[int | None]) -> None:
        current = self.values
        for position, value in enumerate(values):
            if value:
                current[position] += value

    def merge(self, other: Totals) -> None:
        self.add(other.values)

    def __getitem__(self, name: str) -> int:
        return self.values[_FIELD[name]]

    @property
    def cost_nusd(self) -> int:
        return self["reported_nusd"] + self["estimated_nusd"]

    @property
    def cost_usd(self) -> float | None:
        return cost_usd(
            self["calls"], self["reported_calls"] + self["estimated_calls"], self.cost_nusd
        )

    def json(self) -> JsonObject:
        estimated_calls = self["estimated_calls"]
        return {
            "calls": self["calls"],
            "failed_calls": self["failed_calls"],
            "input_tokens": self["input_tokens"],
            "estimated_input_tokens": self["estimated_input_tokens"],
            "output_tokens": self["output_tokens"],
            "estimated_output_tokens": self["estimated_output_tokens"],
            "reasoning_tokens": self["reasoning_tokens"],
            "cache_read_tokens": self["cache_read_tokens"],
            "cache_write_tokens": self["cache_write_tokens"],
            "cache_input_tokens": self["cache_input_tokens"],
            "cache_calls": self["cache_calls"],
            "unreported_calls": self["unreported_calls"],
            "cost_usd": self.cost_usd,
            "reported_cost_usd": (usd(self["reported_nusd"]) if self["reported_calls"] else None),
            "reported_calls": self["reported_calls"],
            "estimated_cost_usd": usd(self["estimated_nusd"]) if estimated_calls else None,
            "estimated_calls": estimated_calls,
            "unpriced_calls": self["unpriced_calls"],
            "retrospective_calls": self["retrospective_calls"],
            "uncached_cost_usd": usd(self["uncached_nusd"]) if estimated_calls else None,
        }


# ---------------------------------------------------------------------------
# Report context
# ---------------------------------------------------------------------------


class ReportContext:
    """One report's reconciled index connection, window and listed Sessions.

    ``sessions_table`` and ``units_table`` load the listed Sessions and the
    ledger units into temporary tables on first use, each with its report
    actor (the display key per-agent rows use), its Project id (``""`` for
    none) and an address id shared by every Session and unit at one Session
    address. A unit at a listed Session's address takes that Session's actor
    and title; another unit is attributed to its Extension owner, else to its
    Agent, else to the standalone actor ``""``.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        window: ReportWindow,
        sessions: Sequence[LiveSession],
        *,
        skill_inventory: _ResolvedInventory | None = None,
    ) -> None:
        self.connection = connection
        self.window = window
        self.calendar = LocalCalendar(window.zone)
        self.sessions = tuple(sessions)
        self.skill_inventory = skill_inventory
        self.by_key: dict[int, LiveSession] = {}
        self._live_addresses: dict[_AddressKey, LiveSession] = {}
        for session in self.sessions:
            self.by_key.setdefault(session.session_key, session)
            self._live_addresses.setdefault(_address_key(session.address), session)
        self._address_ids: dict[_AddressKey, int] = {}
        # Address id -> (actor, Session id, title) for Session-level rows.
        self.addresses: dict[int, tuple[str, str, str | None]] = {}
        # Listed Session key -> its address id.
        self.session_addresses: dict[int, int] = {}

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[Any]:
        return self.connection.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        return self.connection.execute(sql, params).fetchone()[0]

    @cached_property
    def sessions_table(self) -> str:
        self.connection.execute(
            "CREATE TEMP TABLE report_sessions (session_key INTEGER PRIMARY KEY, "
            "actor TEXT NOT NULL, project TEXT NOT NULL, address INTEGER NOT NULL)"
        )
        for key, session in self.by_key.items():
            self.session_addresses[key] = self._address_id(
                _address_key(session.address), session.display_key, session.title
            )
        self.connection.executemany(
            "INSERT INTO temp.report_sessions VALUES (?, ?, ?, ?)",
            [
                (key, session.display_key, session.address.project_id or "", address)
                for (key, session), address in zip(
                    self.by_key.items(), self.session_addresses.values(), strict=True
                )
            ],
        )
        return "temp.report_sessions"

    @cached_property
    def units_table(self) -> str:
        self.sessions_table  # noqa: B018 - Session address ids come first.
        rows = []
        for key, project, agent, session_id, owner, title in self.connection.execute(
            "SELECT session_key, project_id, agent_id, session_id, owner_name, session_title "
            "FROM stat_usage_units"
        ):
            address: int | None = None
            live = self._live_addresses.get((project, agent, session_id))
            if live is not None:
                actor, title = live.display_key, live.title or title
            elif owner:
                actor = extension_actor_key(owner)
            elif agent:
                actor = format_agent_address(agent, project or None)
            else:
                actor = ""
            if agent and session_id:
                address = self._address_id((project, agent, session_id), actor, title)
            rows.append((key, actor, project, address))
        self.connection.execute(
            "CREATE TEMP TABLE report_units (unit_key INTEGER PRIMARY KEY, "
            "actor TEXT NOT NULL, project TEXT NOT NULL, address INTEGER)"
        )
        self.connection.executemany("INSERT INTO temp.report_units VALUES (?, ?, ?, ?)", rows)
        return "temp.report_units"

    def _address_id(self, key: _AddressKey, actor: str, title: str | None) -> int:
        address = self._address_ids.get(key)
        if address is None:
            address = self._address_ids[key] = len(self._address_ids)
            self.addresses[address] = (actor, key[2], title)
        return address

    # -- window conditions ---------------------------------------------------

    def hour_condition(self, alias: str, window: ReportWindow | None = None) -> str:
        selected = window or self.window
        low, high = selected.hours
        return _bounds(f"{alias}.hour", selected, low, high)

    def instant_condition(self, column: str, window: ReportWindow | None = None) -> str:
        selected = window or self.window
        low, high = selected.instants
        return _bounds(column, selected, low, high)

    # -- rows ------------------------------------------------------------------

    def run_row(self, row: Sequence[Any]) -> JsonObject:
        """A ``RunRow`` from an ``agg_runs`` row selected with ``RUN_ROW_SQL``."""
        session = self.by_key.get(row[0])
        return {
            "agent_id": session.display_key if session is not None else "",
            "session_id": session.address.session_id if session is not None else "",
            "session_title": session.title if session is not None else None,
            "run_id": row[1],
            "origin": row[2],
            "status": row[3],
            "started_at": instant_timestamp(row[4]),
            "duration_ms": row[5],
            "cost_usd": run_cost_usd(row[7], row[8], row[6]),
            "input_tokens": row[9],
            "output_tokens": row[10],
            "calls": row[7],
            "model_steps": row[11],
            "tool_calls": row[12],
            "iterations": row[13],
            "primary_model": row[14],
            "models": json.loads(row[15]),
        }

    def run_rows(self, condition: str, order: str, limit: int) -> list[JsonObject]:
        """Up to ``limit`` RunRows of in-window Runs matching ``condition``, by ``order``."""
        return [
            self.run_row(row)
            for row in self.query(
                f"SELECT {RUN_ROW_SQL} FROM agg_runs r "
                f"WHERE {self.instant_condition('r.start_instant')} AND ({condition}) "
                f"ORDER BY {order}, r.start_instant DESC, r.session_key, r.run_id "
                f"LIMIT {int(limit)}"
            )
        ]


# Columns of ``agg_runs r`` that ``ReportContext.run_row`` reads, in its order.
RUN_ROW_SQL = (
    "r.session_key, r.run_id, r.origin, r.status, r.start_instant, r.duration_ms, "
    "r.reported_nusd + r.estimated_nusd, r.calls, r.unpriced_calls, r.input_tokens, "
    "r.output_tokens, r.model_steps, r.tool_calls, r.iterations, r.primary_model, r.models"
)
# Highest known Run cost first; a Run whose every request is unpriced sorts last.
RUN_COST_ORDER = (
    "(r.calls > 0 AND r.unpriced_calls >= r.calls), "
    "r.reported_nusd + r.estimated_nusd DESC, r.calls DESC"
)


def _bounds(column: str, window: ReportWindow, low: int, high: int) -> str:
    """``column`` within ``[low, high)``, naming only the bounded sides.

    An open side adds no condition, so an all-time read scans its table
    instead of walking a time index row by row.
    """
    conditions = []
    if window.since is not None:
        conditions.append(f"{column} >= {low}")
    if window.until is not None:
        conditions.append(f"{column} < {high}")
    return " AND ".join(conditions) or "1"


def _address_key(address: SessionAddress) -> _AddressKey:
    return (address.project_id or "", address.agent_id, address.session_id)


class RunFact(NamedTuple):
    """The measures of one in-window Run that whole-window Run figures read."""

    session_key: int
    origin: str
    status: str
    start_hour: int
    duration_ms: int | None
    first_visible_ms: int | None
    cost_nusd: int
    calls: int
    unpriced_calls: int
    iterations: int | None
    model_steps: int
    tool_calls: int
    tool_ms: int
    changed_files: int | None
    lines_added: int | None
    lines_removed: int | None

    @property
    def cost(self) -> int | None:
        """The known cost in nano-USD; ``None`` when every request is unpriced."""
        if self.calls and self.unpriced_calls >= self.calls:
            return None
        return self.cost_nusd


def load_runs(context: ReportContext, window: ReportWindow | None = None) -> list[RunFact]:
    """Every Run that started in ``window`` (default: the report window)."""
    return [
        RunFact(*row)
        for row in context.query(
            f"""
            SELECT r.session_key, r.origin, r.status, r.start_instant / {MICROSECONDS_PER_HOUR},
                r.duration_ms, r.first_visible_ms, r.reported_nusd + r.estimated_nusd, r.calls,
                r.unpriced_calls, r.iterations, r.model_steps, r.tool_calls, r.tool_ms,
                r.changed_files, r.lines_added, r.lines_removed
            FROM agg_runs r
            WHERE {context.instant_condition("r.start_instant", window)}
            """
        )
    ]


def usage_totals(context: ReportContext, window: ReportWindow | None = None) -> Totals:
    """The usage cube's Totals over ``window`` (default: the report window)."""
    return Totals(
        context.query(
            f"SELECT {totals_sql()} FROM agg_usage a WHERE {context.hour_condition('a', window)}"
        )[0]
    )


def grouped_totals(context: ReportContext, key_sql: str, joins: str = "") -> dict[Any, Totals]:
    """The report window's usage Totals per ``key_sql`` value of ``agg_usage a`` rows."""
    return {
        row[0]: Totals(row[1:])
        for row in context.query(
            f"SELECT {key_sql} AS key, {totals_sql()} FROM agg_usage a {joins} "
            f"WHERE {context.hour_condition('a')} GROUP BY key"
        )
    }


RUN_STATUSES = ("completed", "failed", "cancelled", "interrupted", "running")


def status_counts(runs: Iterable[RunFact]) -> JsonObject:
    """``{total, completed, failed, cancelled, interrupted, running}`` of ``runs``."""
    counts = dict.fromkeys(RUN_STATUSES, 0)
    total = 0
    for run in runs:
        total += 1
        if run.status in counts:
            counts[run.status] += 1
    return {"total": total, **counts}


def sorted_values(values: Iterable[int | None]) -> list[int]:
    return sorted(value for value in values if value is not None)
