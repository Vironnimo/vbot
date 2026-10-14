# Scratch Areas

Temporary intermediate files for a Session and its Sub-Agent tree, owned by
`core/tools/scratch_areas.py::ScratchAreas`. This is a Tools-domain resource owner,
not an Agent-callable Tool. Shared vocabulary: GLOSSARY -> Scratch Area.

## Ownership and layout

`DataDirectoryLayout.scratch_temporary` names `<data-dir>/artifacts/temp/scratch`.
`ScratchAreas` alone resolves participants, creates folders, records activity and
removes complete areas. `TemporaryFileManager` does not sweep this tree.

```
scratch/sc_<12 base32 characters>/
  main/                        root Session's folder
    tmp/                       process TEMP, TMP and TMPDIR
  sub1/                        first allocated Sub-Agent folder
    tmp/
  sub2/
    tmp/

scratch-state/<area-name>/
  .owner                       root Session generation
  .participants                Sub-Agent generation -> subN folder names
  .activity                    last activity, stored as filesystem mtime
  .retire                      pending archive removal, when present
  <folder-name>.incarnation     token and directory filesystem identities
```

`DataDirectoryLayout.scratch_state_temporary` names the separate disposable
bookkeeping directory. The owner creates and repairs it lazily; it is not a
required Storage bootstrap directory. Nothing inside an announced Scratch Area
is required system state. Older `.activity` and `.incarnation` files there are
inert Agent files: arbitrary contents, replacement or deletion do not affect
setup. Agents may empty or remove their entire folder.

New area names use the shared `new_id("sc", claim=...)` allocator; exclusive
directory creation claims each candidate and retries collisions. Each Session
generation has one stable folder within the area: `main` for its root and `subN`
for Sub-Agents, numbered in allocation order, regardless of tree depth. Assigned
names stay reserved even when their folders are removed. The names are actual
filesystem paths, not display aliases or truncated Session ids.

The globally unique Session generation, not its scope-local public Session id,
keys the external assignment records. They are written atomically and survive
restarts. An in-memory index of area assignments is rebuilt from `.owner` records
on first use; known ownership is repaired when disposable state disappears.
Missing or malformed participant mappings allocate unused folders, leaving
unidentified existing work in place rather than adopting another Session's files.
An area whose ownership record is lost across a restart is left for normal
retention; the Session receives a fresh area.

Existing generation-named areas keep their exact paths, including newly joining
Sub-Agents, until normal retention or archive removes the area. No active files
are moved, and previously announced paths keep working. The next area uses short
names. This preserves existing references while keeping internal 32-character
generation ids out of newly allocated Agent-facing paths (user review 2026-10-14).

Identity lookup and nearest-first ancestry lookup are injected. The farthest live
ancestor determines the area; a missing ancestor ends the live tree. A surviving
Child therefore starts a fresh area after its ancestor is archived. Tree
resolution is not cached: archive, restore and address reuse can change it.
Agent rename and Agent Takeover keep the generation and folder; forks have a fresh
generation and no Parent link.

`folder_for(address)` runs filesystem and Session lookups on a bounded worker pool,
ensures the own folder and `tmp/`, touches `.activity`, and returns `ScratchFolder`
with `own`, `area`, `tmp`, `recreated` and `incarnation`. External incarnation
records identify the own and tmp directories by device, inode and birth time
where available; normal writes do not change the token, including across restarts.
Linux reads birth time through `statx`, because Python's `lstat` does not expose
it; the returned device/inode must match the checked directory. Unsupported
kernels, C libraries or filesystems retain device/inode-only best-effort identity:
replacement can remain undetected if a recreated directory reuses its previous inode number.
Access/I/O errors and inconsistent observations remain setup failures. Mutable
ctime and mtime are never used as creation identity.
Detected directory recreation/replacement and missing/malformed records receive a new token,
so Chat can detect loss even when a process recreated the folder before the next
Run. Missing tmp is recreated; a file or link replacing a disposable directory or
metadata entry is removed as an entry and repaired, never followed. Repair of the
shared state root is serialized across trees so one repair cannot erase another
tree's newly written retirement intent. Unreadable entries remain failures, never
an assertion that the old data was absent.

## Retention and archive

`start()` schedules an immediate sweep, then hourly sweeps. The clock is injected;
`SCRATCH_RETENTION` is 7 days. The external marker mtime survives restarts. Missing
or malformed activity state receives a full retention grace interval; this safely
adopts areas from the former layout without trusting old directory mtimes or
Agent-owned markers. The injected busy-address snapshot combines active
Runs, queued input, unfinished commands and live Agent Terminals. Any participant
keeps its whole area and refreshes activity. Operator Terminals are outside this
ownership. Run starts and process launches record activity through `folder_for`.
Runtime also records each admitted Run's Session address synchronously. Before and
after a candidate's rename, a worker resolves one finite snapshot of newly admitted
addresses to their trees. Admission in that tree keeps/restores its area and
refreshes activity; unrelated admissions do not stop its cleanup. The post-rename
snapshot is the retirement decision boundary: later starts wait for the area lock
and get a fresh folder when needed. No retry loop waits for a quiet server, the
counter lock holds no filesystem I/O, and admission records are cleared after the
last outstanding sweep settles.

