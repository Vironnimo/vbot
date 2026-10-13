# Scratch Areas

Temporary intermediate files for a Session and its Sub-Agent tree, owned by
`core/tools/scratch_areas.py::ScratchAreas`. This is a Tools-domain resource owner,
not an Agent-callable Tool. Shared vocabulary: GLOSSARY -> Scratch Area.

## Ownership and layout

`DataDirectoryLayout.scratch_temporary` names `<data-dir>/artifacts/temp/scratch`.
`ScratchAreas` alone resolves participants, creates folders, records activity and
removes complete areas. `TemporaryFileManager` does not sweep this tree.

```
scratch/<root-generation>/
  .activity                    last activity, stored as filesystem mtime
  <participant-generation>/
    .incarnation               folder-incarnation token
    tmp/                       process TEMP, TMP and TMPDIR
```

The globally unique Session generation, not its scope-local public Session id,
keys both directories. Identity lookup and nearest-first ancestry lookup are
injected. The farthest live ancestor determines the area; a missing ancestor ends
the live tree. A surviving Child therefore starts a fresh area after its ancestor
is archived. Resolution is not cached: archive, restore and address reuse can
change the live tree. Agent rename and Agent Takeover keep the generation and
folder; forks have a fresh generation and no Parent link.

`folder_for(address)` runs filesystem and Session lookups on a bounded worker pool,
ensures the own folder and `tmp/`, touches `.activity`, and returns `ScratchFolder`
with `own`, `area`, `tmp`, `recreated` and `incarnation`. The persistent incarnation
changes when the folder is recreated, so Chat can detect loss even when a process
launch recreated it before the next Run. Do not infer disappearance from a failed
filesystem check or silently replace unreadable markers.

## Retention and archive

`start()` schedules an immediate sweep, then hourly sweeps. The clock is injected;
`SCRATCH_RETENTION` is 7 days. The marker mtime survives restarts; an absent marker
falls back to the area's mtime. The injected busy-address snapshot combines active
Runs, queued input, unfinished commands and live Agent Terminals. Any participant
keeps its whole area and refreshes activity. Operator Terminals are outside this
ownership. Run starts and process launches record activity through `folder_for`.
Runtime also records every admitted Run synchronously in an activity counter. A
sweep cannot retire an area from a busy snapshot taken before that admission;
if admission races the rename, the original area is restored under its root lock
before folder creation can continue. The counter lock never holds filesystem I/O.

Creation, touch and retirement serialize per root. Retirement renames the area to
a private trash entry while holding the lock, then deletes it outside the lock.
Sweeps retry leftover trash. Shared `core/utils/tree_move.py::remove_tree` removes
symlinks and Windows junctions as links and clears read-only attributes without
following targets. Errors leave cleanup for a later sweep and log no filenames
from inside an area. One INFO record counts removed areas per sweep; per-area
details are DEBUG and cleanup failures WARN.

Archive captures root generation ids with `roots_for_archive(addresses)` before
changing Session liveness, then calls `remove_areas(root_ids)` after success.
Before the rename, archive removal writes an empty `.retire-<root-generation>`
marker beside the area. A Windows open-file handle can refuse the rename itself;
this marker survives restart and makes the next sweep retry regardless of age or
busy state. A restored Session's `folder_for` first retires any such pending area,
or fails while the OS still refuses it; it never adopts the archived files. The
marker is cleared after retirement and before a replacement area can be created.
Archiving a Child alone does not remove its root's area. Restore creates folders
on demand. `remove_area_of` is the convenience operation for still-live roots;
using it after archive cannot resolve the archived generation. `stop` requests
shutdown and `aclose` settles the sweep task and its worker.

## Chat and process contracts

Chat's `_scratch_context.py` owns the persisted `[scratch-area]` note. At Run start,
after resolving effective callable Tools, Chat calls `folder_for` for Run kinds
`user`, `channel`, `cron`, `calendar`, `subagent` and `system` with at least one of
`bash`, `terminal`, `write`, `edit` or `apply_patch`. Background learning kinds and
`live` are excluded. The live Tool catalog, Run restriction and live denial apply,
including a Tool enabled since the prompt epoch was pinned. Manual Compaction does
not enter the Agentic executor.

