# Archive

Deleted Identity Agents, Projects and Sessions as restorable archive entries: archive, restore, permanent deletion (purge), retention and startup recovery.

## Overview

`core/archive/` (`ArchiveService`, Runtime accessor `runtime.archive`) owns the archive policies: the payload layout, the guards around each operation, restore checks, purges, the retention sweep, startup recovery and the owner log lines. Runtime bootstrap builds it from an `ArchiveServices` dataclass after `AutomationReferences` (`core/runtime/_bootstrap.py::_archive_services`), the same pattern as `AgentRenameServices`. It composes primitives of the existing owners and duplicates none:

- Sessions keep the entry rows beside the Sessions they hold: `SessionArchiveLedger` (`ChatSessionManager.archive_ledger`, `core/sessions/archive_ledger.py`), so Session states and entry membership change in one transaction (`sessions.md`).
- `AgentStore` and `ProjectStore` move their own files into a payload and back (`archive_files`, `restore_files`, `inspect_archived`, `restore_target_problem`; `agent.md`, `projects.md`).
- Run admission guards (`runs.md`), automation references (`automation.md`), Terminals, shell commands and Scratch Areas (`tools/shell.md`, `tools/scratch.md`), Recall eviction, the usage import and the Skill and Team caches arrive as injected objects and callables.

Every delete path archives: `agent.delete` -> `archive_agent`, `project.rm` -> `archive_project`, `session.delete` -> `archive_session`. An Extension's `archive_group` and core's cleanup of removed Extensions archive owner-managed Sessions as `owner_group` entries inside the Sessions domain (`ChatSessionManager.archive_temporary_group`, `sessions/owned-execution.md`) and then release their root Scratch Areas through `release_temporary_group` (`agent.md`). The RPC handlers keep only surface work: the last-Agent check, error mapping, the Project Agent landing after a Session archive, and `resource_changed` publishing (`server.md`).

A delete with `permanent: true` archives the same way, then purges exactly the entry it created, after the archive released the reference lock; there is no separate erase path. The archive has succeeded by then, so the purge never fails the delete: the result names the entry and says whether it was purged (`archive/purge-and-retention.md` -> Permanent delete).

`archive.list`, `archive.show`, `archive.restore` and `archive.purge` (`server/rpc/archive_methods.py`) reach the service; the CLI `vbot archive` area calls them (`cli/archive-commands.md`), and so does the WebUI's Settings -> Archive page, while its delete dialogs also offer `permanent: true` (`webui/archive.md`).

## Terms

Core terms (Agent, Project, Session, Workspace, Archive Entry) live in `.vorch/GLOSSARY.md`.

### Payload
The files an archive entry holds under the data directory's `archive/` folder, recorded tree by tree with where each came from (Data Model; Payload layout and data snapshots).

### Purge
The permanent deletion of an entry: its member Sessions, its recorded payload trees, then the entry row; manual, as part of a permanent delete, or by the retention sweep. Not the same as deleting a live resource, which archives it.

### Retention period
How long an archived entry rests before the retention sweep purges it, counted from its retention start; it may be unlimited (Retention; `archive/purge-and-retention.md`).

## Data Model

