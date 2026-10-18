# Logs

Read-only daily log viewer subsystem spanning backend parsing/watching and the WebUI Logs tab.

## Overview

The logs subsystem exposes server log files from `<data_dir>/logs/` for inspection in the WebUI. A packaged server also exposes its installation's `<install root>/logs/` under the `application/` namespace, including update phases and packaged CLI logs. `server/_log_sources.py` discovers the running installation off the Event Loop and includes it only when its recorded server data directory matches this Runtime; source checkouts and unrelated server instances expose no installation logs. It owns daily file discovery, parsing the canonical log format into structured entries, and live updates for one selected file through a dedicated WebSocket. It does not write logs, edit or delete log files, or reuse the shared app event bus; the writing pipeline deletes old daily files (`logging/pipeline.md` -> Retention), and open views see that as a catalog event. Every read and event carries a bounded page of entries; filtering stays local in the WebUI over the entries it has loaded of one file.

## Data Model

- Log catalog: `{ files: string[], default_file: string | null }` - the daily files (names that start with their date) newest first, then the directory's other files by name, such as the crash log and its previous generation (`logging/pipeline.md` -> Crash logs); `default_file` is the newest daily file, or the first file when there is none.
- Read result: `{ file: string, entries: ParsedLogEntry[], next_before: int | null, cursor?: string }` - one page in file order: the newest `LOG_PAGE_ENTRIES` (500) visible entries that start before the page end. `next_before` is the byte offset to read the next older page before, `null` at the file start. Only a newest-page read (no `before`) carries `cursor`.
- Parsed log entry:
  - `offset: int` - byte offset of the entry's first line. Unique and increasing within one file generation; it is the paging position and the WebUI row key.
  - `timestamp: string`
  - `level: string` - lower-cased level such as `info`, `warn`, `error`; a line that doesn't match the header gets `level: "unknown"` with empty `timestamp`/`logger_name`
  - `logger_name: string`
  - `message: string`
  - `continuation: string` - multiline tail such as stack traces
  - `raw: string` - the source line(s) verbatim (continuation rows joined with `\n`), so an accessor can copy an entry exactly as written to the file; `message`/`level` are derived/normalized, `raw` is not
- Live stream `reset`: `{ type: "reset", file: string, entries: ParsedLogEntry[], next_before: int | null }` - replaces the reader's entries with the newest page.
- Live stream `append`: `{ type: "append", file: string, from_offset: int, entries: ParsedLogEntry[] }` - the reader drops its entries with `offset >= from_offset` (the last entry, which was still open: a partial line or a traceback being written), then appends `entries`.
- Live stream catalog event: `{ type: "catalog", file: string, files: string[], default_file: string | null }`
- Server transport keepalive: `{ type: "heartbeat", timestamp: string }` after 25 seconds without a delivered frame. It has no file, cursor, entries, or sequence and must not change the log projection; it is not a `LogViewer.subscribe()` data event.

## Interfaces

- `core/utils/log_viewer.py`
  - `await LogViewer.list_files()` -> `{ files, default_file }`
  - `await LogViewer.read_file(file_name, *, before=None)` -> the newest page with a `cursor`, or with `before` the page ending there without one
  - `LogViewer.subscribe(file_name, cursor?)` -> async generator of entry and catalog events
- Server RPC
  - `log.list` - returns the log catalog
  - `log.read { file, before? }` - returns one page of parsed entries; without `before` the newest page plus a read cursor. `before` is a positive byte offset, normally a result's `next_before`; one greater than the file size -> `invalid_request`.
- CLI: `vbot log list|read` (`cli/log_management.py`; paging: `cli/system-commands.md`).
- Server transport
  - `GET /ws/logs?file=<name>&cursor=<cursor>` - streams append/reset events for the selected file plus catalog events when the directory's daily file list changes; with a cursor it first replays what changed since that `log.read` (a `reset` with the newest page when the file no longer starts with what the read covered). Without a cursor the stream starts at the file's current end. Invalid file name, missing file or malformed cursor -> close code `1008`. When the file's watcher stops unexpectedly the stream ends and the socket closes with `1011`; the WebUI's reconnect then reads again and resubscribes to a fresh watcher. A stream file read that fails with another `OSError` (a share lock or virus scanner while the stream starts or resyncs) closes the socket with `1011` too and logs one WARNING without traceback (`tests/server/test_websocket_logs.py`).
