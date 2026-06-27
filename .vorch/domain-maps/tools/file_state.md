# File-State Guard (read-before-write)

The per-session read-before-write / stale-file guard shared by `read`, `write`, and `edit`. Lives in `core/tools/file_state.py`.

## Overview

A single runtime-owned `FileReadState` registry remembers, per session, the `(mtime, size)` of every file a session has read, so `write`/`edit` can **block** (hard failure envelope) a clobber of a file the session never read or that changed on disk since the read. It is constructor-injected into the three tools (one instance built in `Runtime`, like `ProcessManager` for `bash`) — not a module singleton. Design follows OpenCode's (since-removed) `FileTimeService`; it deliberately does **not** track sibling writers, store read timestamps, or hash content (a Hermes-style sibling layer was considered and rejected — sessions isolate concurrent subagents already, and mtime/size answer "changed since I read it" regardless of who changed it).

## Interfaces

- `FileReadState.record_read(session_id, resolved)` — stat the file and store `(mtime, size)`. Called by `read` *before* reading bytes, and by `write`/`edit` *after* a successful write (a write is an implicit read → restamp, so the same session writes again without re-reading).
- `FileReadState.check_stale(session_id, resolved) -> StaleReason | None` — `NEVER_READ` (no stamp for this session+path), `MODIFIED` (current `(mtime, size)` ≠ stamp), or `None` (safe). Returns `None` when the file cannot be stat'd (vanished mid-call → a race, not staleness).
- `stale_failure_text(reason, resolved) -> (code, message)` — shared mapping so both tools emit identical failures: `file_not_read` / `file_modified_since_read`.
- `FILE_STATE_GUARD_ENABLED` (module constant, default `True`) — single process-wide off switch; when `False`, `record_read` no-ops and `check_stale` returns `None`.

## Constraints & Gotchas

- **Key is `(session_id, str(resolved))`** — per session, by resolved absolute path. Different sessions (including subagents, which get their own session) never share read history. State is in-memory only, lost on restart (which just forces re-reads).
- **New files are exempt at the call site, not here.** `write` runs `check_stale` only when `resolved.exists()`; `edit` always operates on an existing file. `check_stale` itself reports `NEVER_READ` for any unstamped path regardless of existence — callers must gate on existence.
- **Comparison is not-equal on `(mtime, size)`**, not strictly-newer. `size` is a free second signal that catches some same-`mtime`-tick changes (coarse filesystem resolution). A same-length change within the same mtime tick is the accepted blind spot — no content hashing closes it by design.
- **Stamp before the byte read** (in `read`): if an external write lands in the window after the stamp, the stamp stays older than the new content, so the next write/edit errs toward a harmless re-read rather than missing the change.
- **Bash writes are not stamped.** A file changed via `bash` (e.g. `sed -i`) is later seen as `MODIFIED` and forces a re-read — intended (the session's in-memory view is stale). vBot deliberately does **not** parse bash commands to re-stamp touched files (Claude Code does; we accept the extra re-read instead).
- **Partial reads count as full reads** — `record_read` ignores offset/limit/truncation, so a truncated read of a large file still satisfies the guard for a later full `write`.
- **Bounded:** at most `_MAX_TRACKED_FILES` (8192) entries; oldest insertion evicted first. A rare eviction only costs a harmless re-read.
