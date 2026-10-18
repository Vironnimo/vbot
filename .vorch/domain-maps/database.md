# Database

The shared SQLite kernel (`core/database/`): how every vBot database opens, evolves, stays consistent under concurrency, and is snapshotted and recovered.

## Overview

`core/database/` owns connection policy, schema evolution, the canonical and disposable profiles, the data-store marker and maintenance guard, data snapshots and their consistency, recovery (quarantine, incidents, automatic and operator restores, the update rollback), configuration backups and operator-safe status. An owner declares a `DatabaseSpec` and calls `open_database`; it keeps its own DDL, queries, domain errors and domain semantics. The kernel knows no Session, Channel or Provider concept.

Canonical owners: the Session store (`sessions.md`), the Provider usage history (`providers/usage.md`), the Channel state (`channels.md`), the Model Usage recorder (`usage.md`) and Extension databases opened through `host.open_database` (`extensions/databases.md`; the bundled Swarm Extension is one). Disposable owners: the Statistics index (`statistics.md`) and the one Recall Passage index (`recall.md`). Each declares its spec beside its schema (`*_database_spec`).

Not owned here: data-directory placement and creation (`storage.md`; initialization only writes the bootstrap marker, `write_bootstrap_marker`), the lifecycle of Extension databases (when an Extension opens, restores or releases them, `extensions/databases.md`) and the operator surfaces (`cli/data-store.md`, `server.md`, `webui.md`).

## Terms

Core terms (Session, Run, Runtime) live in `.vorch/GLOSSARY.md`.

### Format generation
The incompatible-format counter of one database, stored in `PRAGMA user_version`, `kernel_meta.format_generation` and the marker entry. Generation 1 is current. Within a generation only additive changes happen; a new generation needs an explicit offline converter.

### Migration ledger
The `kernel_migrations` table: one row per applied named data migration with `applied_at`, `applied_by_version` and `breaks_older`. It replaces ad-hoc version keys in owner tables.

### Retired index
An index a spec no longer declares and lists in `retired_indexes`; the kernel drops it on open. The only object the kernel ever drops, because an index is derived data.

### Canonical / disposable profile
Canonical: authoritative data, registered in `data-store.json`, `synchronous=FULL`, included in data snapshots, quarantined and auto-restored on corruption (never on a schema mismatch). Disposable: a rebuildable projection with `kernel_meta.projection_version`, `synchronous=NORMAL`; a version mismatch, foreign/newer file or corruption discards the file with its sidecars and rebuilds it empty. Busy or locked is unavailable in both profiles and never grounds to discard or quarantine.

### Data snapshot
A verified copy of the durable data under `<data-dir>/snapshots/<id>/`: one member per canonical database registered in the marker plus the JSON document set (layout: `database/snapshots-and-recovery.md` -> Files). Not a Session-only backup. Databases restore per member; the documents restore only as one set.

### JSON document set
Every existing file matching `core.json_documents.SNAPSHOT_DOCUMENTS` (which kinds: `json-documents.md` -> Snapshots and doctor), captured as one snapshot member; symbolic links, Windows junctions and names starting with `.` are never members. The Secret Store `secrets.json` is a member (ciphertext only), but whole-set restores keep the live one (`DOCUMENTS_KEPT_BY_RESTORES`): a snapshot set restore and the update rollback neither replace nor remove it, nor does a full configuration backup restore (`secrets.md`).

### Compound mutation
One change that spans a canonical database and durable JSON documents as a unit, such as an Agent rename (Session rows plus `agents/<id>/`). It enters the Runtime's snapshot barrier shared, so an online data snapshot copies it either before or after the whole change (Snapshot consistency). An ordinary write to one database or one document is not one; the member freeze orders those (Capture role).

### Capture role
How an online data snapshot captures one canonical database (`DatabaseSpec.snapshot_capture`). `held` (the default): its writes, like every snapshot document change, wait while a capture freezes the members. `anchor`: the one database that keeps taking writes during the freeze and fixes the instant the snapshot stands for; today `sessions`, so Runs go on. `trailing`: keeps taking writes too and is copied after the freeze ends, so its copy may be newer than the anchor's; only data that stays correct ahead of the anchor may trail; today `model_usage`. A new canonical owner is held unless it must keep writing during a capture and stays correct ahead of the anchor.

