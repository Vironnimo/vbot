# Logging

Cross-cutting policy for what vBot writes to its application logs and at which level, plus the writing pipeline in `core/utils/logging.py`. Reading and displaying log files is the log viewer's job (`logs.md`).

## Overview

Every domain logs through per-module `vbot.<domain>` loggers into one pipeline: `LogManager` writes daily files `<data_dir>/logs/YYYY-MM-DD.log` in the canonical line format `timestamp [LEVEL] name - message` (parse contract: `logs.md`) and keeps them for 90 days and 500 MB at most (Retention below). The default level is INFO; the `LOG_LEVEL` configuration value selects DEBUG. The standalone Desktop writes the same format to its own config directory (`desktop.md`); the Windows application host, its update worker and every CLI command run on a packaged installation log under `<install root>/logs/` (`cli/application.md`); a source-mode `vbot autostart enable|disable`, and the autostart unit refresh `vbot update` runs, log into the target's existing data directory.

This map is a policy. Code that logs differently is a finding, not a precedent: before changing a level or removing a line, check it against the purpose below, not only against the "silent" list.

## Purpose of INFO

The INFO log alone must let an operator reconstruct what the server did:

- which version ran, in which mode, and why each process started and stopped;
- what work finished and how: one terminal line per Run, per scheduled job firing, per update phase;
- which control-plane state changed, by whom, with stable ids and changed field names;
- which health transitions happened - degradation and recovery.

Everything that happens per step, per call, per attempt or per poll belongs at DEBUG. A quieter log that loses this thread is a regression, not a cleanup; a louder log that buries it is one too.

## Level rules

- **INFO** - process lifecycle (one start line, one stop line with the reason); Run terminal outcome; each material control-plane mutation, once, after the change; recovery back to healthy; rare significant events (update phases, schema evolution, data snapshots).
- **WARNING** - expected operational failures and degradation: a failed Run, a Provider or Channel that stops working, a scheduled job disabled after failures, an invalid user file that is ignored.
- **ERROR** - unexpected failures (with traceback) and events that risk or cause data loss (automatic restore after corruption, a Channel reply that could not be delivered).
- **DEBUG** - per-attempt retries, per-Tool-call statistics, request and stream traffic, reads, polls, acknowledgements, cache maintenance, Run starts, internal mechanics.
- **Silent** - effective no-ops, appearance and UI-selection changes.

## Conventions

- **Line format:** `<Event in past tense> (key=value key=value)` - stable vBot ids, changed field names (not values), counts, durations. Multi-value fields are comma-separated inside one value. `extra=` is not rendered by the formatter; everything an operator needs goes into the message.
- **One event, one line.** A "started" plus "completed" pair for one short operation is merged into the outcome line; the start moves to DEBUG.
- **Log at the owner.** A mutation logs in the core owner that performs it, so RPC, Tool, command and CLI paths are all covered; when more than one path can cause the change, an `actor=` field (for example `rpc`, `tool`, `command`, `agent=<id>`) says which one did; a single-path owner omits it. The RPC layer does not add a second line.
- **Transitions, not repetitions.** A persistent condition (unreachable server, invalid configuration file, missing include, unsupported SQLite build) logs once when it starts and once when it ends - not on every read, build, poll or attempt. Retry loops log attempts at DEBUG and the final outcome once. Track such conditions with `core/utils/log_conditions.py` (`LoggedConditions`, bounded) rather than a private warn-once set.
- **Failures are never INFO.** An outcome field such as `outcome=failed` on an INFO line is a WARNING.

## Never log

At any level, including DEBUG and exception messages built by vBot:

- credentials, token values, OAuth codes, Provider Account ids;
- Prompt, Skill, Cron, Memory and Workspace file content; Model output; user message text; titles generated from them;
- external conversation, chat, user, thread or Live call ids and platform display names (Channel platforms, Provider-assigned Live call or conversation ids), including ids derived from them. Provider-assigned Tool call ids are correlation ids within a stored Session and may be logged.

Use the vBot-owned id of the same object instead (`core/utils/ids.py`). Channel-derived Session ids (`ch-<channel id>-<platform part>`, `channels.md`) are the one derived form a line may carry: every Session id is logged as usual, and the pipeline writes the platform part as a pseudonym (Constraints & Gotchas). Channel code itself still names only the Channel id and conversation kinds.

asyncio Task names follow the same rules: they appear in asyncio's own log lines (an unretrieved Task exception) and in the `python -m asyncio ps` task listing, which no formatter pseudonymizes. Long-lived Tasks are named `<purpose>:<vBot-owned id>` (for example `run:<run id>`, `mcp-call:<connection id>:<operation>`); a name never carries a platform id, Channel-derived Session id, resource URI, user content or credential.

