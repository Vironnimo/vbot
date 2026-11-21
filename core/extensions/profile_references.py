"""Read durable Permission Profile selections, including disabled bundled Extensions.

Do not activate an Extension to inspect its data. These narrow queries are
contracts with the Swarm and Factory stores, including their pre-existing rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from core.database import (
    APPLICATION_IDS,
    Database,
    DatabaseError,
    canonical_database_path,
    read_marker,
)
from core.database.recovery import pending_restore
from core.database.spec import canonical_data_dir
from core.database.status import inspect_registered_database
from core.utils.file_status import exists_strict


def write_profile_assignment[T](
    database: Database, names: Iterable[str | None], write: Callable[[sqlite3.Connection], T]
) -> T:
    """Commit a profile selection while the Settings catalog still admits it."""
    from core.storage.storage import permission_profile_assignment

    with permission_profile_assignment(
        database.data_dir or canonical_data_dir(database.spec), names
    ):
        return database.write(write)


_SWARM = """
    WITH selections(label, profile) AS (
        SELECT 'Swarm profile ' || id, payload FROM profiles
        UNION ALL
        SELECT 'Swarm ' || id, profile_snapshot FROM swarms WHERE state != 'deleting'
    )
    SELECT label FROM selections
    WHERE json_extract(profile, '$.permission_ceiling_profile') = :name
       OR EXISTS (
           SELECT 1 FROM json_each(profile, '$.participants') AS participant
           WHERE json_extract(participant.value, '$.permission_profile') = :name
       )
    ORDER BY label
"""
_FACTORY = """
    SELECT 'Factory ' || id FROM native_factories
    WHERE json_extract(body_json, '$.config.permission_ceiling_profile') = :name
    ORDER BY id
"""
_BEFORE_NATIVE_FACTORY = """
    SELECT NOT EXISTS (SELECT 1 FROM sqlite_schema WHERE name = 'native_factories')
       AND NOT EXISTS (SELECT 1 FROM kernel_migrations WHERE name = 'native_factory_teams')
       AND EXISTS (SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'cards')
"""


def extension_profile_references(data_dir: Path, name: str) -> tuple[list[str], list[str]]:
    """Return assignments and locations whose assignments could not be verified."""
    references: list[str] = []
    unavailable: list[str] = []
    try:
        marker = read_marker(data_dir)
    except DatabaseError:
        marker = None
    if marker is None:
        return [], ["data-store.json"]
    for database, query in (("ext.swarm.swarm", _SWARM), ("ext.factory.factory", _FACTORY)):
        path = canonical_database_path(data_dir, database)
        entry = marker.databases.get(database)
        try:
            if entry is None:
                if exists_strict(path):
                    unavailable.append(database)
                continue
            if entry.format_generation != 1 or pending_restore(data_dir, database):
                unavailable.append(database)
                continue
            with inspect_registered_database(
                data_dir, database, entry, application_id=APPLICATION_IDS["extensions"]
            ) as db:
                # Before native Factory teams the store held only Workflows and Cards.
                # A missing table after its migration is damage: run the query and refuse.
                if (
                    database == "ext.factory.factory"
                    and db.execute(_BEFORE_NATIVE_FACTORY).fetchone()[0]
                ):
                    continue
                references.extend(row[0] for row in db.execute(query, {"name": name}))
        except DatabaseError, OSError, sqlite3.Error:
            unavailable.append(database)
    return references, unavailable