### Configuration backup
A frequent, deduplicated copy of the configuration files (`core/database/config_backups.py`): the JSON document set (`secrets.json` included) plus `CONFIGURATION_FILE_PATTERNS`, the Workspace identity files in the default Workspace location and the prompt overrides (`database/config-backups.md`). Not a data snapshot: it holds no database, the Runtime takes it while it runs, and it restores per file.

### Update snapshot
The data snapshot an updater takes for one update operation, offline after the previous server stopped: manifest `reason` `update <operation-id>`, bound in memory to a stamp of the data (`update_rollback.UpdateSnapshot`). Only this snapshot may be restored automatically, and only after that operation's candidate failed its verification start.

## Evolution contract (Generation 1)

This is the engineering contract behind the PROJECT.md "Persisted formats are stable" convention.

1. Additive schema changes apply automatically on open: new tables, views, indexes, triggers, and new columns that are nullable or have a default (not generated, not part of the primary key). The reconciler diffs the declared DDL against the live database and applies the missing objects plus pending migrations in one `BEGIN IMMEDIATE` transaction (logged INFO once per open that changed something); an up-to-date database needs no write transaction.
2. Forbidden within a generation: renaming, retyping or dropping columns or tables; changing or adding constraints on an existing table; a NOT NULL column without default; changing an existing index, view or trigger definition. A changed object gets a new name. The reconciler never drops or rewrites objects and fails closed with `DatabaseSchemaMismatchError` when a live object with the same name differs, when a planned addition fails on the existing rows (a UNIQUE index over duplicates) or when a declared table or view cannot be read; the error names the database, the object and the difference and states the additive rule. The file is intact, so it is never quarantined or replaced from a snapshot. An invalid declaration is a programming error (`ValueError`). Extra live columns, tables and objects are tolerated: a newer vBot may have added them.
3. Data migrations are `Migration(name, breaks_older=False, apply=None)` steps with stable append-only names (`[a-z0-9][a-z0-9_.-]*`, e.g. `sessions.0003_backfill_x`). `apply` must be idempotent and may only fill new structures or NULLs; it runs after the reconcile inside the same transaction. A freshly created database records every declared migration as applied without running it.
4. `breaks_older=True` is required when older code would misread the data (a new role or enum value, a changed meaning); a marker migration without `apply` is enough. An older vBot that finds an unknown ledger row with `breaks_older=1` refuses to open with `DatabaseFormatError` and never quarantines; recovery is the pre-update data snapshot. A release that ships such a migration (core or bundled Extension) says so in its GitHub release notes (`.vorch/workflows/release-workflow.md` -> step 7).
5. SQL names its columns: Runtime reads under `core/`, `server/` or `resources/extensions/` use no `SELECT *` or `alias.*`, and every `INSERT` or `REPLACE` there and in `cli/` lists its columns, because an older binary's positional insert fails once a table gains a column. Only connection-private `temp` tables (`temp.` qualified or `CREATE TEMP TABLE` in the same file) are exempt; `tests/core/database/test_named_columns.py` enforces both over every string literal, without an allowlist.
6. A generation change is a destructive offline conversion by the updater or CLI under a data snapshot and the maintenance guard; normal startup never converts. An older canonical generation, and an existing data directory without a marker (every pre-Generation-1 directory, and any existing directory vBot did not create), raise `DatabaseConversionRequiredError` (a `DatabaseFormatError`). Pre-Generation-1 data (vBot 0.4.4 and earlier) has no converter: it was deleted once every known data directory was converted (user decision, 2026-10-01). That error, the status of an existing directory without a marker and the missing-`format_version` diagnostic of JSON documents (`json-documents.md`) end with `older_format_hint()` (`errors.py`; reasons: `database/agent-facing-text.md`). A newer generation raises a plain `DatabaseFormatError`. App code knows only the current generation.

Stored timestamps are fixed-width UTC text `YYYY-MM-DDTHH:MM:SS.ffffffZ` from `core/utils/timestamps.py`, compared in SQL as text, never through `julianday()`. Designing a table, index or column follows `database/schema-design.md`.

## Data Model

