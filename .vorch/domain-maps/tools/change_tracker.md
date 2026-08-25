# Change Tracker (git-style change statistics)

Task-gated depth for `core/tools/change_tracker.py` - the session-scoped
file-content tracker that powers the WebUI's git-style change statistics.

## What it does

Tracks, per session, one real content delta per mutated file so the chat loop
can compute git-style before/after line diffs - streamed live after each
dispatched Tool round and consumed once at Run end. Every mutation is recorded
against the file's **actual on-disk content immediately before the mutation**,
so external changes between tool calls (formatters, shell commands, other
sessions) stay outside the run's delta instead of being attributed to it.
Repeated mutations of one file within a Run count once against the first
mutation's pre-state, matching how `git diff --stat` reports a working-tree
delta. No git repository or external process is involved; the diff uses
`difflib.SequenceMatcher` with **autojunk disabled** (the default heuristic
treats frequently repeated lines in long files as junk and inflates replace
blocks where git reports a minimal diff).

## Data flow

1. `write`/`edit` -> capture the pre-mutation on-disk content, then `ChangeTracker.record_write(session_id, resolved, before, after)` stores the pair per `(session, path)`. The capture must happen **inside the mutation lock before the atomic write** - reading afterwards would record the new content as its own baseline (past failure mode: every write netted to zero). `edit` already has the decoded old text in hand; `write` reads the existing bytes bounded by `MAX_TRACKED_BYTES` and BOM-stripped. A brand-new file records `before=""`.
2. Chat loop after each dispatched Tool round -> `ChangeTracker.peek_run_stats(session_id)` computes the same totals **without consuming** them and emits them as the transient `run_change_stats` Run event (`{change_stats: {files, added, removed, paths}}`) whenever they differ from the previously emitted value. An all-zero object (edits reverted within the run) retires an earlier nonzero total; `None` (nothing tracked) emits nothing. This is what the WebUI displays while the Run is still executing.
3. Chat loop run end (`_execute_run_impl` finally block) -> peek first so an all-zero outcome persists explicitly, then `take_run_stats(session_id)` consumes the per-run deltas. Stats land in `run.terminal_payload_extras["change_stats"]` (live terminal event) and on the persisted `run_summary` message (`change_stats` field, validated by `_validate_change_stats` in `core/chat/messages.py`), so reloads keep the server-computed values - identical to the last live value by construction.

`read` takes no part in change statistics (no baselines are stored anymore); it only stamps `FileReadState` for the read-before-write guard.

## Wiring

- One runtime-owned instance (`Runtime._change_tracker`), exposed as `Runtime.change_tracker`, injected into `ChatLoopDependencies.change_tracker`.
- The chat loop threads it through `ToolDispatchContext.change_tracker` -> `ToolExecutionConfig.change_tracker` -> `ToolContext.change_tracker`.
- `ToolContext.change_tracker` is `None` for direct/legacy callers that do not execute inside Chat - those simply skip tracking. The write/edit handlers read it from the context; no tool registration signature carries the tracker.

## Best-effort semantics

Untracked changes mean the run falls back to the client-side per-tool-call
counts (the `line_change` display facts summed in the WebUI). The fallback
only applies when no server value exists at all; a server-reported zero is
authoritative and suppresses the fallback. Specifically not tracked:

- Non-UTF-8 content and files whose before- or after-content exceeds `MAX_TRACKED_BYTES` (512 KiB).
- What the agent changes purely via `bash`/`terminal` (e.g. `npm install` rewriting `package-lock.json`, formatter runs in a run with no tracked file-tool touch) - no tool-based tracker can see that. Changes that happen between two tracked mutations of the same file in one run are absorbed into that run's net disk delta, which matches git.
- Formatter/shell churn after a run's last tracked mutation is invisible until the next run touches the file; it lands in the commit but not in any run's stats.

## Limits

- `_MAX_TRACKED_FILES` (4096) session entries, oldest evicted first.
- `_MAX_REPORTED_PATHS` (200) paths per run payload.
- Per-run deltas are in-memory only: a server restart loses them (the UI falls back to the tool-fact sum for that run).