## Constraints & Gotchas

- **Retention.** Every directory the pipeline writes daily files to (`<data_dir>/logs/`, and `<install root>/logs/` of the Windows application) keeps them for `LOG_RETENTION_DAYS` (90) and at most `LOG_RETENTION_MAX_BYTES` (500 MB) in total; both are module constants in `core/utils/logging.py`, not Settings. Opening a day's file - a process's first record there and the first record of each new day - hands a sweep of that directory to the single `vbot-log-retention` thread, at most once per directory and day in the process. The sweep deletes daily files older than 90 days, then, oldest first, while all daily files together exceed 500 MB; files dated today or later are never deleted. Only regular files named exactly `YYYY-MM-DD.log` with a valid date count and are deleted; everything else in the directory (`server-startup.log`, `<id>-startup.log`, the extensionless daily files of older versions) stays untouched.
  - The sweep never runs on the writing thread (often the Event Loop) or inside a handler's emit, so its own log lines cannot block or re-enter a write. The thread is a plain executor, not a `core/utils/workers.py` pool: the pools measure through `core.performance`, which logs through this module.
  - Several processes (server, CLI, Windows application) may sweep one directory. A file another process removed counts as gone; one that cannot be deleted (on Windows, held open elsewhere) stays and is retried by the next sweep, at the next process start or day.
  - A sweep that deleted files logs one INFO line (`count`, `bytes`, `reason=age|size|age,size`, `oldest`/`newest` deleted day). A failing sweep warns once per streak and the streak's end logs one INFO (`LoggedConditions`).
  - Closing a `DailyFileHandler` or `LogManager` waits for the sweep it started (at most 5 s); the manager waits before detaching its outputs, so even a short CLI command writes the outcome into its log.
  - Open Logs views drop a deleted file through the directory watcher's catalog event (`logs.md`). The standalone Desktop's own daily files (`desktop.md`) are not covered.
  - Tests: `tests/core/utils/test_logging.py` (age, size cap, unrelated files, undeletable and vanishing files, streak warnings).
- `vbot.*` loggers reach the daily files at the configured level. Other libraries' loggers (asyncio, httpx, Channel libraries) reach them only at WARNING and above, under their own logger name, through the router `LogManager` adds to the root logger - never through `logging.lastResort` on stderr. Windows' Proactor `_call_connection_lost` ConnectionResetError callback noise is written at DEBUG.
- uvicorn is routed into `vbot.server.uvicorn` by `build_uvicorn_log_config`: its own INFO lifecycle lines drop to DEBUG, routine lifecycle lines of every server websocket route are filtered, handshake rejections stay visible, and a Live path is written without its call id (`logs.md`).
- Constructing a `LogManager` activates it; `close()` deactivates it. Managers nest: the newest open manager owns the outputs, and closing it hands them back to the manager it replaced. The Runtime opens its manager at startup and closes it at shutdown, so an unstarted Runtime leaves logging alone. The `logs` directory and daily file are created with the first record.
- The server process (`server/main.py`) owns the one start line and one stop line; the Runtime only contributes its startup summary, so a Runtime embedded without the server (scripts, tests) logs its lifecycle at DEBUG (`server.md`).
- Server processes launched by the CLI or the Windows application run with console logging off (`VBOT_LOG_STDIO=0`): their log lines go only to the daily file, and the captured stdout/stderr (`server-startup.log`) keeps only crash output. The CLI logs a start outcome and any stop that bypassed the server's shutdown into the target's daily file (`cli.md`).
- `_VBotFormatter` pseudonymizes Channel-derived Session ids in the whole formatted line, tracebacks and third-party lines included: `ch-<channel id>-<platform part>` becomes `ch-<channel id>-#<6 hex>`, or `ch-#<6 hex>` for a channel id the process has not registered; `ch-<channel id>-main` names no platform id and stays. `ChannelStorage` registers every channel id it reads or saves (`register_log_channel_ids`). The pseudonym is a truncated SHA-256 of everything after `ch-`, so it is identical in both forms, across processes and days, and greppable for one conversation. It cannot be reversed (each value matches hundreds of possible platform ids), but whoever already knows a platform id can confirm a match. Only the managed outputs are covered: `caplog` and other handlers see the raw record, so Channel tests still assert that Channel code logs no derived Session id.
- Tests that assert log output own the behavior they assert (for example "no external id is logged"); do not add tests that merely pin a message's wording (`PROJECT.md` -> Testing).
