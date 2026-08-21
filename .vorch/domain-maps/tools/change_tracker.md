# Change Tracker (git-style change statistics)

Task-gated depth for `core/tools/change_tracker.py` — the session-scoped
file-content tracker that powers the WebUI's git-style change statistics.

## What it does

Tracks, per session, the last known text content of every file the session has
read or written, so the chat loop can compute a real before/after line diff at
the end of a run. The diff uses `difflib.SequenceMatcher` — the same Myers
line-diff algorithm git uses for `git diff --stat` — so repeated edits of the
same line count once and a full-file rewrite shows only the lines that actually
changed. No git repository or external process is involved.

## Data flow

1. `read` (full-file reads only, no `offset`/`limit` window) → `ChangeTracker.record_read(session_id, resolved, content)` stores the file's text as the session baseline.
2. `write`/`edit` → `ChangeTracker.record_write(session_id, resolved, before, after)` stores the before/after pair per `(session, path)`. The `before` is the session's last known content (from read or a prior write), so repeated edits of one file in a run diff against the run's first baseline instead of summing per-call counts.
3. Chat loop run end (`_execute_run_impl` finally block) → `ChangeTracker.take_run_stats(session_id)` computes one real line diff per changed file, sums added/removed, and returns `{files, added, removed, paths}` (paths sorted, capped at 200). The per-run deltas are consumed and cleared.
4. The stats land in `run.terminal_payload_extras["change_stats"]` (live UI) and on the persisted `run_summary` message (`change_stats` field, validated by `_validate_change_stats` in `core/chat/messages.py`), so reloads keep the server-computed values.

## Wiring

- One runtime-owned instance (`Runtime._change_tracker`), exposed as `Runtime.change_tracker`, injected into `register_read_tool`/`make_read_handler` and into `ChatLoopDependencies.change_tracker`.
- The chat loop threads it through `ToolDispatchContext.change_tracker` → `ToolExecutionConfig.change_tracker` → `ToolContext.change_tracker`.
- `ToolContext.change_tracker` is `None` for direct/legacy callers that do not execute inside Chat — those simply skip tracking.

## Best-effort semantics

A missing baseline means the run falls back to the client-side per-tool-call
counts (the `line_change` display facts summed in the WebUI). Specifically not
tracked:

- Files never read in the session (an existing file written without a read has no known before-content).
- Non-UTF-8 content, files over `_MAX_TRACKED_BYTES` (512 KiB), and content that fails to encode.
- Partial reads (`offset`/`limit` windows) — only a complete read is a trustworthy baseline.
- What the agent changes via `bash`/`terminal` (e.g. `npm install` rewriting `package-lock.json`) — no tool-based tracker can see that.

## Limits

- `_MAX_TRACKED_FILES` (4096) baselines, oldest evicted first.
- `_MAX_REPORTED_PATHS` (200) paths per run payload.
- Per-run deltas are in-memory only: a server restart loses them (the UI falls back to the tool-fact sum for that run).
