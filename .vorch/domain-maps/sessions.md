# Sessions

Canonical Session history, metadata, prompt state, completion activity, Stream drafts, and SQLite persistence.

## Overview

`core/sessions/` owns the system-managed Session domain and the single canonical database `<data-dir>/sessions.db`. Every other domain addresses Sessions only through `ChatSessionManager`; no caller constructs storage paths or queries the database directly.

History is stored once. A fork reads its source's entries through lineage instead of copying them, and a history edit supersedes the replaced tail in place instead of deleting it. Every history read therefore names one view: the current view (what the Session shows and sends to the Model) or the own audit (what the Session itself wrote). See Terms.

Data snapshots, quarantine, restore, recovery incidents and the data-store marker belong to the kernel (`database.md`); the Session store contributes only its spec hooks.

`store.py` opens the database through the kernel, and private `_store_*` modules receive its connection (module map: `sessions/storage.md`). Helpers never open their own runtime connection or commit: entries, metadata, revisions, prompt state, Stream drafts, receipts and FTS effects share the caller's transaction. Helpers return typed records or summary dicts rather than raw rows, so `ChatSessionManager` does not decode storage columns.

## Terms

Core terms (Session, Agent, Run, Project, Working Project) live in `.vorch/GLOSSARY.md`.

### Session generation
One incarnation of a public Session address: archiving ends the live generation, and recreating the address starts a new one that neither collides with nor inherits derived state from the archived one (Storage Contract). Not the public Session id or its history revision.

### Entry
One stored history item at a dense per-Session sequence number; every role is an entry, Tool results, Run summaries and history-edit markers included (`sessions/storage.md` -> Tables). Not an Archive Entry (GLOSSARY), although both expose an `entry_id`.

### Current view
What a Session shows and sends to the Model: its own non-superseded entries plus the entries it inherits through lineage (`sessions/lineage.md` -> Views). Not the own audit.

### Own audit
Every entry the Session wrote itself from its fork point on, superseded ones included; Statistics and the Session's own spend read it (`sessions/lineage.md` -> Views). Not the current view: inherited entries and materialized copies precede the fork point.

### Lineage and fork point
A fork's lineage names, per inherited range of history, the ancestor Session that stores it and how far that ancestor's history reached at fork time, always the storing Session directly; the fork point is where the fork's own entries start (`sessions/lineage.md` -> Fork). Not the direct fork parent, which only names the Session a fork was made from.

### Superseded entry
An entry a history edit replaced: it leaves its owner's current view but stays stored for the own audit and for forks made before the edit (`sessions/lineage.md` -> History edit).

### Materialized copy
A descendant's own copy of an entry it inherited from a Session being deleted, made before the delete (`sessions/lineage.md` -> Delete). Not the descendant's own history: copies precede its fork point.

### Review fork
A fork that reviews its source for a Reflection and then follows its source's lifecycle instead of outliving it (`sessions/lineage.md` -> Delete; `automation.md` -> Reflection). Not a user fork or a fork into another Agent or Project, which stays when its source goes.

### Stream draft
The streamed Model output of a running Run that no Assistant entry holds yet, kept only so restart recovery can turn it into the Run's `interrupted` Assistant entry (`sessions/runs.md` -> Stream drafts). Not history, a journal, or anything a request reads.

### Prompt state
The per-Session prompt inputs kept outside metadata: prompt pins, the seen-Skill set and the prompt-cache affinity id (`sessions/lists-and-metadata.md` -> Prompt state APIs; fork and move: `sessions/lineage.md` -> Fork). Not metadata: the metadata facade rejects their keys.

### Prompt-cache affinity id
An opaque per-Session cache-lineage value that Chat passes to Provider Adapters apart from Session identity; a routing hint, never a correctness boundary, so Compaction and history edits keep it. Not a Session id or generation id.

## Extension-owned execution

Temporary Session bindings, delivery receipts, Run execution owners and group titles live in dedicated relations outside open metadata; domain state belongs to the Extension. Exact address plus Session generation and registered owner establish authority; no fork or materialized copy carries a binding. Owner-managed Sessions reject ordinary archive, move and delete, and ordinary Agent/Project discovery must not report their synthetic `tmp_...` participant Agents (`sessions/owned-execution.md`).

## Storage Contract

- `sessions` stores one row per generation, with exactly one live row per public address and any number of archived generations. Entries, Runs, lineage, prompt state and owned relations reference the immutable surrogate `session_key`, so Agent renames and Session moves update one row.
- `finish_run` returns the committed Chat History cursor; only then does Run management publish a persistence acknowledgement. Startup settles Runs left running as `interrupted` (`process_restart`) without replaying Model or Tool work, and Sessions writes their terminal line, which the Run manager never sees (`sessions/runs.md`).
- Missing live-address reads raise the typed `SessionNotFoundError` (a `ChatSessionError` subtype), allowing disposable consumers to distinguish a concurrent delete/archive race from storage failure.
- Channel conversation pointers, conversation kinds and group participants are Channel state in `channels.db`, not Session metadata (Channel metadata keys: `channels.md`).

## Interfaces

