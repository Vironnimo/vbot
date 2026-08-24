# Change Tracker (git-style change statistics)

Task-gated depth for `core/tools/change_tracker.py` — the session-scoped
file-content tracker that powers the WebUI's git-style change statistics.

## What it does

Tracks, per session, the last known text content of every file the session has
read or written, so the chat loop can compute real before/after line diffs —
streamed live after each dispatched Tool round and consumed once at Run end.
The diff uses `difflib.SequenceMatcher` — the same Myers
line-diff algorithm git uses for `git diff --stat` — so repeated edits of the
same line count once and a full-file rewrite shows only the lines that actually
changed. No git repository or external process is involved.

## Data flow

1. `read` (full-file reads only, no `offset`/`limit` window) → `ChangeTracker.record_read(session_id, resolved, content)` stores the file's **raw text** (BOM stripped, line endings untouched) as the session baseline. The numbered `N| ` rendering is never a baseline — a later write must diff against real content, not against the gutter.
2. `write`/`edit` → `ChangeTracker.record_write(session_id, resolved, before, after)` stores the before/after pair per `(session, path)`. The `before` is the session's last known content (from read or a prior write), so repeated edits of one file in a run diff against the run's first baseline instead of summing per-call counts.
3. Chat loop after each dispatched Tool round → `ChangeTracker.peek_run_stats(session_id)` computes the same totals **without consuming** them and emits them as the transient `run_change_stats` Run event (`{change_stats: {files, added, removed, paths}}`) whenever they differ from the previously emitted value. An all-zero object (edits reverted to their baseline) retires an earlier nonzero total; `None` (nothing tracked) emits nothing. This is what the WebUI displays while the Run is still executing.
4. Chat loop run end (`_execute_run_impl` finally block) → peek first so an all-zero outcome persists explicitly, then `take_run_stats(session_id)` consumes the per-run deltas. Stats land in `run.terminal_payload_extras["change_stats"]` (live terminal event) and on the persisted `run_summary` message (`change_stats` field, validated by `_validate_change_stats` in `core/chat/messages.py`), so reloads keep the server-computed values — identical to the last live value by construction.

## Wiring

- One runtime-owned instance (`Runtime._change_tracker`), exposed as `Runtime.change_tracker`, injected into `ChatLoopDependencies.change_tracker`.
- The chat loop threads it through `ToolDispatchContext.change_tracker` → `ToolExecutionConfig.change_tracker` → `ToolContext.change_tracker`.
- `ToolContext.change_tracker` is `None` for direct/legacy callers that do not execute inside Chat — those simply skip tracking. The read/write/edit handlers read it from the context; no tool registration signature carries the tracker.

## Best-effort semantics

A missing baseline means the run falls back to the client-side per-tool-call
counts (the `line_change` display facts summed in the WebUI). The fallback
only applies when no server value exists at all; a server-reported zero is
authoritative and suppresses the fallback. Specifically not tracked:

- Files never read in the session (an existing file written without a read has no known before-content).
- Non-UTF-8 content, files over `_MAX_TRACKED_BYTES` (512 KiB), and content that fails to encode.
- Partial reads (`offset`/`limit` windows) — only a complete read is a trustworthy baseline.
- What the agent changes via `bash`/`terminal` (e.g. `npm install` rewriting `package-lock.json`) — no tool-based tracker can see that.

## Limits

- `_MAX_TRACKED_FILES` (4096) baselines, oldest evicted first.
- `_MAX_REPORTED_PATHS` (200) paths per run payload.
- Per-run deltas are in-memory only: a server restart loses them (the UI falls back to the tool-fact sum for that run).
