# Process Tool

Manages background process sessions created by `bash`.

## Data Model

- Process session ids are distinct from chat Session ids.
- `ProcessManager` stores process sessions in memory by process `session_id`, scoped by Agent and Run.
- Runtime injects Storage's `TemporaryFileManager`. Every process session then leases one file under `<data_dir>/artifacts/temp/bash/` and writes combined output incrementally — decoded via an incremental UTF-8 decoder (chunk-split multibyte safe), ANSI-stripped, and flushed per chunk so the file is readable while the process runs. The file is the *complete* record and is not subject to the in-memory buffer cap. `ProcessSession.log_file` exposes the path (`None` without a temporary-file manager or after a write error, which disables file logging for that session best-effort). The lease remains active through process exit and both stream readers, then starts Storage's 72-hour retention; ProcessManager no longer owns file cleanup.

## Interfaces

- Tool name: `process`
- Registration: `register_process_tool(registry, process_manager)`
- Schema: required `action`; optional `session_id`, `timeout_ms`, `offset`, `limit`, `data`, and `eof`.
- Actions: `list`, `poll`, `log`, `write`, `submit`, `kill`, `clear`. `list` entries include `log_file` (path or `null`).
- Display: summary fields `action` and `session_id`.
- `ProcessManager.spawn(scope_key, agent_id, argv, *, env, cwd) -> str`
- `subprocess_creation_flags(*, new_process_group=False, platform_name=os.name) -> int` is the shared child-process launch policy for core Tools: zero on non-Windows; `CREATE_NO_WINDOW` on Windows, optionally combined with `CREATE_NEW_PROCESS_GROUP`.
- `ProcessManager.poll/log/write/submit/kill/clear(..., agent_id=...)`
- `ProcessManager.list_sessions(agent_id) -> list[ProcessSession]`
- `ProcessManager.cancel_scope(scope_key) -> None`

## Constraints & Gotchas

- Access is isolated by `ToolContext.agent_id`; missing and cross-agent sessions use not-found semantics.
- `cancel_scope(run.id)` kills all active processes started by tools in that Run.
- Combined output buffers are capped; `process log` returns a window from that buffer.
- Core Tool subprocesses must use `subprocess_creation_flags`: the managed command, Bash environment probe, ripgrep search, and Windows `taskkill` helpers stay windowless when vBot runs without a parent console, while managed commands retain their separate process group for tree cancellation.
- The in-memory finished-Session TTL and the complete-output file retention are separate: evicting a `ProcessSession` does not remove its 72-hour temporary log.
- `process poll` output is incremental since the previous poll.
- `waiting_for_input` is a best-effort hint only.
- All surfaced output is ANSI-stripped at the single decode boundary (`_decode` in `core/tools/process_manager.py`, via `core/utils/ansi.strip_ansi`): raw bytes stay in the buffer so byte offsets and the cap stay accurate, but the `poll`/`log` text the model and UI see has terminal escape/color sequences removed. This stops a model from copying escape codes into file writes and keeps output clean. Consequence: an agent cannot inspect *literal* terminal escape codes through process output — `read` the file directly if that is ever needed.
