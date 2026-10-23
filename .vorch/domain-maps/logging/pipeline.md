# Logging: pipeline

The writing pipeline in `core/utils/logging.py` and the process logging around it: `LogManager`, retention, crash logs, uvicorn routing, console logging of managed servers, Session-id pseudonymization, and server ends without a stop line. What to log at which level: `logging.md`.

## LogManager

Constructing a `LogManager` activates it; `close()` deactivates it. Managers nest: the newest open manager owns the outputs, and closing it hands them back to the manager it replaced. The Runtime opens its manager at startup and closes it at shutdown, so an unstarted Runtime leaves logging alone. The `logs` directory and daily file are created with the first record.

## Retention

Every directory the pipeline writes daily files to (`<data_dir>/logs/`, and `<install root>/logs/` of the Windows application) keeps them for `LOG_RETENTION_DAYS` (90) and at most `LOG_RETENTION_MAX_BYTES` (500 MB) in total; both are module constants in `core/utils/logging.py`, not Settings. Opening a day's file - a process's first record there and the first record of each new day - hands a sweep of that directory to the single `vbot-log-retention` thread, at most once per directory and day in the process. The sweep deletes daily files older than 90 days, then, oldest first, while all daily files together exceed 500 MB; files dated today or later are never deleted. Only regular files named exactly `YYYY-MM-DD.log` with a valid date count and are deleted; everything else in the directory (`server-startup.log`, `<id>-startup.log`, `server-crash.log` and its `.1` generation, the extensionless daily files of older versions) stays untouched.

- The sweep never runs on the writing thread (often the Event Loop) or inside a handler's emit, so its own log lines cannot block or re-enter a write. The thread is a plain executor, not a `core/utils/workers.py` pool: the pools measure through `core.performance`, which logs through this module.
- Several processes (server, CLI, Windows application) may sweep one directory. A file another process removed counts as gone; one that cannot be deleted (on Windows, held open elsewhere) stays and is retried by the next sweep, at the next process start or day.
- A sweep that deleted files logs one INFO line (`count`, `bytes`, `reason=age|size|age,size`, `oldest`/`newest` deleted day). A failing sweep warns once per streak and the streak's end logs one INFO (`LoggedConditions`).
- Closing a `DailyFileHandler` or `LogManager` waits for the sweep it started (at most 5 s); the manager waits before detaching its outputs, so even a short CLI command writes the outcome into its log.
- Open Logs views drop a deleted file through the directory watcher's catalog event (`logs.md`). The standalone Desktop's own daily files (`desktop/window.md` -> Standalone logging) are not covered.
- Tests: `tests/core/utils/test_logging.py` (age, size cap, unrelated files, undeletable and vanishing files, streak warnings).

## Crash logs

A native crash (an access violation or segmentation fault in sqlite-vec, winpty, onnxruntime, Pillow, ...), an abort or a fatal interpreter error ends the process without a log record or stop line. The server process (`server/main.py`, right after it creates its `LogManager`) therefore enables `faulthandler` (`all_threads=True`, `c_stack=True`) into `<data_dir>/logs/server-crash.log`, and the standalone Desktop (`desktop/main.py`) into `<config-dir>/logs/desktop-crash.log`. A dump holds every Python thread's stack with thread names and code locations only, and the crashing thread's C stack where the platform provides one (Linux; Windows prints `<cannot get C stack on this system>`).

