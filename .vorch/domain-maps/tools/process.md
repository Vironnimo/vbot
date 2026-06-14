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
