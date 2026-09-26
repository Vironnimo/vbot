"""Report scans over durable requests, independent of retained Session records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from core.projects.address import format_agent_address
from core.sessions import SessionAddress
from core.statistics._accumulators import ReportLedger
from core.statistics._extensions import extension_actor_key
from core.statistics._projection import CALL_COLUMNS, datetime_instant
from core.statistics._units import ReportUnit, UnitScan
from core.statistics._usage import (
    ESTIMATED_INPUT_SQL,
    ESTIMATED_OUTPUT_SQL,
    MEASURED_INPUT_SQL,
    MEASURED_OUTPUT_SQL,
)
from core.statistics.report import RunActivity

RunKey = tuple[SessionAddress | int, str]


class AccountingScan(UnitScan):
    """Select durable call facts, retaining live labels and Extension slices."""

    call_status_sql = "r.status"

    def __init__(
        self,
        connection: sqlite3.Connection,
        live_units: Sequence[ReportUnit],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        group: bool = False,
    ) -> None:
        self._prefix = "stat_selected_usage" if group else "stat_usage"
        if group:
            _select_runs(connection, live_units)
            units = list(live_units)
            self.live_positions: list[int | None] = list(range(len(units)))
        else:
            live = {
                unit.address: (position, unit)
                for position, unit in enumerate(live_units)
                if unit.address is not None
            }
            units = []
            self.live_positions = []
            conditions = ["c.session_key = a.session_key"]
            bounds = []
            if since is not None:
                conditions.append("c.instant >= ?")
                bounds.append(datetime_instant(since))
            if until is not None:
                conditions.append("c.instant <= ?")
                bounds.append(datetime_instant(until))
            for key, project, agent, session, owner, title in connection.execute(
                "SELECT a.session_key, a.project_id, a.agent_id, a.session_id, a.owner_name, "
                "a.session_title FROM stat_usage_units a WHERE EXISTS "
                f"(SELECT 1 FROM stat_usage_calls c WHERE {' AND '.join(conditions)}) "
                "ORDER BY a.session_key",
                bounds,
            ):
                address = (
                    SessionAddress(project or None, agent, session) if agent and session else None
                )
                current = live.get(address) if address is not None else None
                if current is not None:
                    position, unit = current
                    units.append(replace(unit, session_key=key))
                    self.live_positions.append(position)
                else:
                    display = (
                        extension_actor_key(owner)
                        if owner
                        else format_agent_address(agent, project or None)
                        if agent
                        else ""
                    )
                    units.append(ReportUnit(display, key, session, title=title, address=address))
                    self.live_positions.append(None)
        super().__init__(connection, units, since=since, until=until)

    def table(self, table: str) -> str:
        if table in {"stat_calls", "stat_records"}:
            return f"{self._prefix}_{table.removeprefix('stat_')}"
        return table

    def _scanned_sessions(self) -> set[int]:
        # Archived and standalone requests have no live Session range. Their
        # own indexed request timestamps supply the authoritative window.
        return {unit.session_key for unit in self.units}


def _select_runs(connection: sqlite3.Connection, units: Sequence[ReportUnit]) -> None:
    """Restrict Extension group usage to its exact owned Run identities."""
    connection.execute("DROP TABLE IF EXISTS temp.usage_run_slices")
    connection.execute(
        "CREATE TEMP TABLE usage_run_slices "
        "(session_key INTEGER, project_id TEXT, agent_id TEXT, session_id TEXT, run_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO temp.usage_run_slices "
        "(session_key, project_id, agent_id, session_id, run_id) VALUES (?, ?, ?, ?, ?)",
        [
            (
                unit.session_key,
                unit.address.project_id or "",
                unit.address.agent_id,
                unit.address.session_id,
                unit.run_id,
            )
            for unit in units
            if unit.address is not None and unit.run_id is not None
        ],
    )
    selected = ", ".join(
        "s.session_key AS session_key" if name.strip() == "session_key" else f"c.{name.strip()}"
        for name in CALL_COLUMNS.split(",")
    )
    for suffix in ("calls", "records"):
        connection.execute(f"DROP TABLE IF EXISTS temp.stat_selected_usage_{suffix}")
    # Drive these lookups from the requested Run slices. Without a fixed join
    # order SQLite may scan all request records and build an automatic index
    # of the slices instead of using the Session/Run index.
    connection.execute(
        "CREATE TEMP TABLE stat_selected_usage_records AS "
        "SELECT s.session_key, r.seq, r.timestamp, r.instant, r.run_id, r.status "
        "FROM temp.usage_run_slices s "
        "CROSS JOIN stat_usage_units a ON a.project_id = s.project_id AND a.agent_id = s.agent_id "
        "AND a.session_id = s.session_id "
        "CROSS JOIN stat_usage_records r ON r.session_key = a.session_key AND r.run_id = s.run_id"
    )
    connection.execute(
        f"CREATE TEMP TABLE stat_selected_usage_calls AS SELECT {selected} "
        "FROM temp.stat_selected_usage_records s "
        # The selected Session key is a Run-slice position, not the ledger's
        # Session key. Recover the latter by the record's primary key so the
        # call lookup uses (session_key, seq), without indexing all unrelated
        # requests for each group and participant report.
        "CROSS JOIN stat_usage_records r ON r.seq = s.seq "
        "CROSS JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq"
    )
    # A takeover changes the Session address without reattributing incurred
    # requests. Keep its retained diagnostic slice when the new exact address
    # has no recorded requests; Run ids alone cannot establish an alias.
    connection.execute("DROP TABLE IF EXISTS temp.usage_fallback_slices")
    connection.execute(
        "CREATE TEMP TABLE usage_fallback_slices AS "
        "SELECT s.session_key FROM temp.usage_run_slices s WHERE NOT EXISTS "
        "(SELECT 1 FROM temp.stat_selected_usage_records r "
        "WHERE r.session_key = s.session_key)"
    )
    saved_columns = ", ".join(f"c.{name.strip()}" for name in CALL_COLUMNS.split(","))
    connection.execute(
        f"INSERT INTO temp.stat_selected_usage_calls ({CALL_COLUMNS}) "
        f"SELECT {saved_columns} FROM temp.usage_fallback_slices s "
        "JOIN temp.stat_calls c ON c.session_key = s.session_key"
    )
    connection.execute(
        "INSERT INTO temp.stat_selected_usage_records "
        "(session_key, seq, timestamp, instant, run_id, status) "
        "SELECT r.session_key, r.seq, r.timestamp, r.instant, r.run_id, NULL "
        "FROM temp.usage_fallback_slices s "
        "JOIN temp.stat_records r ON r.session_key = s.session_key "
        "JOIN temp.stat_calls c ON c.session_key = r.session_key AND c.seq = r.seq"
    )
    for suffix in ("calls", "records"):
        connection.execute(
            f"CREATE UNIQUE INDEX temp.stat_selected_usage_{suffix}_key "
            f"ON stat_selected_usage_{suffix}(session_key, seq)"
        )


def account_run_activity(
    connection: sqlite3.Connection,
    runs: Sequence[RunActivity],
    units: Sequence[ReportUnit],
) -> list[RunActivity]:
    """Use every recorded attempt in the selected Runs, including auxiliary work."""
    addresses = {
        (unit.display_key, unit.session_id): unit.address
        for unit in units
        if unit.address is not None
    }
    result = []
    for run in runs:
        address = addresses.get((run.agent_id, run.session_id))
        if address is None:
            result.append(run)
            continue
        row = connection.execute(
            f"""
            SELECT {MEASURED_INPUT_SQL}, {ESTIMATED_INPUT_SQL},
                {MEASURED_OUTPUT_SQL}, {ESTIMATED_OUTPUT_SQL},
                json_group_array(DISTINCT c.model_key), COUNT(*)
            FROM stat_usage_units a
            JOIN stat_usage_records r ON r.session_key = a.session_key
            JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq
            WHERE a.project_id = ? AND a.agent_id = ? AND a.session_id = ? AND r.run_id = ?
            """,
            (address.project_id or "", address.agent_id, address.session_id, run.run_id),
        ).fetchone()
        if row[5] == 0:
            result.append(run)
            continue
        result.append(
            replace(
                run,
                measured_input_tokens=row[0] or 0,
                estimated_input_tokens=row[1] or 0,
                measured_output_tokens=row[2] or 0,
                estimated_output_tokens=row[3] or 0,
                models=sorted(json.loads(row[4])),
            )
        )
    return result


def retained_run_durations(scan: UnitScan) -> dict[RunKey, int]:
    """Read known full Run durations before the financial scan replaces its units."""
    durations = {}
    for unit, run_id, duration in scan.execute(
        f"SELECT u.unit, r.run_id, r.duration_ms FROM {scan.source('stat_runs', 'r')} "
        "WHERE r.run_id IS NOT NULL AND r.duration_ms IS NOT NULL"
    ):
        report_unit = scan.units[unit]
        durations[(report_unit.address or report_unit.session_key, run_id)] = duration
    return durations


def account_model_runs(
    scan: AccountingScan, ledger: ReportLedger, durations: dict[RunKey, int]
) -> None:
    """Count each Model and Provider once per Run with an in-window request."""
    for model in ledger.models.values():
        model.runs = 0
        model.run_duration_total_ms = 0
        model.run_duration_count = 0
    for provider in ledger.providers.values():
        provider.runs = 0
    models: dict[str, set[RunKey]] = {}
    providers: dict[str, set[RunKey]] = {}
    for unit, run_id, model_key in scan.execute(
        f"""
        SELECT u.unit, r.run_id, c.model_key
        FROM {scan.source("stat_calls", "c")}
        JOIN {scan.table("stat_records")} r ON r.session_key = c.session_key AND r.seq = c.seq
        WHERE {scan.where("c")} AND r.run_id IS NOT NULL AND r.run_id <> ''
        GROUP BY u.unit, r.run_id, c.model_key
        """
    ):
        report_unit = scan.units[unit]
        key = (report_unit.address or report_unit.session_key, run_id)
        model = ledger.model(model_key)
        models.setdefault(model_key, set()).add(key)
        providers.setdefault(model.provider, set()).add(key)
    for model_key, keys in models.items():
        model = ledger.model(model_key)
        model.runs = len(keys)
        known = [durations[key] for key in keys if key in durations]
        model.run_duration_count = len(known)
        model.run_duration_total_ms = sum(known)
    for provider_key, keys in providers.items():
        ledger.provider(provider_key).runs = len(keys)