- Kernel tables in every database: `kernel_meta(key, value)` (`database_id`, `database_name`, `format_generation`, `created_at`, `created_by_version`, and `projection_version` for disposable databases) and `kernel_migrations(name, applied_at, applied_by_version, breaks_older)`, both STRICT. `PRAGMA application_id` comes from `APPLICATION_IDS` (one id per owner; Extensions share one).
- Names: `lower_snake` for core owners, `ext.<owner>.<name>` for Extensions. Canonical paths are fixed by name (`canonical_database_path`): `provider_usage` -> `provider-usage.db` (`_` becomes `-`), `ext.<owner>.<name>` -> `extension-data/<owner>/<name>.db`.
- Kernel files in the data directory: `data-store.json` (the marker: every registered database with its `database_id` and generation), `data-maintenance.json` (the maintenance guard, present while an offline operation is incomplete), `data-store.lock` (the OS-owned operation lock: crash-released, never broken by age, exclusive across threads and processes), and the roots `snapshots/`, `incidents/`, `quarantine/` (`database/snapshots-and-recovery.md` -> Files) and `config-backups/`.
- The marker, snapshot manifests and incident records evolve additively like the databases: readers require the known fields, ignore unknown ones, and refuse a marker `format_version` or manifest `manifest_version` other than 1; a record that is rewritten keeps the unknown fields it read, so an older vBot never drops what a newer one added.

## Interfaces

