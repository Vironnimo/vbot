# Logs

Read-only daily log viewer subsystem spanning backend parsing/watching and the WebUI Logs tab.

## Overview

The logs subsystem exposes application log files from `<data_dir>/logs/` for inspection in the WebUI. It owns daily file discovery, parsing the canonical log format into structured entries, and live updates for one selected file through a dedicated WebSocket. It does not write logs, edit log files, or reuse the shared app event bus. Filtering stays local in the WebUI after one file is loaded.

## Data Model

- Daily log catalog: `{ files: string[], default_file: string | null }`
- Read snapshot result: `{ file: string, entries: ParsedLogEntry[], cursor: string }`
- Parsed log entry:
  - `timestamp: string`
  - `level: string` — lower-cased level such as `info`, `warn`, `error`; a line that doesn't match the header gets `level: "unknown"` with empty `timestamp`/`logger_name`
  - `logger_name: string`
  - `message: string`
  - `continuation: string` — multiline tail such as stack traces
- Live stream event:
  - `type: "append" | "reset"`
  - `file: string`
  - `entries: ParsedLogEntry[]`

## Interfaces

- `core/utils/log_viewer.py`
  - `LogViewer.list_files()` → `{ files, default_file }`
  - `LogViewer.read_file(file_name)` → `{ file, entries, cursor }`
  - `LogViewer.subscribe(file_name, cursor?)` → async generator of `{ type, file, entries }`
- Server RPC
  - `log.list` — returns the daily log catalog sorted newest-first
  - `log.read { file }` — returns parsed entries plus a handoff cursor for one selected file
- Server transport
  - `GET /ws/logs?file=<name>&cursor=<cursor>` — streams append/reset events for one selected file only and can replay the read→socket handoff gap. Invalid file name or unknown/mismatched cursor → close code `1008`.
- WebUI
  - `listLogs()` / `readLogFile()` / `subscribeLogEvents()` in `webui/src/lib/api.js`
  - `webui/src/lib/logsView.js` owns client-side selection/filter/search/sort helpers
  - `webui/src/components/LogsView.svelte` renders the tab and reconnects its dedicated log stream using the latest read cursor

## Conventions

- Treat the log format `timestamp [LEVEL] name - message` as the canonical parse contract.
- Validate file names strictly; never allow path traversal or absolute paths.
- If a line does not match the header format, append it to the previous entry's `continuation` when possible; otherwise keep it visible as an `unknown` entry.
- The level filter is based only on parsed `level`. Logger names are searchable through free-text search, not separate filter UI.
- Entry ordering is accessor-local UI state. Switching between newest-first and oldest-first must not trigger another `log.read` call for the same file.
- `cursor` is an internal handoff token, not user-visible UI state.
- The selected file remains user-controlled. Refreshing the catalog may add newer files, but it must not auto-switch the active selection.
- The Logs toolbar uses the shared simple dropdown style for file, level, and order controls.
- Routine `/ws` and `/ws/logs` lifecycle noise (connection open/closed, accept) is filtered on two layers: at write time by the logging pipeline (`is_logs_websocket_lifecycle_record`, so it never lands in daily files going forward) and again at read/stream time in `parse_log_entries` (`_should_include_entry`, so pre-existing matching rows don't surface in `log.read` results or `/ws/logs` events). Genuine websocket transport failures must still stay visible.

## External Dependencies

- `watchfiles` — `awatch` watches the whole logs *directory* (`recursive=False`) in forced-polling mode (`force_polling=True`, `poll_delay_ms=50`, `debounce=100`), not the single selected file. Live append/reset events are derived from re-read file snapshots, not from raw watcher payloads — this polling setup is deliberate for reliable Windows behavior.

## Constraints & Gotchas

- Newest-file selection assumes daily filenames sort newest-first lexicographically.
- Initial load reads one full selected daily file into memory.
- Windows watcher events may duplicate or coalesce changes; derive append/reset events from file snapshots rather than raw watcher event counts.
- If a file is truncated, replaced, or otherwise diverges from the previous parsed prefix, emit a `reset` event so the UI replaces its entry list.
- **Watcher lifecycle is per-file and ref-counted.** One watcher task per file, shared by all subscribers; it starts on the first subscriber and stops when the last one leaves. `aclose()` (called on server shutdown with a 1 s timeout) tears down every watcher. Snapshots are read and diffed under a single async lock.
- **Cursor handoff is the no-gap guarantee.** `read_file` stores a one-shot handoff snapshot under a fresh UUID `cursor`, keyed per file (only the file's latest cursor is retained; total handoffs bounded to `MAX_READ_HANDOFFS = 32`, oldest pruned). `subscribe(cursor)` pops it and emits the missed append/reset diff *before* live events; `subscribe(cursor=None)` falls back to popping the file's latest stored cursor. A cursor is single-use; an unknown or file-mismatched cursor raises `ValueError`. This is what prevents losing lines appended between `log.read` and the socket connecting.
- **One shared `LogViewer` instance.** It lives on `app.state.log_viewer` and is shared by `log.read` (RPC) and `/ws/logs`; the RPC helper lazily creates and caches it if missing. The cursor handoff only works because both paths hit the same instance — never instantiate a `LogViewer` per request or per connection.
