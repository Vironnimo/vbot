# Process Tool

Manages background process sessions created by `bash`.

## Data Model

- Process session ids are distinct from chat Session ids.
- `ProcessManager` stores process sessions in memory by process `session_id`, scoped by Agent and Run.

## Interfaces

- Tool name: `process`
- Registration: `register_process_tool(registry, process_manager)`
- Schema: required `action`; optional `session_id`, `timeout_ms`, `offset`, `limit`, `data`, and `eof`.
- Actions: `list`, `poll`, `log`, `write`, `submit`, `kill`, `clear`.
- Display: summary fields `action` and `session_id`.
- `ProcessManager.spawn(scope_key, agent_id, argv, *, env, cwd) -> str`
- `ProcessManager.poll/log/write/submit/kill/clear(..., agent_id=...)`
- `ProcessManager.list_sessions(agent_id) -> list[ProcessSession]`
- `ProcessManager.cancel_scope(scope_key) -> None`

## Constraints & Gotchas

- Access is isolated by `ToolContext.agent_id`; missing and cross-agent sessions use not-found semantics.
- `cancel_scope(run.id)` kills all active processes started by tools in that Run.
- Combined output buffers are capped; `process log` returns a window from that buffer.
- `process poll` output is incremental since the previous poll.
- `waiting_for_input` is a best-effort hint only.
- All surfaced output is ANSI-stripped at the single decode boundary (`_decode` in `core/tools/process_manager.py`, via `core/utils/ansi.strip_ansi`): raw bytes stay in the buffer so byte offsets and the cap stay accurate, but the `poll`/`log` text the model and UI see has terminal escape/color sequences removed. This stops a model from copying escape codes into file writes and keeps output clean. Consequence: an agent cannot inspect *literal* terminal escape codes through process output — `read` the file directly if that is ever needed.
