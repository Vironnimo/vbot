# Archive

Deleted Identity Agents, Projects and Sessions as restorable archive entries: archive, restore, permanent deletion (purge) and startup recovery.

## Overview

`core/archive/` (`ArchiveService`, Runtime accessor `runtime.archive`) owns the archive policies: the payload layout, the guards around each operation, restore checks, purges, startup recovery and the owner log lines. Runtime bootstrap builds it from an `ArchiveServices` dataclass after `AutomationReferences` (`core/runtime/_bootstrap.py::_archive_services`), the same pattern as `AgentRenameServices`. It composes primitives of the existing owners and duplicates none:

- Sessions keep the entry rows beside the Sessions they hold: `SessionArchiveLedger` (`ChatSessionManager.archive_ledger`, `core/sessions/archive_ledger.py`), so Session states and entry membership change in one transaction (`sessions.md`).
- `AgentStore` and `ProjectStore` move their own files into a payload and back (`archive_files`, `restore_files`, `inspect_archived`, `restore_target_problem`; `agent.md`, `projects.md`).
- Run admission guards (`runs.md`), automation references (`automation.md`), Terminals, Recall eviction, the usage import and the Skill and Team caches arrive as injected objects and callables.

Every delete path archives: `agent.delete` -> `archive_agent`, `project.rm` -> `archive_project`, `session.delete` -> `archive_session`. An Extension's `archive_group` and core's cleanup of removed Extensions archive owner-managed Sessions as `owner_group` entries inside the Sessions domain (`ChatSessionManager.archive_temporary_group`, `sessions/owned-execution.md`). The RPC handlers keep only surface work: the last-Agent check, error mapping, the Project Agent landing after a Session archive, and `resource_changed` publishing (`server.md`).

Restore, purge, list and show exist only as the Python API so far: no RPC, CLI or WebUI surface reaches them, and no automatic retention purge runs.

## Terms

Core terms (Agent, Project, Session, Workspace) live in `.vorch/GLOSSARY.md`.

### Archive entry
One archived unit - an Identity Agent, a Project, one Session, an Extension group's Sessions, or legacy archive files - with its member Sessions and payload trees, restored or deleted permanently as a whole. Public id `arc_` plus 12 base32 characters; the integer `entry_key` never leaves the Session domain's callers.

### Payload
The files of an entry under `<data-dir>/archive/`, recorded per tree in `archive_entry_trees` with a role (`agent`, `workspace`, `project`, `files`) and the `source_path` it came from.

### Purge
The permanent deletion of an entry: its member Sessions, its recorded payload trees, then the entry row. Not the same as deleting a live resource, which archives it.

## Data Model

