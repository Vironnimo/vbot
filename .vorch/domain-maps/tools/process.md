# Process Tool

Lets an Agent inspect and control only its own background Process Sessions created by the `bash` Tool.

## Data Model

- Process Session ids are distinct from Chat Session ids and identify only commands that `bash` started through `ProcessManager`; the Tool cannot discover or control arbitrary operating-system processes. Every spawned Process Session starts with a raw stdin pipe and tracks the definitive `stdin_open` state until EOF, kill, or process exit closes it.
- `ProcessManager` stores Process Sessions in memory by process `session_id`, scoped by Agent and Run.
- A handed-off Bash process may carry one tracked automatic completion-notification task plus a manual-acknowledgement bit. `register_completion_notification(...)` attaches the watcher task; `acknowledge_completion(...)` requires a terminal process, records manual delivery, and cancels the watcher. If the watcher already submitted its result to the shared completion coordinator, cancellation withdraws that still-pending notice.
- Runtime injects Storage's `TemporaryFileManager`. Every Process Session then leases one file under `<data_dir>/artifacts/temp/bash/` and writes combined output incrementally — decoded via an incremental UTF-8 decoder (chunk-split multibyte safe), ANSI-stripped, and flushed per chunk so the file is readable while the process runs. The file is the complete record and is not subject to the in-memory buffer cap. `ProcessSession.log_file` exposes the path (`None` without a temporary-file manager or after a write error, which disables file logging for that session best-effort). The lease remains active through process exit and both stream readers, then starts Storage's 72-hour retention; ProcessManager does not own file cleanup.

## Interfaces

- Tool name: `process`
- Registration: `register_process_tool(registry, process_manager)`
- Schema: one flat model-facing object with required `action` (`status`, `input`, or `kill`) and sibling `session_id`, `text`, `newline`, and `eof` properties. It omits `additionalProperties` and Boolean JSON Schema defaults; descriptions state that `session_id` is required for `input`/`kill`, `text` is required for `input`, and omitted `newline`/`eof` use `true`/`false`. The handler remains authoritative for action-specific required and inapplicable fields.
- `status` without `session_id` lists the Agent's tracked Process Sessions. `status` with `session_id` returns an immediate, non-blocking, non-consuming snapshot with lifecycle timestamps, definitive `stdin_open`, best-effort `waiting_for_input`, a newest-output tail capped at 30,000 characters, truncation state, and `log_file`; list summaries also carry `stdin_open`.
- `input` requires `session_id` and `text`, writes raw UTF-8 to the Process Session's stdin pipe, appends a newline by default, and may close stdin with `eof: true`. It does not provide a terminal/TTY. `newline: false` sends the text exactly; an empty call that neither appends a newline nor closes stdin is rejected. On Windows `pwsh` is non-interactive, so PowerShell host prompts such as `Read-Host` are unavailable; a command expecting later Process input must use `[Console]::In.ReadLine()`, `[Console]::In.ReadToEnd()`, or a native child process.
- `kill` requires `session_id` and stops that Process Session.
- The Tool description tells the Agent to use the `session_id` returned by a handed-off `bash` call, explicitly excludes arbitrary operating-system processes, explains that Bash `output` is only a capped snapshot while `log_file` receives the complete combined stdout/stderr stream live through exit, and says that normal completion is delivered automatically.
- Display: the summary builder renders the flat `action` plus `session_id` when present.
- `ProcessManager.spawn(scope_key, agent_id, argv, *, env, cwd) -> str`
- `subprocess_creation_flags(*, new_process_group=False, platform_name=os.name) -> int` is the shared child-process launch policy for core Tools: zero on non-Windows; `CREATE_NO_WINDOW` on Windows, optionally combined with `CREATE_NEW_PROCESS_GROUP`.
- `ProcessManager.poll(...)` and `ProcessManager.log(...)` remain internal Bash lifecycle/output primitives and are not public Process actions.
- `ProcessManager.snapshot(session_id, agent_id)` provides the public non-consuming status view; `ProcessManager.send_input(session_id, agent_id, text, *, newline, eof)` owns stdin delivery; `ProcessManager.kill(session_id, agent_id)` owns termination.
- `ProcessManager.list_sessions(agent_id) -> list[ProcessSession]`
- `ProcessManager.cancel_scope(scope_key) -> None`

## Constraints & Gotchas

- Access is isolated by `ToolContext.agent_id`; missing and cross-agent sessions use not-found semantics.
- `cancel_scope(run.id)` kills all active processes started by tools in that Run.
- Combined in-memory output buffers are capped. Detailed `status` returns the newest output tail without advancing Bash's internal polling cursor and points to `log_file` for the complete retained record when available.
- Core Tool subprocesses must use `subprocess_creation_flags`: the managed command, Bash environment probe, ripgrep search, and Windows `taskkill` helpers stay windowless when vBot runs without a parent console, while managed commands retain their separate process group for tree cancellation.
- The in-memory finished-Session TTL and the complete-output file retention are separate: evicting a `ProcessSession` does not remove its 72-hour temporary log.
- The TTL sweeper removes old terminal Process Sessions automatically; there is no public clear action.
- A terminal detailed `status` and a successful `kill` register their manual completion acknowledgement through `ToolContext.after_result_persisted`; merely returning from the handler is not delivery. This persistence boundary preserves automatic completion if Chat fails before the manual Tool Result reaches Session history, while cancelling a pending coalesced notice when persistence succeeds.
- `waiting_for_input` is a best-effort hint only.
- Raw stdin support is not interactive-terminal support: there is no PTY/ConPTY, terminal resize, host prompt, or TUI contract. Add a separate explicit terminal mode when that capability is implemented; do not weaken the non-interactive Bash default or imply terminal semantics through `process.input`.
- All surfaced output is ANSI-stripped at the single decode boundary (`_decode` in `core/tools/process_manager.py`, via `core/utils/ansi.strip_ansi`): raw bytes stay in the buffer so byte offsets and the cap stay accurate, but the text the Agent and UI see has terminal escape/color sequences removed. This stops an Agent from copying escape codes into file writes and keeps output clean. Consequence: an Agent cannot inspect literal terminal escape codes through Process output — `read` the file directly if that is ever needed.