- `ChatSessionManager` owns Session lifecycle operations and lookups (`sessions/lists-and-metadata.md`). `ChatSessionManager.run_async(function, ...)` runs any blocking work that reads or writes Sessions on the Session database's own worker pool (`database.md`); callers outside the domain use it instead of `asyncio.to_thread` or a private pool, keeping only non-database work on their own pools.
- `ChatSession` is a path-free handle: `load` reads the own audit, `load_active` the current view, `load_since(cursor)` the delta after a cursor or `None` to force a rebuild (`sessions/history-api.md`). `get`/`get_async` capture the live generation in `ChatSession.generation_id` during the presence check; Chat carries it into generation-checked Run admission. Address-only handles have no generation expectation.
- `write_lock(address)` protects ordered Chat boundaries and whole move/fork/archive operations; Provider latency and other unbounded work stay outside it. It is context-reentrant (lease rules: `sessions/history-api.md` -> Write lock).
- `list_summaries_page` is the bounded accessor read model (one transaction, keyset cursor, total count); there is no full-metadata listing. Recall visibility is classified in SQL from the visibility mask, so callers never classify metadata in Python (`sessions/lists-and-metadata.md`).
- `archive(address)` archives one live Session with its idle review forks as a `session` archive entry; Agent and Project archives, every restore and every purge go by entry through `archive_ledger` (`SessionArchiveLedger`), which only the archive service drives (`archive.md`; Session side: `sessions/ownership.md`). Its tables live in `sessions.db` (`archive.md` -> Data Model), so an entry's row, membership and member states change in one transaction.

## Cross-Domain Rules

- Chat owns `ChatMessage`, request assembly, Run execution, Compaction behavior, and when Stream drafts are written. Sessions owns canonical persistence, ordering, views, metadata and prompt-state transactionality, validation, and storage lifecycle.
- Agents and Projects must share the Runtime Session manager when started by Runtime. Their standalone stores may lazily own one and must close owned resources. Changes that span Session rows and Agent or Project files compensate a failed half (`sessions/ownership.md` -> Paired changes). These pairs, the `/agent` takeover (Session move plus both current-Session pointers) and a Session archive (archive plus current-pointer re-aim) hold the snapshot barrier, so an online data snapshot never captures one half (`database.md` -> Snapshot consistency).
- Recall and Statistics are disposable projections. Both include the Session generation in freshness state. Recall reads the current view; Statistics appends each Session's own audit through `load_since`, and ordinary appends must not rebuild a complete Session projection.

## Constraints & Gotchas

- Public Message ids are not unique: `seq` is canonical identity inside a generation, and forks and materialized copies share ids across Sessions. Never add uniqueness on them without changing the Compaction contract and auditing imported data, and never build pagination or frozen snapshots on them alone: use generation plus internal sequence, keeping public-id input only where compatibility requires it.
- Read history only through a view: `_store_lineage.view_ranges` plus `ordered_rows` or `view_query`, or the `_store_history` helpers built on them. Filtering `entries` by `session_key` alone misses inherited entries and includes superseded ones.
- Delete a Session only through `_store_mutations.delete_session` (`delete`, `delete_temporary_group`). `session_lineage.ancestor_key` is `ON DELETE RESTRICT`, so a direct row delete fails while descendants inherit from it; `delete_session` materializes their copies first.
- Search uses two disposable FTS5 indexes over the entries some current view contains; a derived-index failure never blocks a canonical write. Membership changes need the FTS rule: forget the affected entries (`fts_forget`/`fts_forget_keys`) before the change that can end their membership, and re-index the same candidates (`fts_index_keys`) after it. See `sessions/search.md`.
- Store timestamps only in the canonical form (PROJECT.md -> Conventions -> Time); the store compares and orders them as text.
- Partial metadata changes use `mutate_metadata`/`mutate_metadata_with_previous`, never `get_metadata` followed by `set_metadata` (reserved for complete replacement and exact compensation snapshots), which loses concurrent fields: SQLite serializes writes, not a read-modify-write split across transactions. Mutation callbacks may run twice and must remain deterministic, side-effect free, and JSON-serializable. Never write prompt state through metadata (`sessions/lists-and-metadata.md` -> Metadata facade).
- Never compare only public Session address plus history revision for derived freshness; archive/recreate can repeat the same revision. Key on generation plus revision, and reread when `history_id` changes after a restore (`sessions/lists-and-metadata.md` -> Lists and batched reads).
- Stored Run membership is authoritative. Messages of a Run require that Run admitted, running and not inherited in this Session; a settled or inherited Run rejects new output and cannot be admitted again; duplicate completion is rejected.

## Agent-facing text

This domain owns the title System Prompt (`TITLE_SYSTEM_PROMPT` in `core/sessions/titles.py`). Before you change, add or delete a sentence of it, read `sessions/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Forks, moves, history edits, deletion, review-fork lifecycle, which entries a view holds -> `sessions/lineage.md`
- Message search, the FTS indexes, their upkeep, rebuild or status health -> `sessions/search.md`
- Temporary bindings, owned Runs, group titles, delivery, Note, Extension Tool or command receipts -> `sessions/owned-execution.md`
- Store modules, tables, indexes, named migrations, write budgets, snapshot facts, bootstrap, `vbot-docs` script -> `sessions/storage.md`
- Run admission, settlement or restart recovery, Tool call storage, Stream drafts, change statistics or completion activity -> `sessions/runs.md`
- History appends and reads, cursors, Chat History snapshot, Compaction commit, reply targets, deferred Notes, Skill cache, write lock -> `sessions/history-api.md`
- Session creation and lookup, lists, summaries, batched reads, metadata keys, prompt-state APIs, Recall visibility or Ephemeral Sessions -> `sessions/lists-and-metadata.md`
- Working Project binding, rename retargeting, Sub-Agent links, archive scope membership, restore provenance or paired Agent/Project changes -> `sessions/ownership.md`
- Session or group title generation, the title parser or title callbacks -> `sessions/titles.md`
