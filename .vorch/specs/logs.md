# Logs

Read-only daily log viewer subsystem spanning backend parsing/watching and the WebUI Logs tab.

## Overview

The logs subsystem exposes application log files from `<data_dir>/logs/` for inspection in the WebUI. It owns daily file discovery, parsing the canonical log format into structured entries, and live updates for one selected file through a dedicated WebSocket. It does not write logs, edit log files, or reuse the shared app event bus. Filtering stays local in the WebUI after one file is loaded.

## Data Model

- Daily log catalog: `{ files: string[], default_file: string | null }`
- Read snapshot result: `{ file: string, entries: ParsedLogEntry[], cursor: string }`
- Parsed log entry:
  - `timestamp: string`
  - `level: string` — lower-cased level such as `info`, `warn`, `error`
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
  - `GET /ws/logs?file=<name>&cursor=<cursor>` — streams append/reset events for one selected file only and can replay the read→socket handoff gap
- WebUI
  - `listLogs()` / `readLogFile()` / `subscribeLogEvents()` in `webui/src/lib/api.js`
  - `webui/src/lib/logsView.js` owns client-side selection/filter/search helpers
  - `webui/src/components/LogsView.svelte` renders the tab and reconnects its dedicated log stream using the latest read cursor

## Conventions

- Treat the log format `timestamp [LEVEL] name - message` as the canonical parse contract.
- Validate file names strictly; never allow path traversal or absolute paths.
- If a line does not match the header format, append it to the previous entry's `continuation` when possible; otherwise keep it visible as an `unknown` entry.
- The level filter is based only on parsed `level`. Logger names are searchable through free-text search, not separate filter UI.
- `cursor` is an internal handoff token, not user-visible UI state.
- The selected file remains user-controlled. Refreshing the catalog may add newer files, but it must not auto-switch the active selection.

## External Dependencies

- `watchfiles` — watches the logs directory so `/ws/logs` can push file-backed live updates without polling.

## Constraints & Gotchas

- Newest-file selection assumes daily filenames sort newest-first lexicographically.
- Initial load reads one full selected daily file into memory.
- Windows watcher events may duplicate or coalesce changes; derive append/reset events from file snapshots rather than raw watcher event counts.
- If a file is truncated, replaced, or otherwise diverges from the previous parsed prefix, emit a `reset` event so the UI replaces its entry list.