- Declarations (`spec.py`): a `DatabaseSpec` declares identity, profile, generation, schema, migrations and retired indexes, plus optional hooks: `after_open(writer)` (owner checks and repairs after the reconcile), `health(connection)` (status), `snapshot_facts` (integer facts verified per snapshot member), `connection_setup(connection)` (runs first on every connection and must not write; `database/connections.md`), `projection_version` (disposable) and `snapshot_capture` (Capture role).
- Opening: `open_database(spec) -> Database` checks the file read-only, reconciles, runs `after_open` and probes every declared table and view (`database/connections.md` -> Open sequence). A canonical open refuses during maintenance and without a marker, auto-restores a registered database that is missing, corrupt or of another identity (never over an operational failure), and registers an unlisted database, so new owners stay additive (`database/snapshots-and-recovery.md` -> Canonical open and automatic restore). Disposable owners hold a `DisposableDatabase` (`database/disposable.md`). `open_offline_database(spec)` opens a canonical file anywhere without marker, guard, registration or restore (offline conversions, staging, tests): never use it for Runtime paths.
- `Database`: `read()` is one read transaction (contention raises `DatabaseUnavailableError` at once); `write(op)` is one `BEGIN IMMEDIATE` transaction with bounded busy retry that first passes the member freeze; `read_async` retries the whole side-effect-free callback on contention; `write_async`/`run_async` use the database's own worker pool (`database/connections.md`). Owners route all blocking work that uses the database through `run_async`, not `asyncio.to_thread` or a private pool (two databases: one hop each; mostly file or network I/O may keep its own pool where the owning map says why, e.g. `projects/resolution.md`). Metrics: `performance/metric-catalog.md`.
- Errors (`errors.py`): `DatabaseError` base; `DatabaseUnavailableError` (busy, locked, permission, disk full, transient I/O, a contended operation lock; subclasses `OperationLockBusyError` and `MemberFrozenError`); `DatabaseCorruptError` (classified SQLite corruption, failed `quick_check` or `foreign_key_check`, a missing, foreign or mismatched kernel identity, untrusted snapshot/incident evidence) - the only class that grounds quarantine and automatic restore; `DatabaseFormatError` (unreadable or newer marker, maintenance guard, newer generation, unknown `breaks_older` migration, a failing migration or `after_open` check; subclasses `DatabaseConversionRequiredError` and `DatabaseSchemaMismatchError`) - never quarantined or restored; `IncidentConflictError`; `UpdateRollbackRefusedError`. None inherits a domain error; owners may subclass (`SessionStoreCorruptError`). Messages name the database and the failure kind; RPC mapping: `server/operator-surfaces.md`.
- Runtime composition: `Runtime.snapshot_barrier` is the one barrier of a Runtime (kept across restarts) and is injected into the stores and services that hold compound mutations (`database/snapshot-consistency.md` -> Barrier holders); a store constructed without one gets a private barrier no snapshot uses. `Runtime.canonical_databases()` returns the open handles; `core/runtime/databases.canonical_database_specs(data_dir)` declares them for offline tools (CLI status and restore, the updater worker's snapshot and rollback, which run the code that wrote the data). A new canonical owner is added in both places; Extension databases have no offline spec (`extensions/databases.md`).

## Conventions

- Process-wide state: the kernel keeps two pieces of process-wide state, keyed by resolved path, because they describe what this process does to files, not an object graph (the named exception in PROJECT.md -> Conventions -> Dependency injection). Tracked connections: every connection goes through `connect_tracked`, and quarantine, discard and restore refuse while `has_live_connection` reports one for the file. The member freeze, keyed by the resolved data directory: every `Database.write` and JSON document write of a data directory passes it, including standalone stores, offline tools and Extension databases no Runtime constructs; `write_json_document` callers have no injected object a per-Runtime freeze could travel through, and a gate keyed by path cannot be missed by an owner that was never handed it. The snapshot barrier, by contrast, is injected (Runtime composition).
- Restore identity: a restore keeps `database_id` but brings back older content, so a projection or cursor that follows a canonical database keys on `database_id` and `Database.restore_id` (the `incident_id` of its latest restore, else `None`) and rebuilds when `restore_id` changes (`usage.md`; Statistics and Recall through `ChatSessionManager.history_id`; mechanics: `database/snapshots-and-recovery.md` -> Canonical open and automatic restore).
- Transaction control: under legacy transaction control a statement outside an explicit transaction commits on its own, and `commit()` ends nothing unless a `BEGIN` is open. `write` and `read` own their transactions; `after_open(writer)` runs outside any, so a multi-statement hook step opens its own `BEGIN IMMEDIATE` and rolls back on failure (`database/connections.md` -> Transaction control).
- Owners never open their own connections to a kernel database, never commit inside a helper that receives the kernel's connection, and keep domain decisions (e.g. FTS repair) in their `after_open`/`health` hooks.

## Snapshot consistency

An online data snapshot shows exactly what a crash at one instant would have left - the start of the anchor copy - and holds every compound mutation whole (`database/snapshot-consistency.md`). For owners:

- `Database.write` and canonical open gate every registered database (offline and disposable handles never), and `write_json_document` every document write; owners wrap removals and moves of snapshot documents outside compound mutations in `document_change` (`json-documents.md` -> Writing).
- Compound mutations enter `compound_mutation()` (worker threads) or `compound_mutation_async()` (Event Loop) before any store lock and outside database transactions, never take the operation lock and stay short; nesting is allowed.
- Freeze admission: a held change on an Event Loop thread, or inside another change of the same data directory, enters at once and the capture retries. A waiting change keeps its thread's locks, so an owner whose lock Event Loop code takes admits first (`document_change`, then the lock); under that lock it passes `wait=False` and on `MemberFrozenError` gives the lock up and repeats (`AgentStore`).
- Crash equivalence: a multi-step change outside a compound mutation must leave only states that repair themselves or strand no data, since every crash window is capturable.

## Agent-facing text

This domain owns the data-format and data-store marker refusals Agents read through startup failures, `vbot data-store status` and `vbot doctor config`. Before you change, add or delete a sentence of them, read `database/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing snapshots, the marker format, canonical open and automatic restore, incidents, quarantine, staging, unregister or retire, `remove_retired_document_copies`, `read_retained_snapshot_members`, the update rollback, status or the kernel's log lines -> `database/snapshots-and-recovery.md`
- Adding a compound mutation or a snapshot-member write under an owner lock or on the Event Loop, or changing the barrier, freeze or capture order -> `database/snapshot-consistency.md`
- Changing configuration backups or `purge_config_backup_files` -> `database/config-backups.md`
- Changing the open sequence, connections, the reader pool, retry, closure, transaction control, the journal mode or `backup()` -> `database/connections.md`
- Designing or changing a table, column, index or stored timestamp in any kernel database -> `database/schema-design.md`
- Writing or changing a disposable owner or `DisposableDatabase` -> `database/disposable.md`