- Each process start appends one line in the daily log format (`<local time> [INFO] vbot.server - Server process started (pid=<pid>)`, `vbot.desktop - Desktop process started` for the Desktop): a dump follows the line of the process that crashed, the daily log's start line carries the same pid, and the log viewer shows the dump as that line's continuation (`logs.md`).
- The file is appended across starts, so a dump survives the restart a supervisor makes after the crash. A start that finds it over 1 MiB first moves it to its single previous generation (`server-crash.log.1`), as the application does with `server-startup.log` (`cli/application.md`). The file stays open for the process lifetime: `faulthandler` keeps the file object referenced until the interpreter has finalized, where native libraries can still crash. A crash log that cannot be opened logs one WARNING, and the process runs without it.
- On Windows `faulthandler` sees an exception before native code's own handlers do: a dump of an exception the code then catches (a .NET null reference inside the Desktop's WebView2 host, a `ctypes` call that reports an access violation as `OSError`) is not followed by an end of the process; the daily log shows the process going on.
- Tests: `tests/server/test_main.py`, `tests/desktop/test_main.py`.

## Ends without a stop line

A server process killed from outside (`TerminateProcess`, a Job that ends its process tree, `kill -9`) or ended by a native crash writes no stop line. Three WARN lines in the daily log fill the gap: the next server start names the predecessor whose control record it took over (`Previous server process ended without shutting down (pid=...)`, `server/process-lifecycle.md`); restart recovery writes each recovered Run's terminal line (`Run interrupted (... cause=process_restart)`, `runs.md`); and on Windows the tray, as soon as it notices, writes `Server process ended without shutting down (pid=... started=... ended=... uptime=... exit_code=...)` on `vbot.application.server_exit` (`cli/application/tray.md`). Times are local with offset; a value the tray cannot know reads `unknown`. The exit code tells the cause: a kill shows the code its killer chose, a native crash a Windows status code in hexadecimal such as `0xC0000005` (access violation), whose dump is then in `server-crash.log` after the start line of the same pid.

## uvicorn

uvicorn is routed into `vbot.server.uvicorn` by `build_uvicorn_log_config`: `ManagedLoggerProxyHandler` forwards its own INFO lines at DEBUG, keeps a websocket handshake answered without accepting it (for example 403) at its level, and writes a Live path as `/ws/live/{call_id}` (`redact_live_websocket_path`), because its segment is a Provider call id. The proxy suppresses a traceback for the same exception Runtime already marked as logged during failed startup; unrelated server failures remain visible.

Routine lifecycle lines of every server websocket route (`/ws`, `/ws/logs`, `/ws/terminals/{terminal_id}`, `/ws/live/{call_id}`: INFO `connection open`/`connection closed` and `"WebSocket <path>" [accepted]`) never reach the daily files: the proxy's `QuietLogsWebSocketLifecycleFilter` drops each record `is_logs_websocket_lifecycle_record` matches. Genuine websocket transport failures stay visible. The predicate behind it, `is_routine_websocket_lifecycle_message`, is shared with the log viewer, which hides matching rows written before the filter existed (`logs.md` -> Conventions).

## Console logging of managed servers

Server processes launched by the CLI or the Windows application run with console logging off (`VBOT_LOG_STDIO=0`): their log lines go only to the daily file, and the captured stdout/stderr (`server-startup.log`) keeps only crash output. The CLI logs a start outcome and any stop that bypassed the server's shutdown into the target's daily file (`cli/server-lifecycle.md`); the tray uses the same path (`cli.server_management.log_bypassed_stop`, a briefly nested `LogManager` on the target's data directory) for a server end it observed.

## Pseudonymized Session ids

`_VBotFormatter` pseudonymizes Channel-derived Session ids in the whole formatted line, tracebacks and third-party lines included: `ch-<channel id>-<platform part>` becomes `ch-<channel id>-#<6 hex>`, or `ch-#<6 hex>` for a channel id the process has not registered; `ch-<channel id>-main` names no platform id and stays. `ChannelStorage` registers every channel id it reads or saves (`register_log_channel_ids`). The pseudonym is a truncated SHA-256 of everything after `ch-`, so it is identical in both forms, across processes and days, and greppable for one conversation. It cannot be reversed (each value matches hundreds of possible platform ids), but whoever already knows a platform id can confirm a match. Only the managed outputs are covered: `caplog` and other handlers see the raw record, so Channel tests still assert that Channel code logs no derived Session id.