- Ids: the public `entry_id` is `arc_` plus 12 base32 characters; the integer `entry_key` never leaves the Session domain's callers.
- Tables live in `sessions.db` (`core/sessions/schema.py`): `archive_entries` (one row per entry: `entry_id`, `kind`, `state`, `subject_id`, scope `project_id`/`agent_id` (`''` without one), `owner_name` for `owner_group`, `archived_at`, `retention_start`, `origin`, `cleanup_pending`, open `facts_json`), `archive_entry_sessions` (each archived Session generation belongs to exactly one entry; both foreign keys cascade) and `archive_entry_trees` (data-dir relative POSIX `path` as key, role `agent`, `workspace`, `project` or `files`, `source_path`).
- Kinds (open vocabulary, no CHECK): `agent` (the tree `agents/<id>/` and the Agent's live global Sessions), `project` (the Anchor `projects/<id>/` and every live Session of the Project; the repository is never touched), `session` (one Session with its live review forks, `sessions.md` -> Terms -> Review fork; backfill: one archived batch of one scope), `owner_group` (the Sessions of one Extension owner group; listed and purgeable, not restorable: only the owner can resume work), `files` (legacy archive content only; purgeable, not restorable).
- States (open vocabulary; every transition is a compare-and-set in one transaction, a mismatch raises `ArchiveEntryBusyError`): `archiving` (files may be moving, Sessions still live) -> `archived` (resting state; `cleanup_pending = 1` marks unfinished post-commit work) -> `restoring` (files may be moving back, Sessions still archived) -> `restored` (Sessions live, follow-up pending) -> row deleted. `archived` -> `purging` (never restorable, resumable) -> row deleted. A failed archive deletes its `archiving` row; a failed restore returns to `archived`.
- `origin`: `operation`, `backfill` (the migration) or `recovered` (an orphan payload adopted at startup). `retention_start` is the archive time, or the adoption time for `backfill` and `recovered` entries. A `recovered` entry is never due: nothing records how old its payload is.
- Facts (`facts_json`): writers merge and keep unknown keys; `payload_format` decides restorability without reading payloads. Catalog: `archive/operations.md` -> Entry facts.

## Payload layout and data snapshots

- A new entry's payload is `archive/entries/<entry_id>/agent` or `.../project`. Legacy payloads keep their legacy paths; their trees are recorded, so no code branches on the layout. Archiving the same id again creates another entry with its own payload; no code path replaces or deletes another entry's payload.
- An Identity Agent's Workspace outside its own directory stays where it is, is re-attached on restore and is never deleted (`archive/operations.md` -> Workspace handling).
- A purge deletes exactly the recorded trees with `remove_tree(path, within=archive/)` (`core/utils/tree_move.py`: never follows links, refuses anything outside `archive/`). Nothing else under `archive/` is ever claimed or deleted.
- `archive/` is outside the JSON Document Contract and outside data snapshots (`json-documents.md` -> Outside the contract); the ledger rows are inside, because they live in `sessions.db`. A whole-snapshot restore can therefore leave rows and payloads out of step: a row whose payload is gone lists as not restorable (`payload_missing`) and a purge still deletes its Sessions; an `archive/entries/arc_*` directory without a row is adopted at the next start as a `recovered` entry without Sessions, its kind read from its `agent/` or `project/` child.

## Interfaces

- Archive: `archive_agent(agent_id)`, `archive_project(project_id, copy_identity_files=)` and `archive_session(address)` return an outcome naming the entry. Each refuses with `ArchiveSubjectInUseError` while a Channel (Agent only) or a live automation targets the subject, and with `RunAdmissionBlockedError` while it has active or queued Runs. `archive_session` takes the Session's live review forks along, and the guards cover them too, so a review still running refuses the archive. Outcomes: `archive/operations.md` -> Archive outcomes.
- Restore: `restore_check(entry_id, target_id=)` returns every blocker and warning at once and changes nothing; `restore(entry_id, target_id=)` raises `ArchiveRestoreConflictError` when only taken ids or addresses block (another target id avoids them), `ArchiveEntryBusyError` while another operation holds the entry and `ArchiveNotRestorableError` for every other blocker (catalog: `archive/operations.md` -> Restore checks and outcomes).
- Purge: `purge(entry_ids, all_matching=, reason=, actor=)` returns `PurgeOutcome(purged, pending, skipped, gone, kept)` and refuses the whole call before deleting anything for an unknown id or an entry not `archived`/`purging`; `all_matching` never deletes an entry that may hold the user's folders (`archive/purge-and-retention.md`).
- Retention: `start()` (inside the serving Event Loop), `stop()`, `aclose()` and `retention_changed()` (any thread); see Retention.
- Reads: `list(filters, cursor=, limit=)` pages entries newest first and `show(entry_id, session_limit=)` adds members, payload state and the full restore check (`archive/operations.md` -> Reads).
- `recover()` (blocking, startup only; see Recovery) and `add_changed_callback(callback)`, which registers on the ledger: the service's operations, `ChatSessionManager.archive`, `archive_temporary_group` and `delete_temporary_group` call it after entries changed, and the server bridges it to `resource_changed(kind="archive")` (`server/_app_lifecycle.py`, `server/events-and-reconnect.md`).
- Errors live in `core/archive/errors.py`; `ArchiveEntryError`, `ArchiveEntryNotFoundError`, `ArchiveEntryBusyError` and `ArchiveMembersManagedError` (the ledger's `commit_restore` refusal, which the service maps to `owner_managed`) come from `core.sessions`.

## Conventions

- Archives carry review members along: Agent and Project archives include every live review follower of their Sessions (`SessionArchiveLedger.scope_addresses`), and reviews keep their original owner on restore; a restore refuses the whole entry until every member's Agent/Project scope exists (`archive/operations.md` -> Review members).
- Payload presence uses strict filesystem checks. An inaccessible payload stays visible with `payload_unavailable` (detail `files: unavailable`) and blocks restore until access returns; it is never reported as missing. A failed restore whose payload cannot be checked remains pending for recovery.
- Locking: the caller holds the automation reference lock (`AutomationReferences.lock`) across an archive or restore, so the reference check and the change it admits are one step; the lock is not reentrant, so the service never takes it. `archive.restore` and the archive step of `agent.delete` also hold the Agent lifecycle mutation lock, because a restore creates an Agent, Project or Session id like Agent create and rename. A purge needs no reference lock: it only deletes, and the entry state admits it. The service takes the Run admission guards itself (`archive/operations.md` -> Locking helpers).
- Blocking work runs on the Session database's pool (`ChatSessionManager.run_async`); purges and the retention sweep's reads run on the one-worker `archive` pool (`core/archive/_purge.py::PURGE_WORKERS`), so two purges never overlap.
- A successful Session, Agent or Project archive removes the Scratch Areas of its root Sessions after closing their commands and Terminals, then closes its Sessions' Provider transports and Portals (`close_session_transports`) and removes their Agent Secrets (`remove_session_secrets`, never restored), best effort, while its admission guard is held; a failed archive retains scratch files, and Scratch Areas never enter payloads (`tools/scratch.md` -> Retention and archive).
- Snapshot barrier: Agent and Project archive and restore move snapshot documents and change the anchor, so they run inside `compound_mutation()`; the stores' `archive_files`/`restore_files` enter it too (nesting is allowed). A Session archive and the current-pointer re-aim share `compound_mutation_async()`. A purge holds none: each Session delete is its own transaction, the removed trees under `archive/` are outside snapshots, a `purging` entry repairs itself (`database/snapshot-consistency.md` -> Crash equivalence), and holding the barrier around a large tree removal would starve captures.
- Archive and restore run in a fixed order whose failures leave a state recovery can settle (`archive/operations.md` -> Archive order, Restore order); purges delete member Sessions newest key first through `delete_session`, so descendants elsewhere receive materialized copies (`archive/purge-and-retention.md` -> Purge order).
- Owner log lines (`vbot.archive`, INFO): one line per created, restored and purged entry, carrying ids, never titles or names; failures log once per entry and failure kind with an INFO once they stop (`LoggedConditions`).

## Retention

Entries `archived` longer than the retention period (`archive.retention_days`: 1-3650 or `null`, default 30, applied live) are purged by a background sweep. The archive reads the setting strictly on every use: a degraded `settings.json` never falls back to the default period, because a default could delete entries the user meant to keep longer; the sweep then purges nothing for retention and `list` reports `retention_unknown`. `purge_at` is computed on read, so a changed period applies at once; `recovered` and `files` entries and entries that may hold the user's folders are never due. The Runtime starts the sweep after startup, never in a safe startup mode; shutdown leaves an entry `purging` for the next start's sweep. Each sweep first deletes expired Ephemeral Sessions permanently (`archive/purge-and-retention.md`).

## Recovery

`recover()` runs in bootstrap after a pending Agent rename settled and before Channels, Cron and Runs start, in safe startup modes too (`_recovery.py`, `runtime.md`). Every step is idempotent; a step or an entry whose recovery fails is logged and retried at the next start: neither startup nor safe mode fails because of the archive folder. States, actions and provenance limits: `archive/recovery.md`.

## Migration of earlier archives

The Session database migration `sessions.0001_archive_entries` turned archives written before entries existed into `backfill` entries (`archive/legacy-adoption.md`).

## Constraints & Gotchas

- Never replace, move or delete another entry's payload, and never claim a directory under `archive/` that no entry records except `archive/entries/arc_*`: a user's own folder there must survive every archive operation.
- Startup recovery never moves live files back into a payload, and never strips or records grants while a live Agent holds the archived id. A payload, source or folder that cannot be checked is unavailable, never missing.
- An archive must not leave Sessions archived without an entry: every path that archives Sessions does it through the ledger (`commit_scope`, `ChatSessionManager.archive`, `archive_temporary_group`).
- Restore validates the payload document through its owner before moving it into a contract location; never move an archived `agent.json`/`project.json` back unread.
- Do not take the automation reference lock inside the service, and do not start a purge inside a compound mutation: a long purge would starve data snapshot captures.

Tests: `tests/core/archive/` and `tests/core/sessions/test_archive_ledger.py`; per-feature tests are named in the References files.

## References

Read these only when your task matches - not by default.

- Changing an archive or restore operation: its order, outcomes, entry facts, restore blockers or warnings, Workspace handling, review members or the list/show reads -> `archive/operations.md`
- Changing purge, permanent delete, the retention setting, due computation or the retention sweep -> `archive/purge-and-retention.md`
- Changing startup recovery, interrupted archive or restore states, or restore provenance warnings -> `archive/recovery.md`
- Changing the archive migration or the startup adoption of archived Sessions without an entry, or reasoning about `backfill` entries and legacy archive layouts -> `archive/legacy-adoption.md`
- Changing the archive RPCs (`archive.list`, `archive.show`, `archive.restore`, `archive.purge`), their error codes and `data`, or the archive fields of the `agent.delete`, `project.rm` and `session.delete` results -> `archive/rpcs.md`
