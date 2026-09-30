# Logs

Read-only daily log viewer subsystem spanning backend parsing/watching and the WebUI Logs tab.

## Overview

The logs subsystem exposes application log files from `<data_dir>/logs/` for inspection in the WebUI. It owns daily file discovery, parsing the canonical log format into structured entries, and live updates for one selected file through a dedicated WebSocket. It does not write logs, edit log files, or reuse the shared app event bus. Filtering stays local in the WebUI after one file is loaded.

## Data Model

- Daily log catalog: `{ files: string[], default_file: string | null }`
- Read snapshot result: `{ file: string, entries: ParsedLogEntry[], cursor: string }`
- Parsed log entry:
  - `timestamp: string`
  - `level: string` - lower-cased level such as `info`, `warn`, `error`; a line that doesn't match the header gets `level: "unknown"` with empty `timestamp`/`logger_name`
  - `logger_name: string`
  - `message: string`
  - `continuation: string` - multiline tail such as stack traces
  - `raw: string` - the source line(s) verbatim (continuation rows joined with `\n`), so an accessor can copy an entry exactly as written to the file; `message`/`level` are derived/normalized, `raw` is not
- Live stream entry event: `{ type: "append" | "reset", file: string, entries: ParsedLogEntry[] }`
- Live stream catalog event: `{ type: "catalog", file: string, files: string[], default_file: string | null }`
- Server transport keepalive: `{ type: "heartbeat", timestamp: string }` after 25 seconds without a delivered frame. It has no file, cursor, entries, or sequence and must not change the log projection; it is not a `LogViewer.subscribe()` data event.

## Interfaces

- `core/utils/log_viewer.py`
  - `await LogViewer.list_files()` -> `{ files, default_file }`
  - `await LogViewer.read_file(file_name)` -> `{ file, entries, cursor }`
  - `LogViewer.subscribe(file_name, cursor?)` -> async generator of entry and catalog events
- Server RPC
  - `log.list` - returns the daily log catalog sorted newest-first
  - `log.read { file }` - returns parsed entries plus a read cursor for one selected file
- Server transport
  - `GET /ws/logs?file=<name>&cursor=<cursor>` - streams append/reset events for the selected file plus catalog events when the directory's daily file list changes; with a cursor it first replays what changed since that `log.read` (a `reset` with the whole file when the file no longer starts with what the read covered). Without a cursor the stream starts at the file's current end. Invalid file name, missing file or malformed cursor -> close code `1008`.
- WebUI
  - `listLogs()` / `readLogFile()` / `subscribeLogEvents()` in `webui/src/lib/api.js`
  - `webui/src/lib/logsView.js` owns client-side selection/filter/search/sort helpers
  - `webui/src/components/LogsView.svelte` renders the tab and reconnects its dedicated log stream using the latest read cursor
  - `webui/src/components/logs/LogsEntry.svelte` renders one entry: a one-line summary of `message` with a line count for `continuation`, expandable in place to the full `message` + `continuation` body; its Copy action copies `raw`

## Conventions

- Treat the log format `timestamp [LEVEL] name - message` as the canonical parse contract.
- Validate file names strictly; never allow path traversal or absolute paths.
- If a line does not match the header format, append it to the previous entry's `continuation` when possible; otherwise keep it visible as an `unknown` entry.
- The level filter is based only on parsed `level`. Logger names are searchable through free-text search, not separate filter UI.
- Entry ordering is accessor-local UI state. Switching between newest-first and oldest-first must not trigger another `log.read` call for the same file.
- `cursor` is an opaque read token, not user-visible UI state.
- The selected file remains user-controlled. A live catalog event may add a newer file, but it must not auto-switch a still-valid active selection; if the selected file disappeared, the WebUI falls back to the catalog default and starts that file's read/stream flow.
- The Logs toolbar uses the shared simple dropdown style for file, level, and order controls.
- Routine lifecycle noise of the server's websocket routes (`/ws`, `/ws/logs`, `/ws/terminals/{terminal_id}`, `/ws/live/{call_id}`: connection open/closed, accept) is filtered on two layers: at write time by the logging pipeline (`is_logs_websocket_lifecycle_record`, so it never lands in daily files going forward) and again at read/stream time in `parse_log_entries` (`_should_include_entry`, so pre-existing matching rows don't surface in `log.read` results or `/ws/logs` events). Handshake rejections (e.g. 403) and genuine websocket transport failures stay visible; a Live path is written as `/ws/live/{call_id}` because its segment is a Provider call id.

