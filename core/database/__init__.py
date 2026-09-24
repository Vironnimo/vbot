"""The shared SQLite kernel for every vBot database.

An owner declares a :class:`DatabaseSpec` and opens it with
:func:`open_database`. The kernel owns connections, the journal policy,
serialized writes, pooled reads, the per-database worker pool, schema
evolution (additive reconcile, retired indexes, the migration ledger and
format generations), the canonical and disposable profiles, the data-store
marker and maintenance guard, data snapshots, quarantine, recovery incidents
and automatic restore. See ``.vorch/domain-maps/database.md``.
"""

from core.database._connections import (
    JOURNAL_MODE_DELETE,
    JOURNAL_MODE_WAL,
    has_live_connection,
    is_wal_reset_vulnerable,
    required_journal_mode,
)
from core.database.database import Database, open_database, open_offline_database
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
)
from core.database.marker import (
    MAINTENANCE_GUARD_FILE_NAME,
    MARKER_FILE_NAME,
    DataStoreMarker,
    MaintenanceOperation,
    MarkerEntry,
    begin_maintenance,
    finish_maintenance,
    maintenance,
    read_maintenance,
    read_marker,
    write_bootstrap_marker,
    write_marker_for_databases,
)
from core.database.recovery import (
    acknowledge_incident,
    active_incidents,
    read_incident,
    restore_data_snapshot,
)
from core.database.snapshots import (
    create_data_snapshot,
    list_data_snapshots,
    read_snapshot_health,
    read_verified_manifest,
    snapshot_root,
    snapshot_summaries,
    snapshot_summary,
)
from core.database.spec import (
    APPLICATION_IDS,
    CANONICAL,
    DISPOSABLE,
    DatabaseHealth,
    DatabaseProfile,
    DatabaseSpec,
    Migration,
    SnapshotFacts,
    canonical_database_path,
)
from core.database.status import data_store_status

__all__ = [
    "APPLICATION_IDS",
    "CANONICAL",
    "DISPOSABLE",
    "JOURNAL_MODE_DELETE",
    "JOURNAL_MODE_WAL",
    "MAINTENANCE_GUARD_FILE_NAME",
    "MARKER_FILE_NAME",
    "DataStoreMarker",
    "Database",
    "DatabaseCorruptError",
    "DatabaseError",
    "DatabaseFormatError",
    "DatabaseHealth",
    "DatabaseProfile",
    "DatabaseSpec",
    "DatabaseUnavailableError",
    "IncidentConflictError",
    "MaintenanceOperation",
    "MarkerEntry",
    "Migration",
    "SnapshotFacts",
    "acknowledge_incident",
    "active_incidents",
    "begin_maintenance",
    "canonical_database_path",
    "create_data_snapshot",
    "data_store_status",
    "finish_maintenance",
    "has_live_connection",
    "is_wal_reset_vulnerable",
    "list_data_snapshots",
    "maintenance",
    "open_database",
    "open_offline_database",
    "read_incident",
    "read_maintenance",
    "read_marker",
    "read_snapshot_health",
    "read_verified_manifest",
    "required_journal_mode",
    "restore_data_snapshot",
    "snapshot_root",
    "snapshot_summaries",
    "snapshot_summary",
    "write_bootstrap_marker",
    "write_marker_for_databases",
]
