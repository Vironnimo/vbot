# Extension Databases

How an Extension opens, migrates, releases and unregisters its own databases through the owner-bound host (API 7), and how startup modes, snapshots and restores treat them. This file owns the Extension side; the kernel side (identity, snapshots, quarantine, restore) is `database.md`.

## Opening and ownership

An owner-bound host offers `await host.open_database(name, schema_sql, migrations=(), retired_indexes=())`, returning a kernel `Database` (`database.md`) for the canonical database `ext.<owner>.<name>` at `<data-dir>/extension-data/<owner>/<name>.db` with the shared Extension application id at format generation 1. `name` and the Extension id must be lowercase safe ids (`core.utils.ids.is_safe_id`), else `ValueError`; a second open of an already open name is refused. First open creates and registers it in the data-store marker; reopening applies the kernel's additive reconcile and named migrations, so schema changes stay additive.

`core/extensions/databases.py::ExtensionDatabases`, created by `ExtensionHostFactory`, owns the handles per registration identity: `ExtensionRegistry` releases a registration after that owner's shutdown handlers in `fire_shutdown` (Runtime shutdown, reload) and `deactivate` (live disable), closing its handles; a released registration can never open again, and the reloaded Extension opens anew. Runtime shutdown closes any handle a release missed.

The returned `Database` carries the kernel's `read_async`/`write_async`/`run_async`, so an Extension runs its blocking work on that database's own worker pool instead of a private one. Extensions import `Database`, `Migration` and `DatabaseError` from `core.extensions.databases` and never open SQLite files, `core.database` internals or `core.sessions` storage themselves.

## Startup and opening point

The owner-bound host is bound only when `ExtensionRuntime.startup` fires startup handlers from the server lifespan after Runtime bootstrap (and again after a reload), so the earliest open is an `api.operations.startup` handler (the Swarm Extension opens there). Safe startup modes (`--verification-only`, test instances) load no Extensions (`core/runtime/_bootstrap.py`) and fire no startup handlers, so an updater's candidate verification never opens, migrates or auto-restores an Extension database, and an Extension open failure never triggers the automatic data rollback. Startup handlers are fail-open: an open failure is logged as the handler's error and the server still starts healthy, with that Extension lacking its database.

## Snapshots, restore and offline tools

`Runtime.canonical_databases()` includes the open handles, so data snapshots copy them online; registered but unopened ones are copied from their files. Offline tools (CLI data-store status/restore, updater snapshot) never declare Extension specs, because that would execute Extension code: they verify Extension members by kernel identity and integrity only, without owner facts or schema compatibility checks.

Automatic restore of a missing or corrupt Extension database happens at that open; the operator path is `vbot data-store snapshot restore <id> --database ext.<owner>.<name> --yes`, which checks only kernel identity and integrity and leaves reconcile, migrations and any refusal to the Extension's next open after the restart (`database/snapshots-and-recovery.md` -> Snapshot creation and retention).

## Removal and unregister

No uninstall flow exists: removing an Extension (deleting it from an Extension root) archives its owner-managed Sessions at the next load (`extensions/host.md` -> Owner-managed Session lifetime) but leaves its databases registered and their files in place, so data snapshots keep copying them; once a registered file is gone too, data snapshots and update snapshots refuse with the release command (`database.md`).

`ExtensionDatabases.unregister(name)`, reached through `Runtime.unregister_extension_database`, RPC `data_store.unregister` and `vbot data-store unregister <name> --yes`, moves the files to quarantine and drops the registration; it is refused while a registration has the database open or is opening it, and an open that starts meanwhile waits, then creates a new, empty database. A disabled but installed Extension's database can be released too; its next open starts empty.

Evidence: `tests/core/extensions/test_extension_databases.py`, `tests/core/runtime/test_runtime_extension_databases.py`.