Open Session metadata `scratch_announced` records the own path, incarnation and
note id. A missing record, changed path/incarnation, recreated folder, or absent
note in the effective Compaction view causes an announcement; a folder change
uses the cleared variant. Forks strip this metadata, so inherited announcements
cannot suppress the fork's own paths. The Compaction finalizer carries the latest
existing Scratch note verbatim after the summary when the projection omitted it,
preserving it during a Run as well as between Runs. Paths never enter the System
Prompt, preserving shared prompt-cache prefixes.

The Shell Tool and Agent Terminal launch call `folder_for` again before starting
each process. Their environment sets `TEMP`, `TMP`, `TMPDIR` to `tmp` and
`VBOT_SCRATCH_DIR` to `own` (`shell.md`, `terminal.md`). The reminder's process
sentence appears only with a callable Shell or Terminal. Run change statistics
exclude paths below the Scratch root (`change_tracker.md`).

Writing only in one's own Scratch folder is Agent guidance, not a filesystem
permission boundary: existing file and Shell Tools retain their normal access.
Results the user keeps belong outside Scratch.

## Agent-facing text

The note is English kernel-authored text, rendered as an existing System Reminder.
Paths use forward slashes. Each sentence addresses a concrete placement or
lifecycle mistake; the user-requested design is the evidence for these rules.

| Text | Reason |
|---|---|
| `Your Scratch folder for intermediate work is {own}.` | Gives the writable location for scripts, downloads and intermediate work instead of the repository or system temp. |
| `Scratch Area: {area}.` | Gives the shared tree boundary so a participant can find the files named by its Parent or peers. |
| `Other folders in this area belong to this conversation's Parent or Sub-Agents.` | Explains the other folders' provenance without suggesting they are public files or unrelated conversations. |
| `Within this area, write only in your own folder; read the others when useful.` | Prevents concurrent writers from editing each other's intermediate files; the explicit area scope keeps final output outside Scratch possible. This is guidance, not enforced isolation. |
| `When delegating, pass input paths and have each Sub-Agent report its result paths.` | Makes file exchange concrete without inventing a shared writable folder or copying large intermediate content into messages. The trigger leaves ordinary chat unaffected. |
| `Temporary files of your processes already go to tmp/ in your folder; VBOT_SCRATCH_DIR names your folder in commands.` | Explains automatic temp placement and the portable command reference; shown only with Shell or Terminal access. |
| `vBot deletes this area after 7 days without activity.` | Prevents using a current filesystem path as a promise of durable storage. |
| `You do not need to clean it up; you can delete files you have finished with.` | Avoids a cleanup task on every turn while permitting deliberate removal of spent files. |
| `Scratch is temporary working space, not Memory or a delivery location.` | Separates intermediate work from durable knowledge and user deliverables. |
| `Put results the user will keep and files you hand to the user in the working directory or Workspace; put notes needed in a later conversation in the Workspace or Memory.` | Supplies the concrete destinations for artifacts that must outlive retention. |
| `Your Scratch folder has been recreated or changed.` | Reports a changed path or incarnation, including when a process already recreated the folder. Earlier files can survive in another area after an intermediate Parent is archived, so this does not assert that every earlier file was deleted. |
| `Do not assume files at earlier Scratch paths still exist; use the folder above for new work.` | Prevents trusting stale references after retention, archive or external deletion and directs new writes to the current own folder. |

## Source and tests

- Resource ownership and filesystem contracts: `core/tools/scratch_areas.py`,
  `tests/core/tools/test_scratch_areas.py`.
- Run-start note and Compaction retention: `core/chat/_run_execution.py`,
  `core/chat/_scratch_context.py`, `core/chat/_request_builder.py`,
  `tests/core/chat/test_chat_loop_scratch.py`.
- Fork metadata strip: `core/sessions/_types.py`; tested through the Chat fork path.
