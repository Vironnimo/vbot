# Archive startup recovery

Task-gated detail for `archive.md` -> Recovery: what `recover()` finds and does at startup, interrupted archive and restore states, and the provenance limits of restores. Source: `core/archive/_recovery.py`, `_restore.py`. Tests: `tests/core/archive/test_archive_recovery.py`, `test_archive_service.py`. Adoption of archived Sessions without an entry: `archive/legacy-adoption.md`.

## Order and failure handling

When `recover()` runs and its idempotent retry rule: `archive.md` -> Recovery. A folder under `archive/` that cannot be read is skipped with a WARNING. A payload, source or folder that cannot be checked counts as such a failure, never as missing, so recovery neither moves nor removes anything on that answer. Order: archived Sessions without membership, then the unsettled entries, then payloads without a row (after settling, because an undone archive may leave one). Recovery never moves files that are live again back into a payload: they may hold new data. It logs WARNING or INFO per settled entry.

Orphan payload classification never follows a symbolic link or Windows junction at `agent/` or `project/`: a container holding only such links becomes a non-restorable `files` entry, without reading their targets.

## Actions per state

| Found | Action |
|---|---|
| archived Sessions without membership (an older vBot after a downgrade) | adopted as `session`/`owner_group` entries, WARNING |
| `archive/entries/arc_*` without a row | real directories are adopted as a `recovered` entry; an empty one is removed; symbolic links and Windows junctions are left alone; WARNING |
| `archiving` | a payload tree moves back to `source_path` when it is free; beside a source it is removed only if every payload entry also exists there unchanged (regular files compared byte for byte, no links followed). Matching configuration alone is insufficient after snapshot restore. Missing, different, unreadable or linked entries keep the payload, adopted as a `recovered` entry; recorded unroots are undone, the row is deleted |
| `archived` with `cleanup_pending` | Agent cleanup or Project/Session current-pointer repair runs again; a live Session at the same address keeps its pointer, and an invalid or unreadable Agent document keeps cleanup pending |
| `restoring`, payload present or target gone | a moved legacy Workspace placed inside the archive returns to the payload (one already back in its own folder stays there), then `archived` again; with neither payload nor target its restore check names `payload_missing`, and it stays purgeable |
| `restoring`, files live at the target | the Agent or Project store verifies the persisted creation timestamp against the restore plan before replaying the configuration rewrite, accepting the archived or target id; once valid, the restore commits and finishes. Missing or different identity evidence leaves the files and Sessions unchanged. Otherwise the files stay live, the entry stays `restoring` with `restore_plan.live_path` and `problem`, its restore check shows both, and a later start settles it again |
| `restoring`, payload or target cannot be checked | state, member Sessions and `restore_plan` stay unchanged; purge remains blocked, and a later start retries the restore |
| `restored` | the follow-up runs and deletes the row |
| `purging` | left; the retention sweep continues it after start, a manual purge at once (each first finishes any cleanup still pending) |

## Interrupted restores

Agent and Project restore plans persist `source_created_at` before any file move. Recovery requires a valid timestamp matching the destination's raw persisted `created_at` under the owning store's lock; a reused id cannot attach archived Sessions to a replacement or overwrite its configuration. A plan without that proof, or a document with no persisted timestamp, stays `restoring` for inspection rather than treating synthesized defaults as identity. Ordinary restores remain available for unstamped payloads.

Agent restore plans also persist the selected `workspace` and `root_project_id`. If the tree is already live, recovery replays that configuration through `AgentStore.complete_restore_files` before the Session commit, using the normal restore rewrite without repairing an archived current-Session pointer. This includes the default Workspace chosen when a moved legacy Workspace's original folder is occupied (`test_archive_recovery.py`).

## Provenance limits

- Project archives record each unrooted Agent's immutable creation timestamp (`agent_created_at`) beside its id. Restoration and interrupted-archive compensation re-root only that incarnation. For records without the stamp, an Agent created after the Project archive is excluded; an unreadable timestamp cannot establish identity. A reused id therefore keeps its own default Project and Workspace (`test_archive_service.py`).
- Working Project associations cannot be reconstructed safely for already-existing archives: old Sessions can later enter a replacement Project, and forks inherit history timestamps. A restore under another id therefore leaves those unproven Identity Session associations unchanged and reports `working_project_association_unproven` in both the check and outcome, asking for an explicit move or fork into the restored Project. A same-id restore keeps its usual semantics.
- Legacy Parent links are backfilled only when exactly one Session generation has that address; ambiguous links stay unchanged and a restore-as warns `subagent_parent_unproven` with their count (they can still name a replacement; start fresh delegations instead of resuming them as restored children).
- Legacy delegation grants without a valid holder stamp are not re-added, and replaced holders keep their own policy; checks and outcomes warn `grant_holder_unproven` or `grant_holder_replaced`, asking for an explicit policy review (`grant_target_missing` still names missing holders).
- These are additive Generation 1 provenance fields, not a payload-format conversion. The Session migration fences older versions because their writes invalidate ownership provenance (`sessions/ownership.md` -> Compatibility). Returning to an older version requires the pre-update data snapshot, not a direct downgrade of the migrated store.
