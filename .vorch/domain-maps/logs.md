# Logs

Read-only daily log viewer subsystem spanning backend parsing/watching and the WebUI Logs tab.

## Overview

The logs subsystem exposes application log files from `<data_dir>/logs/` for inspection in the WebUI. It owns daily file discovery, parsing the canonical log format into structured entries, and live updates for one selected file through a dedicated WebSocket. It does not write logs, edit or delete log files, or reuse the shared app event bus; the writing pipeline deletes old daily files (Retention in `logging.md`), and open views see that as a catalog event. Every read and event carries a bounded page of entries; filtering stays local in the WebUI over the entries it has loaded of one file.

## Data Model

- Daily log catalog: `{ files: string[], default_file: string | null }`
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
  - `log.list` - returns the daily log catalog sorted newest-first
  - `log.read { file, before? }` - returns one page of parsed entries; without `before` the newest page plus a read cursor. `before` is a positive byte offset, normally a result's `next_before`; one greater than the file size -> `invalid_request`.
- CLI: `vbot log read` pages older with `before` until `--limit` entries match, or through the file start for `--limit 0` (`cli/log_management.py`).
- Server transport
  - `GET /ws/logs?file=<name>&cursor=<cursor>` - streams append/reset events for the selected file plus catalog events when the directory's daily file list changes; with a cursor it first replays what changed since that `log.read` (a `reset` with the newest page when the file no longer starts with what the read covered). Without a cursor the stream starts at the file's current end. Invalid file name, missing file or malformed cursor -> close code `1008`. When the file's watcher stops unexpectedly the stream ends and the socket closes with `1011`; the WebUI's reconnect then reads again and resubscribes to a fresh watcher.
- WebUI
  - `listLogs()` / `readLogFile()` / `readOlderLogEntries(file, before)` / `subscribeLogEvents()` in `webui/src/lib/api.js`
  - `webui/src/lib/logsView.js` owns the bounded entry window, paging, and client-side selection/filter/search/sort helpers
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
- Routine lifecycle noise of the server's websocket routes (`/ws`, `/ws/logs`, `/ws/terminals/{terminal_id}`, `/ws/live/{call_id}`: connection open/closed, accept) is filtered on two layers: at write time by the logging pipeline (`is_logs_websocket_lifecycle_record`, so it never lands in daily files going forward) and again at read/stream time by the parser (`_should_include_entry`, so pre-existing matching rows don't surface in `log.read` results or `/ws/logs` events). Hidden entries still count for tail positions but never for page sizes. Handshake rejections (e.g. 403) and genuine websocket transport failures stay visible; a Live path is written as `/ws/live/{call_id}` because its segment is a Provider call id.

## External Dependencies

- `watchfiles` - `awatch` watches the whole logs *directory* (`recursive=False`) in forced-polling mode (`force_polling=True`, `poll_delay_ms=50`, `debounce=100`), not the single selected file. Timeouts are yielded for reliable watcher teardown. Empty batches compare only selected-file and log-directory metadata before lock acquisition; unchanged metadata stops there, while changed metadata triggers a targeted tail advance or catalog reconciliation. Live append/reset events are derived by advancing the file's tail (below), while a changed sorted filename tuple emits a catalog event even when the selected file did not change - this polling setup is deliberate for reliable Windows behavior and naturally covers daily rollover.

## Constraints & Gotchas