## External Dependencies

- `watchfiles` - `awatch` watches the whole logs *directory* (`recursive=False`) in forced-polling mode (`force_polling=True`, `poll_delay_ms=50`, `debounce=100`), not the single selected file. Timeouts are yielded for reliable watcher teardown. Empty batches compare only selected-file and log-directory metadata before lock acquisition; unchanged metadata stops there, while changed metadata triggers targeted snapshot or catalog reconciliation. Live append/reset events are derived from re-read file snapshots, while a changed sorted filename tuple emits a catalog event even when the selected file did not change - this polling setup is deliberate for reliable Windows behavior and naturally covers daily rollover.

## Constraints & Gotchas

- Newest-file selection assumes daily filenames sort newest-first lexicographically.
- Initial load reads one full selected daily file into memory.
- **No log I/O on the Event Loop.** Directory listings, stats, file reads and parsing run on the two-worker `log-viewer` pool (`_LOG_WORKERS`), including the watcher's per-tick metadata checks and re-reads, which reach about ten per second while the server logs. Cursor digests are computed there too; watcher state and event fan-out stay on the Event Loop.
- Windows watcher events may duplicate or coalesce changes; derive append/reset events from file snapshots rather than raw watcher event counts.
- If a file is truncated, replaced, or otherwise diverges from the previous parsed prefix, emit a `reset` event so the UI replaces its entry list.
- **Watcher lifecycle is per-file and ref-counted.** One watcher task per file, shared by all subscribers; it starts on the first subscriber and stops when the last one leaves. `aclose()` (called on server shutdown with a 1 s timeout) tears down every watcher. Snapshots are read and diffed under a single async lock, and that same lock serializes watcher lookup/creation, subscriber registration and watcher retirement: `subscribe` finds or starts the watcher and appends its queue in one lock scope, so the last old subscriber leaving cannot evict the watcher a joining subscriber is about to use (a split would leave that subscriber on a dead watcher with no updates). `_ensure_watcher` therefore requires the caller to hold `_watch_lock`. Regression: `tests/core/utils/test_log_viewer.py`.
- **The read cursor is the no-gap guarantee, and it is stateless.** `read_file` returns `v1.<byte length>.<sha256 hex>`: the number of bytes it parsed and a digest of the file name plus those bytes. The server keeps nothing per read, so concurrent readers of one file (tabs, `vbot logs read`) never displace each other, a cursor can be used any number of times, and it survives a server restart. `subscribe(cursor)` re-reads the file (it does so anyway), checks that the file still starts with those bytes, re-parses that prefix to rebuild what the read returned and emits the missed append/reset diff *before* live events; when the check fails (truncated, rotated, rewritten, or another file's cursor) it emits a `reset` with the whole file instead of failing. Only a malformed cursor raises `ValueError`. `subscribe(cursor=None)` starts at the file's current end. This is what prevents losing lines appended between `log.read` and the socket connecting.
- A WebUI log-stream reconnect reloads the catalog before re-reading and re-subscribing, preserving a still-valid selection and discovering files created while disconnected. A catalog or file-read failure during that recovery keeps the view in `reconnecting` and schedules the next bounded-backoff attempt; only selection change, teardown, or a successful replacement stream ends that retry chain.
- **One shared `LogViewer` instance.** It lives on `app.state.log_viewer` and is shared by `log.read` (RPC) and `/ws/logs`; the RPC helper lazily creates and caches it if missing. Cursors need no shared state, but watchers do: one watcher per file serves every subscriber only because all sockets hit the same instance - never instantiate a `LogViewer` per request or per connection.
