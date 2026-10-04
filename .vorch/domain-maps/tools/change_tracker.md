# Change Tracker (Run change statistics)

Task-gated depth for `core/tools/change_tracker.py` - the Run-scoped
tracker behind the WebUI's git-style change statistics (`N files changed,
+A -R` per Run and Session).

## What it counts

A Run's statistics cover only the lines the Run itself changed through
the file edit Tools (`apply_patch`, `edit`, `write`). Shell commands (`bash`, `terminal`,
`process`), `skill_manage`, Memory and media Tools are not counted: with
several Sessions working in one directory, a change seen on disk cannot be
attributed to the Run that caused it.

- **Segments.** Consecutive writes of one file by one Run form a segment that
  counts once, as the minimal line diff from the content before its first write
  to the content after its last (repeated edits of one line count once, a
  reverted edit counts zero, like `git diff --numstat` of a working tree).
- **Attribution.** The file edit Tools report each committed write with the file's
  actual content right before and after it, captured inside its mutation lock
  before the atomic write (reading afterwards would record the new content as
  its own baseline). When a write's `before` differs from the Run's last
  written content, the file changed in between (another Session, a formatter,
  a shell command): the open segment closes and a new one starts from the
  changed content, so the change in between is never the Run's. A file's
  counts are the sum of its segments, so a Run that edits a line, lets
  something else touch the file, and reverts its edit counts both edits.
- **Every write counts.** Retained contents are only what lets a segment net
  repeated edits. A file with more than `MAX_RETAINED_FILE_CHARS` (512 Ki
  characters) on either side, and the oldest segments once all retained
  contents exceed `MAX_RETAINED_CHARS` (64 Mi characters across Runs), are
  counted right away and drop their contents; the next write of that file
  starts a new segment. There is no eviction that loses counts and no
  "incomplete" state.
- **Diff.** `core/tools/_line_diff.py` (minimal Myers diff, shared with the
  Tool diff details, so per-call and per-Run counts agree). Line endings are
  content: a changed trailing newline or CRLF counts as a changed line, like
  git. A change too large to search within `MAX_EDIT_DISTANCE` counts its
  searched region as replaced.
- Moves count as the source deleted plus the destination created. Binary
  moves and deletions, symlinks and undecodable files are not counted.

## Interface

- `record_write((session_address, run_id), resolved, before, after)` - from
  `_file_changes._commit` for each committed text write (a new file records
  `before=""`, a deletion `after=""`). Validation-only plans and verified no-ops
  record nothing; a partial write records only completed paths.
- `peek_run_stats(run_key)` - current statistics without consuming them.
  Counts are cached per segment; only segments written since the last peek
  are diffed again.
- `take_run_stats(run_key)` - final statistics; detaches the Run first, so a
  failing diff cannot leak its entries to a later Run.
- Both return `None` when the Run recorded no write, and otherwise
  `{files, added, removed, paths, file_stats}`: `paths` lists the changed files
  (absolute paths) in path order and `file_stats` the same files as
  `{path, added, removed}`; a file whose counts are zero is in neither, and a
  Run whose writes net to nothing reports explicit zeros so a reverted change
  retires an earlier total. `paths`/`file_stats` stop at `MAX_REPORTED_PATHS`
  (200) while `files`, `added` and `removed` count every changed file.
- Every operation uses the complete Session address plus Run id: neither id
  alone distinguishes all Agent/Project scopes, and consuming one Run must
  leave concurrent scopes and successor Runs intact.
- Diffs run outside the tracker lock on the caller's worker thread; a segment
  written or taken meanwhile keeps its newer state.

## Data flow

1. The file edit Tools (`_file_changes._commit`) -> `record_write` (Tool worker thread).
2. Chat loop after each dispatched Tool round
   (`core/chat/_agentic_progression.py`) -> `peek_run_stats` on a Chat worker
   -> when the value changed, first stored on the running Run
   (`ChatSession.record_change_stats_async`, best-effort with a warning), then
   the transient `run_change_stats` Run event (`{change_stats}`) that the WebUI
   shows while the Run executes. An all-zero object retires an earlier nonzero
   total; `None` (nothing written yet) is neither stored nor emitted.
3. Run end (`core/chat/_run_execution.py`, `_execute_run_impl` finally block)
   -> `take_run_stats` on a Chat worker inside the visible-boundary guard, so
   Stop cannot skip it or lose the totals -> `run.terminal_payload_extras
   ["change_stats"]` -> every terminal Run event and the persisted
   `run_summary` (`runs` columns `changed_files`/`lines_added`/`lines_removed`
   plus `run_change_paths`, `sessions.md`).

The tracker itself is in memory only. A process restart loses what a Run
wrote after its last Tool round; the Run recovered as `interrupted` keeps the
statistics stored up to then. Session totals are sums over the stored Run
statistics (`sessions.md` -> Interfaces, `summary`).

`read` takes no part in change statistics; it only stamps `FileReadState` for
the read-before-write guard.

## Wiring

- One runtime-owned instance (`Runtime._change_tracker`), exposed as
  `Runtime.change_tracker`, injected into `ChatLoopDependencies.change_tracker`.
- The chat loop threads it through `ToolDispatchContext.change_tracker` ->
  `ToolExecutionConfig.change_tracker` -> `ToolContext.change_tracker`.
- `ToolContext.change_tracker` is `None` for callers outside Chat; those skip
  tracking. No Tool registration signature carries the tracker.