- Newest-file selection assumes daily filenames sort newest-first lexicographically.
- **Every read is bounded.** A page read walks backwards from its end in a range that starts at 64 KiB and doubles until it holds a page, reaches the file start, or reaches `LOG_READ_MAX_BYTES` (4 MiB); a page cut by that cap sets `next_before` to its oldest parsed entry, and a capped range without any entry start becomes one `unknown` entry so paging always advances. `LOG_PAGE_ENTRIES = 500` keeps a page at a few hundred KiB of JSON and milliseconds of parsing while covering several screens, and it bounds what any reset or append carries.
- **The live tail re-reads only the open entry.** `_LogTail` keeps the file's size, mtime, identity (`st_dev`, `st_ino`) and `open_start`, where its last entry starts. A change parses only `[open_start, EOF)` and emits `append` with `from_offset`, so a partial last line or a multi-line entry still being written is re-parsed until complete. A `reset` with the newest page is emitted only when the file appears or disappears, shrinks, is replaced (identity change), or the unread span exceeds `LOG_READ_MAX_BYTES`; more than a page of new entries at once also resets instead of appending. An in-place rewrite that keeps or grows the size is not detected.
- **Subscriber queues are bounded** (`LOG_SUBSCRIBER_QUEUE_SIZE = 32`, a few seconds of watcher events). A subscriber without room for a tick's events loses its backlog for one `_Resync` marker; it then receives the dropped catalog (if any) and one `reset` read at the watcher's tail of that moment, so later events apply in order. A queued end marker (below) survives that drop.
- **The WebUI keeps a bounded window.** `logsView.js` holds at most `LOGS_WINDOW_MAX_ENTRIES = 5000` entries in a `LogEntryWindow` class instance, which Svelte does not deep-proxy; every change bumps `viewState.revision`, which the view's deriveds read. Appends update the entries, the filtered `visible` list and level counts in place; only filter changes refilter. The view mounts visible entries from `renderFrom` on: the newest `LOGS_RENDER_STEP = 250` at first, plus live arrivals; "Load older entries" mounts 250 more loaded rows before it reads the page before `nextBefore`. Live appends past 5000 + 250 drop the oldest entries back to 5000 and move `nextBefore`; loading older stops at 5000. Older-page results for a replaced, trimmed or switched window are dropped.
- **No log I/O on the Event Loop.** Directory listings, stats, file reads, parsing and cursor digests run on the two-worker `log-viewer` pool (`_LOG_WORKERS`), including the watcher's per-tick metadata checks and re-reads, which reach about ten per second while the server logs. Watcher state and event fan-out stay on the Event Loop.
- Windows watcher events may duplicate or coalesce changes; derive append/reset events from the file's tail state rather than raw watcher event counts.
- **Watcher lifecycle is per-file and ref-counted.** One watcher task per file, shared by all subscribers; it starts on the first subscriber and stops when the last one leaves. `aclose()` (called on server shutdown with a 1 s timeout) tears down every watcher. The tail is advanced under a single async lock, and that same lock serializes watcher lookup/creation, subscriber registration and watcher retirement: `subscribe` finds or starts the watcher and appends its queue in one lock scope, so the last old subscriber leaving cannot evict the watcher a joining subscriber is about to use (a split would leave that subscriber on a dead watcher with no updates). `_ensure_watcher` therefore requires the caller to hold `_watch_lock`. Regression: `tests/core/utils/test_log_viewer.py`.
- **A watcher that dies must end its streams.** Heartbeats keep an idle `/ws/logs` socket open, so a subscriber left on a dead watcher would look connected and never reconnect. A watch loop that ends before it was stopped (a crash) therefore retires its watcher under `_watch_lock`: it is unregistered (only if still the registered one) and every subscriber queue is emptied and gets an end marker (`None`), so even a full bounded queue ends, `subscribe` returns and the server closes the socket (`1011`). `_ensure_watcher` never hands out a watcher whose task is done, and a subscriber leaving a retired watcher never touches its successor for the same file. The crash itself is logged at ERROR by the task's done callback.
- **A poll that cannot read the log is skipped, not fatal.** An `OSError` from a metadata check or tail advance (Windows share locks, virus scanners) skips that poll; the tail is not moved, so the next poll sees the change again. A failure streak logs one WARNING and its recovery one INFO (`LoggedConditions`). Files decode as UTF-8 with replacement, so undecodable bytes show as U+FFFD instead of stopping the watcher or failing `log.read`.
- **The read cursor is the no-gap guarantee, and it is stateless.** A newest-page `read_file` returns `v1.<byte length>.<sha256 hex>`: the file size it read up to and a digest of the file name, a NUL byte and the last `_CURSOR_WINDOW_BYTES` (64 KiB) of those bytes (all of them for a smaller read). The server keeps nothing per read, so concurrent readers of one file (tabs, `vbot log read`) never displace each other, a cursor can be used any number of times, and it survives a server restart. `subscribe(cursor)` checks, in the same worker read that advances the watcher's tail, that the file is still at least that long and its window matches; it then rebuilds the reader's tail at the cursor's byte length (a backwards read for the last entry start, bounded like a page read) and emits the missed `append` - from the entry that was still open at the read - or `reset` *before* live events. When the check fails (truncated, rotated, rewritten within the window, or another file's cursor) it emits a `reset` with the newest page instead of failing. Only a malformed cursor raises `ValueError`. `subscribe(cursor=None)` starts at the file's current end. This is what prevents losing lines appended between `log.read` and the socket connecting.
  - Why a window rather than the whole covered prefix: hashing the prefix would make every `log.read` and every cursor subscribe read the whole file, which the bounded-read contract rules out. The window still catches truncation, rotation, replacement and cross-file cursors; it misses only a rewrite confined to bytes before the window, which append-only daily logs never do. Up to 64 KiB the digest covers the whole read, as the first `v1` cursors did; a longer file's older-format cursor simply fails the check and resets.
- A WebUI log-stream reconnect reloads the catalog before re-reading and re-subscribing, preserving a still-valid selection and discovering files created while disconnected. A catalog or file-read failure during that recovery keeps the view in `reconnecting` and schedules the next bounded-backoff attempt; only selection change, teardown, or a successful replacement stream ends that retry chain.
- **One shared `LogViewer` instance.** It lives on `app.state.log_viewer` and is shared by `log.read` (RPC) and `/ws/logs`; the RPC helper lazily creates and caches it if missing. Cursors need no shared state, but watchers do: one watcher per file serves every subscriber only because all sockets hit the same instance - never instantiate a `LogViewer` per request or per connection.