- WebUI
  - `listLogs()` / `readLogFile()` / `readOlderLogEntries(file, before)` / `subscribeLogEvents()` in `webui/src/lib/api.js`
  - `webui/src/lib/logsView.js` owns the bounded entry window (`logs/webui-tab.md`), paging, and client-side selection/filter/search/sort helpers
  - `webui/src/components/LogsView.svelte` renders the tab and reconnects its dedicated log stream using the latest read cursor

## Conventions

- Treat the log format `timestamp [LEVEL] name - message` as the canonical parse contract.
- Validate file names strictly; only configured namespace prefixes are accepted. Never allow path traversal, absolute paths, Windows alternate streams, linked files or linked log directories. Cursors include the full namespace-qualified name, so identical basenames in different sources never share a cursor.
- If a line does not match the header format, append it to the previous entry's `continuation` when possible; otherwise keep it visible as an `unknown` entry.
- The level filter is based only on parsed `level`. Logger names are searchable through free-text search, not separate filter UI.
- Entry ordering is accessor-local UI state. Switching between newest-first and oldest-first must not trigger another `log.read` call for the same file.
- `cursor` is an opaque read token, not user-visible UI state.
- The selected file remains user-controlled. A live catalog event may add a newer file, but it must not auto-switch a still-valid active selection; if the selected file disappeared, the WebUI falls back to the catalog default and starts that file's read/stream flow.
- Routine lifecycle noise of the server's websocket routes is kept out of daily files at write time (`logging/pipeline.md` -> uvicorn) and hidden again at read/stream time by the parser (`_should_include_entry`, the same `is_routine_websocket_lifecycle_message` predicate), so pre-existing matching rows don't surface in `log.read` results or `/ws/logs` events. Hidden entries still count for tail positions but never for page sizes.

## External Dependencies

- `watchfiles` - `awatch` polls every registered logs directory (forced polling, deliberate for reliable Windows behavior; it also covers daily rollover), not the selected file (`logs/live-stream.md`).

## Constraints & Gotchas

- Newest-file selection assumes daily filenames sort newest-first lexicographically.
- **Every read is bounded.** A page read walks backwards from its end in a range that starts at 64 KiB and doubles until it holds a page, reaches the file start, or reaches `LOG_READ_MAX_BYTES` (4 MiB); a page cut by that cap sets `next_before` to its oldest parsed entry, and a capped range without any entry start becomes one `unknown` entry so paging always advances. `LOG_PAGE_ENTRIES = 500` keeps a page at a few hundred KiB of JSON and milliseconds of parsing while covering several screens, and it bounds what any reset or append carries.
- **Live stream.** One ref-counted watcher task per file serves every subscriber; append/reset events derive from the file's tail state, never from raw watcher event counts, and only the still-open last entry is re-read. Subscriber queues are bounded (`LOG_SUBSCRIBER_QUEUE_SIZE`); a subscriber that overflows gets one `reset` instead of its backlog. A watcher that dies ends its streams (close `1011`) so clients reconnect instead of idling on heartbeats, and a poll that cannot read the log is skipped, not fatal. Mechanics: `logs/live-stream.md`.
- **No log I/O on the Event Loop.** Directory listings, stats, file reads, parsing and cursor digests run on the two-worker `log-viewer` pool (`_LOG_WORKERS`), including the watcher's per-tick metadata checks and re-reads, which reach about ten per second while the server logs. Watcher state and event fan-out stay on the Event Loop.
- **The read cursor is the no-gap guarantee, and it is stateless.** A newest-page `read_file` returns an opaque `v1.<byte length>.<sha256 hex>` cursor; the server keeps nothing per read, so concurrent readers of one file never displace each other, a cursor can be used any number of times, and it survives a server restart. `subscribe(cursor)` emits what was appended since that read, or a `reset` with the newest page when the file no longer matches (truncated, rotated, rewritten, another file's cursor), *before* live events; only a malformed cursor raises `ValueError`. `subscribe(cursor=None)` starts at the file's current end. This prevents losing lines appended between `log.read` and the socket connecting (mechanism: `logs/live-stream.md`).
- **One shared `LogViewer` instance.** It lives on `app.state.log_viewer` and is shared by `log.read` (RPC) and `/ws/logs`; the RPC helper lazily creates and caches it if missing. Cursors need no shared state, but watchers do: one watcher per file serves every subscriber only because all sockets hit the same instance - never instantiate a `LogViewer` per request or per connection.

## References

Read these only when your task matches - not by default.

- Changing the file watcher, tail advance, subscriber queues, watcher lifecycle, the read cursor or the `/ws/logs` stream lifecycle -> `logs/live-stream.md`
- Changing the WebUI Logs tab's entry window, paging, mounting or stream reconnect -> `logs/webui-tab.md`
