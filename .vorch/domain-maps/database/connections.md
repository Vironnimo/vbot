# Connections

The kernel's connection policy (`core/database/database.py`, `_runtime.py`, `_connections.py`): the open sequence, the reader pool, transaction retry, closure, transaction control under Python's `sqlite3`, the journal mode and `backup()`. The owner-facing rules (`run_async`, transaction ownership, tracked connections) are in `../database.md`.

## Open sequence

`open_database(spec)` checks application id, generation, `kernel_meta` and the ledger read-only before the journal policy may touch the file, then reconciles, runs `after_open` and probes every declared table and view. A spec's `connection_setup(connection)` runs on the writer and every pooled reader before anything else and must not write; the vector index loads sqlite-vec there.

## Connections

Runtime SQLite connections have no native busy wait; the kernel owns bounded transaction retry: `write(op, *, patience_s=20.0)` and `read_async(op, *, patience_s=20.0)` retry with jitter within their patience budget, which for a write the local transaction lock and SQLite contention share. `write_async`/`run_async` run on the database's own `BoundedWorkerPool` (8 workers, threads `vbot-db-<name>_N`); a closed database raises `DatabaseUnavailableError`. Readers are query-only, open only after the writer has checked identity and reconciled, and form a LIFO pool of 8 permits (60s open retry for operational setup failures, no backoff for transient contention); page caches are 64 MiB for the writer and 16 MiB per reader. Reader admission is independent of the writer transaction lock in both journal modes: a read can start during a reserved write and keeps the committed snapshot established by its first query until its transaction ends. Exhausted reader permits and reader-open failure/backoff use the writer only if its lock is immediately available; otherwise the read fails as unavailable. Synchronous reads never wait for SQLite contention; `read_async` retries the complete callback on the worker within its patience budget. Cold reader setup runs outside the admission lock so it does not block pooled readers.

`is_closed()` observes the monotonic closed flag without taking the transaction lock, so async admission and cached disposable handles never block the Event Loop behind a writer. `close()` stops reader admission, waits for connection opening/disposal to finish, closes pooled and checked-out readers, and keeps writer closure serialized with the current transaction. It does not wait for read callbacks; a reader reaching its transaction boundary after shutdown starts fails as unavailable. Reader closure and permit release have one owner even when shutdown overlaps a reader's cleanup.

The test seam `SYNCHRONOUS_OVERRIDE` (`core/database/database.py`) replaces both profiles' `synchronous` levels wherever the kernel opens a database (PROJECT.md -> Testing -> Disk syncs). A new database is built in a temporary file with `synchronous=OFF`, then fsynced once and published under its name by an atomic rename, so building it syncs only once.

## Transaction control

Every kernel connection opens with `isolation_level=None` under Python's legacy transaction control (`sqlite3.LEGACY_TRANSACTION_CONTROL`, the `autocommit` default through Python 3.14), and the kernel issues its own `BEGIN IMMEDIATE`/`BEGIN`: a statement outside an explicit transaction commits on its own, and `commit()` does nothing unless an explicit `BEGIN` is open. `write` and `read` own their transactions; `after_open(writer)` runs after the reconcile outside any transaction, so a hook step of several statements opens its own `BEGIN IMMEDIATE` and rolls back on failure (`core/sessions/_store_fts.py`, `core/recall/passage_index._prepare_fts`), never relying on `commit()`. `autocommit=True` is the forward spelling and behaves alike, except that `commit()` no longer ends an explicit `BEGIN`; under `autocommit=False` `isolation_level` has no effect and every explicit `BEGIN` fails, so a Python version change checks the `autocommit` default (`development.md` -> Python version).

## Journal policy

Journal policy is per SQLite build (`required_journal_mode()`): WAL unless the build carries the WAL-reset bug, in which case rollback-journal (DELETE) mode is used and an existing WAL file is never live-downgraded. The policy logs one INFO line per process (SQLite version, reason), not one per database; each existing WAL database kept on such a build logs a WARNING once per process. Packaged Windows runtimes pin a fixed WAL-safe SQLite, and the Linux package build refuses a runtime whose SQLite is not WAL-safe (`cli/application.md`); development interpreters must match them, which `scripts/push.py` and the performance scripts check (PROJECT.md -> Development -> Setup).

## Gotchas

- Connection closure waits for SQLite outside the process-wide tracking lock; the connection remains registered until close succeeds, so a slow statement cannot block unrelated database tracking. A read interrupted by shutdown reports `DatabaseUnavailableError`, including a statement attempted on the already closed reader.
- Both journal modes use independent read connections; `backup()` never holds the writer transaction lock. A rollback-journal copy necessarily delays commits, whose contention budget still applies, while other readers proceed. Decode large results after the read transaction ends.
- `backup(dest, *, cancelled) -> bool` uses one `VACUUM INTO` read transaction (cancellable via a progress handler), never a stepped `Connection.backup`, which restarts on every concurrent commit. The copy is not yet synced to disk: the caller makes it durable.

Tests: `tests/core/database/test_database_runtime.py`, `test_database_profiles.py`.