- Tables live in `sessions.db` (`core/sessions/schema.py`): `archive_entries` (one row per entry: `entry_id`, `kind`, `state`, `subject_id`, scope `project_id`/`agent_id` (`''` without one), `owner_name` for `owner_group`, `archived_at`, `retention_start`, `origin`, `cleanup_pending`, open `facts_json`), `archive_entry_sessions` (each archived Session generation belongs to exactly one entry; both foreign keys cascade) and `archive_entry_trees` (data-dir relative POSIX `path` as key, role, `source_path`).
- Kinds (open vocabulary, no CHECK): `agent` (the tree `agents/<id>/` and the Agent's live global Sessions), `project` (the Anchor `projects/<id>/` and every live Session of the Project; the repository is never touched), `session` (one Session; backfill: one archived batch of one scope), `owner_group` (the Sessions of one Extension owner group; listed and purgeable, not restorable: only the owner can resume work), `files` (legacy archive content only; purgeable, not restorable).
- States (open vocabulary; every transition is a compare-and-set in one transaction, a mismatch raises `ArchiveEntryBusyError`): `archiving` (files may be moving, Sessions still live) -> `archived` (resting state; `cleanup_pending = 1` marks unfinished post-commit work) -> `restoring` (files may be moving back, Sessions still archived) -> `restored` (Sessions live, follow-up pending) -> row deleted. `archived` -> `purging` (never restorable, resumable) -> row deleted. A failed archive deletes its `archiving` row; a failed restore returns to `archived`.
- `origin`: `operation`, `backfill` (the migration) or `recovered` (an orphan payload adopted at startup). `retention_start` is the archive time, or the adoption time for `backfill` and `recovered` entries.
- Facts (`facts_json`, writers merge and keep unknown keys): `name` and `payload_format` (agent, project: the integer format version, `"older"` or `"newer"`; decides restorability without reading payloads), `roster_index`, `grants` (`[{agent_id, index}]`, recorded before any config is written and merged on re-run), `workspace` (`{path, external, moved}`) and `root_project_id` (agent), `cwd` and `unrooted_agents` (`[{agent_id, workspace_before, workspace_reset}]`, written before each unroot) (project), `reason` (owner_group: `extension` or `extension_removed`), `restore_plan` (`{target_id, strip_channel_keys, workspace_destination?}` while `restoring`/`restored`), `backfill` (`{match: "timestamp"|"none", tree_mtime}`).

## Payload layout and data snapshots

- A new entry's payload is `archive/entries/<entry_id>/agent` or `.../project`. Legacy payloads keep their legacy paths; their trees are recorded, so no code branches on the layout. Archiving the same id again creates another entry with its own payload; no code path replaces or deletes another entry's payload.
- An Identity Agent's Workspace outside its directory stays where it is (`facts.workspace.external`), is re-attached on restore, and is never deleted; when it is gone, the restore uses the default Workspace (warning `external_workspace_missing`). A legacy archive's moved Workspace tree (role `workspace`) returns to its folder, or becomes the default Workspace when that folder is in use (warning `workspace_path_taken`; a blocker when both are in use).
- A purge deletes exactly the recorded trees with `remove_tree(path, within=archive/)` (`core/utils/tree_move.py`: never follows links, refuses anything outside `archive/`), then removes empty parents up to, not including, `archive/`. Nothing else under `archive/` is ever claimed or deleted.
- `archive/` is outside the JSON Document Contract and outside data snapshots (`settings.md` -> Outside the contract); the ledger rows are inside, because they live in `sessions.db`. A whole-snapshot restore can therefore leave rows and payloads out of step: a row whose payload is gone lists as not restorable (`payload_missing`) and a purge still deletes its Sessions; an `archive/entries/arc_*` directory without a row is adopted at the next start as a `recovered` entry without Sessions, its kind read from its `agent/` or `project/` child.
- Limitation: Workspace files are never in a snapshot. After such a restore an Agent can be live again with its snapshot `agent.json` while its tree content sits in a recovered payload; restoring that payload then needs another target id.

## Interfaces

- Archive: `archive_agent(agent_id)`, `archive_project(project_id, copy_identity_files=)`, `archive_session(address)` return `AgentArchiveOutcome` (entry id, Session count, changed delegation holders), `ProjectArchiveOutcome` (entry id, unrooted Agents, copied and backed-up identity files) and `SessionArchiveOutcome` (entry id, `next_session_id` and `was_current` for an Identity Agent's current Session). Each refuses with `ArchiveSubjectInUseError` (`references` as `kind:id` labels; for a Project or Session also `details` as `{kind, id, name}`) while a Channel (Agent only) or a live automation targets the subject, and with `RunAdmissionBlockedError` while it has active or queued Runs.
- Restore: `restore_check(entry_id, target_id=)` returns every blocker and warning at once and changes nothing; `restore(entry_id, target_id=)` raises `ArchiveRestoreConflictError` when only taken ids or addresses block (`agent_id_taken`, `project_id_taken`, `session_address_taken`: another target id avoids them), `ArchiveEntryBusyError` while another operation holds the entry, and `ArchiveNotRestorableError` for every other blocker (`kind_not_restorable`, `payload_missing`, `payload_invalid`, `older_format`, `newer_format`, `invalid_target_id`, `project_cwd_claimed`, `scope_missing` with the newest entry that holds the missing Agent or Project, `workspace_path_taken`). Warnings: `grant_target_missing`, `root_project_missing`, `external_workspace_missing`, `workspace_path_taken`. Under a new target id the Sessions lose their Channel routing keys, and grants, roots and Sub-Agent parent links follow the new id.
- Purge: `purge(entry_ids, all_matching=, reason=, actor=)` returns `PurgeOutcome(purged, pending)`. An unknown id refuses the whole call with `ArchiveEntryNotFoundError` and a non-`archived`/`purging` entry with `ArchiveEntryBusyError`, before anything is deleted.
- Reads: `list(filters, cursor=, limit=)` pages entries newest first as `ArchiveListing` (label, `restorable` and `not_restorable_reason` from cheap facts only); `show(entry_id)` adds the first member Sessions, the payload state (`present`, `missing`, `none`) and the full restore check.
- `recover()` (blocking, startup only; see Recovery) and `add_changed_callback(callback)`, which registers on the ledger: the service's operations, `ChatSessionManager.archive`, `archive_temporary_group` and `delete_temporary_group` call it after entries changed, and the server bridges it to `resource_changed(kind="archive")` (`server/_app_lifecycle.py`, `server/events-and-reconnect.md`).
- Errors live in `core/archive/errors.py`; `ArchiveEntryError`, `ArchiveEntryNotFoundError` and `ArchiveEntryBusyError` come from `core.sessions`.

## Conventions

- Locking: the caller holds the automation reference lock (`AutomationReferences.lock`, the server's `agent_delete_lock`) across an archive or restore, so the reference check and the change it admits are one step; the lock is not reentrant, so the service never takes it. The service takes the Run admission guards itself (`agent_admission_guard`, `project_admission_guard`, `session_admission_guard` over the members' restored addresses).
- Blocking work runs on the Session database's pool (`ChatSessionManager.run_async`); purges run on the one-worker `archive` pool (`core/archive/_purge.py::PURGE_WORKERS`), so two purges never overlap.
- Snapshot barrier: Agent and Project archive and restore move snapshot documents and change the anchor, so they run inside `compound_mutation()`; the stores' `archive_files`/`restore_files` enter it too (nesting is allowed). A Session archive and the current-pointer re-aim share `compound_mutation_async()`. A purge holds none: each Session delete is its own transaction and a `purging` entry repairs itself (`database.md` -> Crash equivalence).
- Archive order (`_operations.py`): `ledger.begin` records the entry `archiving` with the tree it will create; the store moves the files; `commit_scope` archives the live Sessions and marks the entry `archived` in one transaction. A failure before that commit moves the files back and deletes the entry; when a tree did not go back, the entry stays `archiving` for recovery. An Agent archive then records and removes its delegation grants, drops the id from the roster and clears `cleanup_pending`; Recall eviction and Skill invalidation follow. A Project archive records and performs each unroot first (an Agent whose Workspace was elsewhere gets its default Workspace, optionally with copied identity files) and undoes them on failure.
- Restore order (`_restore.py`): check, `begin_restore` (`restoring` with `restore_plan`), the store rewrites the payload's `agent.json`/`project.json` to the target id (unknown fields kept) and moves it back, `commit_restore` makes the members live (`restored`); then the idempotent follow-up places the roster position, re-adds grants, re-roots unrooted Agents, retargets Sub-Agent parent links under a new id, prunes empty payload directories and deletes the entry.
- Purge order (`_purge.py`): bring the usage ledger up to date first (a failed import purges nothing: every entry is pending `usage_import_failed`); `begin_purge`; delete the members newest key first, one `delete_session` transaction each, so a fork inside the entry goes before its source and descendants elsewhere, archived ones included, receive materialized copies (`sessions/lineage.md` -> Delete); remove the recorded trees; `finish_purge` deletes the row (and an `owner_group` title once no Session of the group remains). A failed entry stays `purging`, is reported pending with one WARNING per failure kind, and a later purge continues it. Usage totals stay; attachment blobs the purged Sessions referenced stay too (`attachments.md`: no reference counting).
- Owner log lines (`vbot.archive`, INFO): `Archive entry created (entry= kind= subject= sessions= ... actor=)`, `Archive entry restored (...)`, `Archive entry purged (... reason= actor=)`; recovery logs WARNING or INFO per settled entry.

## Recovery

`recover()` runs in bootstrap after a pending Agent rename settled and before Channels, Cron and Runs start, in safe startup modes too (`_recovery.py`, `runtime.md`). Every step is idempotent; an entry whose recovery fails is logged and retried at the next start.

| Found | Action |
|---|---|
| archived Sessions without membership (an older vBot after a downgrade) | adopted as `session`/`owner_group` entries, WARNING |
| `archive/entries/arc_*` without a row | adopted as a `recovered` entry; an empty one is removed; WARNING |
| `archiving` | trees move back to `source_path` when it is free, recorded unroots are undone, the row is deleted |
| `archived` with `cleanup_pending` | the Agent cleanup runs again |
| `restoring` | trees return from the target into the payload, then `archived` again |
| `restored` | the follow-up runs and deletes the row |
| `purging` | left; the next purge of the entry continues it |

## Migration of earlier archives

The Session database migration `sessions.0001_archive_entries` (and the Generation 1 converter, which calls the same adoption) turned archives written before entries existed into `backfill` entries: legacy trees under `archive/` keep their paths, pair with their archived Sessions by time, and pre-Generation-1 payloads record `payload_format: "older"` (listed and purgeable, never restored). Detail: `archive/legacy-adoption.md`.

## Constraints & Gotchas

- Never replace, move or delete another entry's payload, and never claim a directory under `archive/` that no entry records except `archive/entries/arc_*`: a user's own folder there must survive every archive operation.
- An archive must not leave Sessions archived without an entry: every path that archives Sessions does it through the ledger (`commit_scope`, `ChatSessionManager.archive`, `archive_temporary_group`).
- Restore validates the payload document through its owner before moving it into a contract location; never move an archived `agent.json`/`project.json` back unread.
- Do not take the automation reference lock inside the service, and do not start a purge inside a compound mutation: a long purge would starve data snapshot captures.

Tests: `tests/core/archive/test_archive_service.py` (archive, restore, purge, reads through the Python API), `test_archive_recovery.py`, `tests/core/sessions/test_archive_ledger.py`, `test_archive_backfill.py`, `test_store_query_plans.py`, `tests/core/agents/test_agents_archive.py`, `tests/core/projects/test_store.py`, `tests/core/utils/test_tree_move.py`, the delete tests of `tests/server/rpc/`, and `tests/scripts/converters/persistence_generation_1/test_sessions.py`.

## References

Read these only when your task matches - not by default.

- Changing the archive migration, the converter's archive step or the startup adoption of archived Sessions without an entry, or reasoning about `backfill` entries and legacy archive layouts -> `archive/legacy-adoption.md`