Creation, touch and retirement serialize per root generation, including when the
directory has a short name. Retirement renames the area to
a private trash entry while holding the lock, then deletes it outside the lock.
Sweeps retry leftover trash. Shared `core/utils/tree_move.py::remove_tree` removes
symlinks and Windows junctions as links and clears read-only attributes without
following targets. Errors leave cleanup for a later sweep and log no filenames
from inside an area. One INFO record counts removed areas per sweep; per-area
details are DEBUG and cleanup failures WARN.

Archive captures root generation ids with `roots_for_archive(addresses)` before
changing Session liveness, then calls `remove_areas(root_ids)` after success.
Before the rename, archive removal writes an empty `.retire` marker in its external
state directory. Legacy `.retire-<area-name>` entries beside the areas are
adopted there, preserving pending removal on upgrade. A Windows open-file handle
can refuse the rename itself;
this marker survives restart and makes the next sweep retry regardless of age or
busy state. A restored Session's `folder_for` first retires any such pending area,
or fails while the OS still refuses it; it never adopts the archived files. The
state is cleared after retirement and before a replacement area can be created.
Archiving a Child alone does not remove its root's area. Restore creates folders
on demand. `remove_area_of` is the convenience operation for still-live roots;
using it after archive cannot resolve the archived generation. `stop` requests
shutdown and `aclose` settles the sweep task and its worker.

Scratch is optional: `folder_for` converts ordinary lookup/setup failures into
`ScratchUnavailableError` and logs bounded failure/recovery transitions without
paths or contents. The conversion happens inside the worker so late failures
after caller cancellation are safe to log as well. Cancellation and
`RunInterruptedError` propagate unchanged. Chat continues without announcing
unavailable paths; Shell and Agent Terminals continue their normal launch without
the Scratch environment overlay. Scratch failure cannot make a requested command
or Run fail. Optional announcement persistence is isolated by Chat too.

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
Prompt, preserving shared prompt-cache prefixes. The bundled `core:runtime`
contains one static, path-free sentence directing temporary work to the own folder
in the latest reminder unless the user requests another location (`prompts.md`).

The Shell Tool and Agent Terminal launch call `folder_for` again before starting
each process. Their environment sets `TEMP`, `TMP`, `TMPDIR` to `tmp` and
`VBOT_SCRATCH_DIR` to `own` (`shell.md`, `terminal.md`). The reminder's process
sentence appears only with a callable Shell or Terminal. Run change statistics
exclude paths below the Scratch root (`change_tracker.md`).

Writing only in one's own Scratch folder is Agent guidance, not a filesystem
permission boundary: existing file and Shell Tools retain their normal access.
Results the user keeps belong outside Scratch.

The 2026-10-13 live eval used three scenarios with DeepSeek V4.1 Flash and GLM
5.3 Flash on the reminder-only implementation. All six reports contained the
correct requested regional figures; shared-area and recreated-folder mechanics
worked. GLM's delegation
scenario nevertheless left extracted inputs in the Project and directed child
intermediates there; DeepSeek initially downloaded into the Project before moving
the file to Scratch. This evidence triggered the plan's optional static Runtime
sentence. That subsequent sentence was reviewed but not evaluated with Models.
Folder placement remains guidance rather than an enforced filesystem boundary.

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
| `When delegating, put the inputs in your own folder and pass their paths; let each Sub-Agent keep its intermediate files in its own Scratch folder and report its result paths.` | Makes file exchange concrete without inventing a shared writable folder or copying large intermediate content into messages. The trigger leaves ordinary chat unaffected. The input location and the Sub-Agents' own folders were added after the eval of 2026-10-13, where a GLM 5.3 Flash Parent extracted inputs into the Project and directed its Sub-Agents to write intermediate results there, overriding their own correct reminders. Not yet rerun with Models (Provider rate limit). |
| `Temporary files of your processes already go to tmp/ in your folder; VBOT_SCRATCH_DIR names your folder in commands.` | Explains automatic temp placement and the portable command reference; shown only with Shell or Terminal access. |
| `vBot deletes this area after 7 days without activity.` | Prevents using a current filesystem path as a promise of durable storage. |
| `You do not need to clean it up; you can delete files you have finished with.` | Avoids a cleanup task on every turn while permitting deliberate removal of spent files. |
| `Scratch is temporary working space, not Memory or a delivery location.` | Separates intermediate work from durable knowledge and user deliverables. |
| `Put results the user will keep and files you hand to the user in the working directory or Workspace; put notes needed in a later conversation in the Workspace or Memory.` | Supplies the concrete destinations for artifacts that must outlive retention. |
| `Your Scratch folder has been recreated or changed.` | Reports a changed path or incarnation, including when a process already recreated the folder. Earlier files can survive in another area after an intermediate Parent is archived, so this does not assert that every earlier file was deleted. |
| `Do not assume files at earlier Scratch paths still exist; use the folder above for new work.` | Prevents trusting stale references after retention, archive or external deletion and directs new writes to the current own folder. |

## Source and tests

- Resource ownership and filesystem contracts: `core/tools/scratch_areas.py`,
  `tests/core/tools/test_scratch_areas.py`; Linux creation-time lookup:
  `core/tools/_scratch_identity.py`, `tests/core/tools/test_scratch_identity.py`.
- Run-start note and Compaction retention: `core/chat/_run_execution.py`,
  `core/chat/_scratch_context.py`, `core/chat/_request_builder.py`,
  `tests/core/chat/test_chat_loop_scratch.py`.
- Fork metadata strip: `core/sessions/_types.py`; tested through the Chat fork path.
