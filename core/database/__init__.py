"""The shared SQLite kernel for every vBot database.

An owner declares a :class:`DatabaseSpec` and opens it with
:func:`open_database`. The kernel owns connections, the journal policy,
serialized writes, pooled reads, the per-database worker pool, schema
evolution (additive reconcile, retired indexes, the migration ledger and
format generations), the canonical and disposable profiles, the data-store
marker and maintenance guard, data snapshots (every canonical database plus
the JSON document set, taken online as a crash at one instant would leave them
and kept apart from compound mutations by the :class:`SnapshotBarrier`; see
``core.database.snapshot_barrier``), quarantine, recovery incidents, automatic
restore and the updater's guarded pre-update snapshot rollback. See
``.vorch/domain-maps/database.md``. :class:`DisposableDatabase` and
:func:`projection_failure` serve owners of disposable projections at runtime.
"""

from core.database._connections import (
    JOURNAL_MODE_DELETE,
    JOURNAL_MODE_WAL,
    has_live_connection,
    is_wal_reset_vulnerable,
    required_journal_mode,
)
from core.database.database import Database, open_database, open_offline_database
from core.database.disposable import DisposableDatabase, ProjectionFailure, projection_failure
from core.database.errors import (
    GENERATION_1_CONVERTER_COMMAND,
    DatabaseConversionRequiredError,
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseSchemaMismatchError,
    DatabaseUnavailableError,
    IncidentConflictError,
    UpdateRollbackRefusedError,
    generation_1_conversion_hint,
)
from core.database.marker import (
    MAINTENANCE_GUARD_FILE_NAME,
    MARKER_FILE_NAME,
    DataStoreMarker,
    MaintenanceOperation,
    MarkerEntry,
    begin_maintenance,
    describe_missing_databases,
    finish_maintenance,
    maintenance,
    read_maintenance,
    read_marker,
    write_bootstrap_marker,
    write_marker_for_databases,
)
from core.database.recovery import (
    SnapshotRestore,
    UnregisteredDatabase,
    acknowledge_incident,
    active_incidents,
    read_incident,
    restore_data_snapshot,
    unregister_database,
)
from core.database.snapshot_barrier import SnapshotBarrier
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
    ANCHOR_CAPTURE,
    APPLICATION_IDS,
    CANONICAL,
    DISPOSABLE,
    HELD_CAPTURE,
    TRAILING_CAPTURE,
    DatabaseHealth,
    DatabaseProfile,
    DatabaseSpec,
    Migration,
    SnapshotCapture,
    SnapshotFacts,
    canonical_database_path,
    is_extension_database_name,
)
from core.database.status import data_store_status
from core.database.update_rollback import (
    UpdateSnapshot,
    create_update_snapshot,
    data_changed_since,
    find_update_snapshot,
    restore_update_snapshot,
)

__all__ = [
    "ANCHOR_CAPTURE",
    "APPLICATION_IDS",
    "CANONICAL",
    "DISPOSABLE",
    "HELD_CAPTURE",
    "GENERATION_1_CONVERTER_COMMAND",
    "JOURNAL_MODE_DELETE",
    "JOURNAL_MODE_WAL",
    "MAINTENANCE_GUARD_FILE_NAME",
    "MARKER_FILE_NAME",
    "TRAILING_CAPTURE",
    "DataStoreMarker",
    "Database",
    "DatabaseConversionRequiredError",
    "DatabaseCorruptError",
    "DatabaseError",
    "DatabaseFormatError",
    "DatabaseHealth",
    "DatabaseProfile",
    "DatabaseSchemaMismatchError",
    "DatabaseSpec",
    "DatabaseUnavailableError",
    "DisposableDatabase",
    "IncidentConflictError",
    "MaintenanceOperation",
    "MarkerEntry",
    "Migration",
    "ProjectionFailure",
    "SnapshotBarrier",
    "SnapshotCapture",
    "SnapshotFacts",
    "SnapshotRestore",
    "UnregisteredDatabase",
    "UpdateRollbackRefusedError",
    "UpdateSnapshot",
    "acknowledge_incident",
    "active_incidents",
    "begin_maintenance",
    "canonical_database_path",
    "create_data_snapshot",
    "create_update_snapshot",
    "data_changed_since",
    "data_store_status",
    "describe_missing_databases",
    "find_update_snapshot",
    "finish_maintenance",
    "generation_1_conversion_hint",
    "has_live_connection",
    "is_extension_database_name",
    "is_wal_reset_vulnerable",
    "list_data_snapshots",
    "maintenance",
    "open_database",
    "open_offline_database",
    "projection_failure",
    "read_incident",
    "read_maintenance",
    "read_marker",
    "read_snapshot_health",
    "read_verified_manifest",
    "required_journal_mode",
    "restore_data_snapshot",
    "restore_update_snapshot",
    "snapshot_root",
    "snapshot_summaries",
    "snapshot_summary",
    "unregister_database",
    "write_bootstrap_marker",
    "write_marker_for_databases",
]
