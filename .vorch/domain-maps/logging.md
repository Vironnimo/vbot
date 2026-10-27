# Logging

Cross-cutting policy for what vBot writes to its application logs and at which level, plus the writing pipeline in `core/utils/logging.py`. Reading and displaying log files is the log viewer's job (`logs.md`).

## Overview

Every domain logs through per-module `vbot.<domain>` loggers into one pipeline: `LogManager` writes daily files `<data_dir>/logs/YYYY-MM-DD.log` in the canonical line format `timestamp [LEVEL] name - message` (parse contract: `logs.md`) and keeps them for 90 days and 500 MB at most (`logging/pipeline.md` -> Retention). The default level is INFO; the `LOG_LEVEL` configuration value (`core.utils.config.Config`: the server's process environment, or a `Config(overrides=...)` of a dev tool; no longer `.env`) selects DEBUG. The standalone Desktop writes the same format to its own config directory (`desktop/window.md` -> Standalone logging); the Windows application host, its update worker and every CLI command run on a packaged installation log under `<install root>/logs/` (`cli/application.md`). Fatal errors that end a process without a log record go to the server's and the Desktop's crash logs (`logging/pipeline.md` -> Crash logs).

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
- **Transitions, not repetitions.** A persistent condition (unreachable server, invalid configuration file, missing include, unsupported SQLite build) logs once when it starts and once when it ends - not on every read, build, poll or attempt. Retry loops log attempts at DEBUG and the final outcome once. Track such conditions with `core/utils/log_conditions.py` (`LoggedConditions`, bounded) rather than a private warn-once set. Events that outsiders can repeat at will, such as refused requests at the server edge, log the first occurrence per reason and then one summary line per interval (`access.md`).
- **Failures are never INFO.** An outcome field such as `outcome=failed` on an INFO line is a WARNING.

## Never log

At any level, including DEBUG and exception messages built by vBot:

- credentials, token values, OAuth codes, Provider Account ids; pairing codes, poll secrets and login tickets of the access domain, and request paths beyond their second segment where a capability or ticket can follow (`access.md`);
- Prompt, Skill, Cron, Memory and Workspace file content; Model output; user message text; titles generated from them;
- external conversation, chat, user, thread or Live call ids and platform display names (Channel platforms, Provider-assigned Live call or conversation ids), including ids derived from them. Provider-assigned Tool call ids are correlation ids within a stored Session and may be logged, and so may the id of a paid Provider job whose cost vBot could not record (a video job, `model_tasks/video.md`), since only it lets the charge be traced.

Use the vBot-owned id of the same object instead (`core/utils/ids.py`). Channel-derived Session ids (`ch-<channel id>-<platform part>`, `channels.md`) are the one derived form a line may carry: every Session id is logged as usual, and the pipeline writes the platform part as a pseudonym (`logging/pipeline.md` -> Pseudonymized Session ids). Channel code itself still names only the Channel id and conversation kinds.

asyncio Task names follow the same rules: they appear in asyncio's own log lines (an unretrieved Task exception) and in the `python -m asyncio ps` task listing (`performance/diagnosis.md`), which no formatter pseudonymizes. Long-lived Tasks are named `<purpose>:<vBot-owned id>` (for example `run:<run id>`, `mcp-call:<connection id>:<operation>`); a name never carries a platform id, Channel-derived Session id, resource URI, user content or credential.

## Constraints & Gotchas

- `vbot.*` loggers reach the daily files at the configured level. Other libraries' loggers (asyncio, httpx, Channel libraries) reach them only at WARNING and above, under their own logger name, through the router `LogManager` adds to the root logger - never through `logging.lastResort` on stderr. Windows' Proactor `_call_connection_lost` ConnectionResetError callback noise is written at DEBUG.
- The server process (`server/main.py`) owns the one start line and one stop line; the Runtime only contributes its startup summary, so a Runtime embedded without the server (scripts, tests) logs its lifecycle at DEBUG (`server.md`).
- Tests that assert log output own the behavior they assert (for example "no external id is logged"); do not add tests that merely pin a message's wording (`development/testing.md` -> Assertions and timing).

## References

Read these only when your task matches - not by default.

- Changing `core/utils/logging.py` (`LogManager`, retention, the formatter's Session-id pseudonymization, uvicorn and third-party routing), `core/utils/crash_logging.py`, or managed-server console logging (`VBOT_LOG_STDIO`), or diagnosing a server end without a stop line -> `logging/pipeline.md`
