# Sessions

Canonical Session history, metadata, completion activity, continuation state, and SQLite persistence.

## Overview

`core/sessions/` owns the system-managed Session domain and the single canonical database `<data-dir>/sessions.db`. Chat, Agents, Projects, Channels, Recall, Statistics, and server orchestration address Sessions only through `ChatSessionManager`; no caller constructs storage paths or queries the database directly. Legacy JSONL artifacts are accepted only by the explicit offline converter under `scripts/converters/` and are rejected at Runtime startup.

## Terms

Core terms (Session, Agent, Run, Project) live in `.vorch/GLOSSARY.md`.

### Session generation
**Definition:** One immutable incarnation of a public `(project_id, agent_id, session_id)` address, identified internally by a unique generation id and surrogate key. Archiving ends the live generation; recreating the same public address starts a new generation without colliding with or inheriting derived state from the archived one.
**Not:** The public Session id or its append revision.

### Continuation journal
**Definition:** Ordered internal recovery records stored in `continuation_records` while visible work is active or interrupted. Chat owns record semantics; Sessions owns transactionality and lifecycle.
**Not:** Canonical Message history or Run events.

### Prompt-cache affinity id
**Definition:** An opaque per-Session lineage value Chat passes separately from Session identity; Provider adapters decide whether their wire can use it.
**Not:** A Session id or generation id. Compaction rotates it to break prior prompt prefixes.

## Storage Contract

- `sessions` stores one row per generation. A partial unique index permits exactly one live row per public address while retaining any number of archived generations. `messages` and `continuation_records` reference the immutable surrogate key, so Agent renames and Session moves update one row rather than every record.
- Message order is the dense integer `seq`; duplicate public Message ids are allowed because Compaction may persist the same checkpoint object more than once. `message_id`, role, and timestamp are indexed projections and must match the validated `ChatMessage` JSON payload.
- Normal appends are transactional and update message count, last Message projections, history revision, and state revision in the same commit. Metadata and activity are JSON objects; generated completion columns make Session-list badge reads indexed and consistent with activity JSON.
- SQLite opens with foreign keys, `synchronous=FULL`, a bounded busy timeout, application id, schema version, startup integrity checks, one serialized writer, and a bounded read pool. `required_journal_mode()` selects WAL only on SQLite builds without the WAL-reset corruption bug; affected builds use rollback-journal mode and an existing WAL database is converted on writer open. Reader connections are query-only and open only after the writer has reconciled schema; close wakes blocked readers. Runtime SQLite failures cross the domain as Session storage errors, not raw `sqlite3` exceptions.
- `SCHEMA_SQL` is the declarative source of truth. Writer open accepts `SCHEMA_CONVERSION_FLOOR <= user_version <= SCHEMA_VERSION`, refuses newer databases, verifies identity and integrity, and transactionally reconciles missing additive columns/objects before readers open. `ADD COLUMN` uses the complete declared column expression so checks and references cannot disappear; a required column without a default is not additive. Primary-key, existing-column contract, table-constraint, generated-column, table-option, and unique-index definition changes are destructive: raise `SCHEMA_CONVERSION_FLOOR` and use an offline converter rather than teaching startup to rewrite data. Readers require exactly `SCHEMA_VERSION` after reconciliation.
- Startup rejects an incomplete conversion marker, any coexistence of `sessions.db` with legacy JSONL, and both live-only and archive-only legacy JSONL. Initial schema creation is transactional; a failed first creation removes its partial database artifacts.
- `scripts/converters/jsonl_to_sqlite.py` is the only JSONL import path. It inventories live and archived sources, tolerates only an incomplete final JSONL record, fingerprints source bytes, imports into staging, semantically compares every Session before publish, checkpoints when WAL is active, atomically publishes, and keeps a durable resumable relocation manifest. It never runs as a startup migration.
- `scripts/converters/session_db.py` owns offline `verify`, `inspect`, `restore`, `backup`, and `compact` operations. Verification accepts the same schema-version bounds as writer open. Backups use SQLite's backup API; copying `sessions.db` alone while WAL is active is not a supported backup.

## Interfaces

- `ChatSession` is a path-free handle: `append`/`append_many`, `load`, `load_active`, continuation operations, notes, Skill activation state, and `load_since(cursor)`. Async counterparts execute through the bounded Session I/O pool.
- `SessionReadCursor` carries generation id, append revision, next sequence, count, and the prior Message id anchor. A cursor from the same generation returns only appended records after its prefix; a generation change, invalid sequence, or mismatched anchor returns `None` so disposable consumers rebuild.
- `ChatSessionManager` owns create/get/get-or-create/exists/list/archive/restore/delete/move/fork and identity/project lifecycle operations. `get_or_create` is one database transaction. Archive is asynchronous at the public boundary, runs off the Event Loop, and holds the Session write lock.
- `write_lock(address)` is context-reentrant for a holder and child tasks. It protects ordered Chat boundaries and whole move/fork/archive operations; Provider latency and other unbounded work stay outside it. Locks are manager-owned and released with manager lifetime.
- `mutate_metadata` and `mutate_metadata_with_previous` perform one transactional read-modify-write. Domain code must use them for partial updates; `set_metadata` is reserved for complete replacement and exact compensation snapshots. Completion activity uses the equivalent atomic mutation path.
- `list_with_metadata`, `list_completion_activity`, and `bookend_timestamps` use set-oriented or direct SQL reads rather than loading complete histories. `history_version(address)` returns generation plus revision for derived projections; comparing revision alone is insufficient.
- `fork` copies canonical history into a new generation without continuation or completion activity and records provenance. `move` retargets one live generation. Identity rename retargets live global Sessions only; archived history keeps its historical address.
- Archive and restore are generation-aware. A new live generation may reuse an archived address; restore fails while that address already has a live generation and otherwise restores the newest archived generation.

## Cross-Domain Rules

- Chat owns `ChatMessage`, request assembly, Run execution, Compaction behavior, and continuation record meaning. Sessions owns canonical persistence, ordering, metadata transactionality, validation, and storage lifecycle.
- Agents and Projects must share the Runtime Session manager when started by Runtime. Their standalone stores may lazily own one and must close owned resources. Agent create/current-Session repair deletes a newly created Session if later file persistence fails; rename restore retargets Sessions back; Agent and Project deletion move filesystem state first and compensate it if Session archiving fails.
- Recall and Statistics are disposable projections. Both include the Session generation in freshness state. Statistics appends through `load_since`; ordinary Message appends must not rebuild a complete Session projection.
- Skill activation cache is synchronized from the supplied active history snapshot when one is provided and otherwise preserves explicit in-memory registrations; a committed Compaction checkpoint resets the epoch.
- Callback failures after committed title or completion mutations are logged and isolated so callers never receive a false failure for state that already persisted.

## Constraints & Gotchas

- Never add uniqueness on Message ids without changing the Compaction contract and auditing imported data; sequence is canonical identity inside a generation.
- Never compare only public Session address plus history revision for derived freshness; archive/recreate can repeat the same revision.
- Never implement metadata as `get_metadata` followed by `set_metadata` for a partial change; concurrent fields will be lost.
- SQLite serializes database writes, not a read-modify-write split across transactions. Domain mutation callbacks must remain deterministic, side-effect free, and JSON-serializable.
- Never force WAL directly. Canonical and disposable Session-derived databases share `required_journal_mode()` so a vulnerable SQLite build cannot reintroduce the WAL-reset race.
- Existing JSONL names belong only in converter code and migration documentation. Canonical Runtime code and Recall use SQLite-backed Session APIs.
