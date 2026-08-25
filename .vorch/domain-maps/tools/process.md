# Process Tool

Lets an Agent inspect and control only its own background processes created by the `bash` Tool.

## Terms

### Tracked Process
**Definition:** One OS subprocess spawned through `ProcessManager` (always by the `bash` Tool today), identified by an opaque `process_id` and tracked in memory until exit plus a TTL sweep. The Agent addresses it in Tool results and `process` actions exclusively as `process_id`.
**Not:** A Chat Session or its id - those are separate namespaces despite both being UUID-ish strings. Also not an arbitrary operating-system process: the Tool cannot discover or control anything `ProcessManager` did not spawn.

## Data Model

- `process_id` values are distinct from Chat Session ids and identify only commands that `bash` started through `ProcessManager`. Every tracked process starts with a raw stdin pipe and tracks the definitive `stdin_open` state until EOF, kill, or process exit closes it.
- `ProcessManager` stores tracked processes in memory by `process_id`, scoped by Agent and Run.
- A tracked process never outlives the server process that owns its in-memory record. `server.main` activates the shared OS containment boundary before Runtime startup: Windows descendants inherit a kill-on-close Job Object, while POSIX Process Manager children run through `core/tools/process_guardian.py` and receive server death through one parent-liveness pipe. systemd adds a service-cgroup boundary (see `cli.md`).
- A handed-off Bash process may carry one tracked automatic completion-notification task plus a manual-acknowledgement bit. `register_completion_notification(...)` attaches the watcher task; `acknowledge_completion(...)` requires a terminal process, records manual delivery, and cancels the watcher. If the watcher already submitted its result to the shared completion coordinator, cancellation withdraws that still-pending notice.
- Runtime injects Storage's `TemporaryFileManager`. Every tracked process then leases one file under `<data_dir>/artifacts/temp/bash/` and writes combined output incrementally - decoded via an incremental UTF-8 decoder (chunk-split multibyte safe), ANSI-stripped, and flushed per chunk so the file is readable while the process runs. The file is the complete record and is not subject to the in-memory buffer cap. `TrackedProcess.log_file` exposes the path (`None` without a temporary-file manager or after a write error, which disables file logging for that process best-effort). The lease remains active through process exit and both stream readers, then starts Storage's 72-hour retention; ProcessManager does not own file cleanup.

## Interfaces

- Tool name: `process`
- Registration: `register_process_tool(registry, process_manager)`
- Schema: one flat model-facing object with required `action` (`status`, `input`, or `kill`) and sibling `process_id`, `text`, `newline`, and `eof` properties. It omits `additionalProperties` and Boolean JSON Schema defaults; descriptions state that `process_id` is required for `input`/`kill`, `text` is required for `input`, and omitted `newline`/`eof` use `true`/`false`. The handler remains authoritative for action-specific required and inapplicable fields.
- `status` without `process_id` lists the Agent's tracked processes (result key `processes`). `status` with `process_id` returns an immediate, non-blocking, non-consuming snapshot with lifecycle timestamps, definitive `stdin_open`, best-effort `waiting_for_input`, a newest-output tail capped at 30,000 characters, truncation state, and `log_file`; list summaries also carry `stdin_open`. Model-facing `log_file` values use the shared forward-slash presentation while `TrackedProcess` retains the native `Path`.
- `input` requires `process_id` and `text`, writes raw UTF-8 to the process's stdin pipe, appends a newline by default, and may close stdin with `eof: true`. It does not provide a terminal/TTY. `newline: false` sends the text exactly; an empty call that neither appends a newline nor closes stdin is rejected. On Windows `pwsh` is non-interactive, so PowerShell host prompts such as `Read-Host` are unavailable; a command expecting later Process input must use `[Console]::In.ReadLine()`, `[Console]::In.ReadToEnd()`, or a native child process.
- `kill` requires `process_id` and stops that process.
- Failure codes: `process_not_found` (missing or cross-agent id) and `process_input_closed` (stdin unavailable).
- The Tool description tells the Agent to use the `process_id` returned by a handed-off `bash` call, explicitly excludes arbitrary operating-system processes, explains that Bash `output` is only a capped snapshot while `log_file` receives the complete combined stdout/stderr stream live through exit, and says that normal completion is delivered automatically.
- Display: the summary builder renders the flat `action` plus `process_id` when present. A successful `status` without an id derives an exact presentation-only `results` count from `processes`; single-process status, input, kill, and failures publish no count.
- `ProcessManager.spawn(scope_key, agent_id, argv, *, env, cwd) -> str`
- `subprocess_creation_flags(*, new_process_group=False, breakaway=False, platform_name=os.name) -> int` is the shared child-process launch policy for core Tools: zero on non-Windows; `CREATE_NO_WINDOW` on Windows, optionally combined with `CREATE_NEW_PROCESS_GROUP` and the explicitly requested Job breakaway used only by server lifecycle handoff.
- `activate_process_containment()` establishes the server-process boundary once; `guarded_process_launch(argv)` returns the exact ordinary argv when no POSIX boundary is active and otherwise returns the private guardian argv plus its inherited lifetime descriptor. `ProcessManager` and Terminal Backend share this seam so they cannot acquire different crash behavior.
- `ProcessManager.poll(...)` and `ProcessManager.log(...)` remain internal Bash lifecycle/output primitives and are not public Process actions.
- `ProcessManager.snapshot(process_id, agent_id)` provides the public non-consuming status view; `ProcessManager.send_input(process_id, agent_id, text, *, newline, eof)` owns stdin delivery; `ProcessManager.kill(process_id, agent_id)` owns termination.
- `ProcessManager.cancel_for_user(process_id, agent_id) -> TrackedProcess` terminates a running process through the same kill machinery while retaining `cancelled_by_user: true` on the in-memory record; handed-off Bash completion delivery uses that fact for explicit user-abort wording. The Chat Activity surface exposes this through `chat.cancel_process` (params `agent_id` + `process_id`) with an addressed Agent and exact process id; it does not acknowledge or suppress the automatic completion notice.
- `ProcessManager.list_processes(agent_id) -> list[TrackedProcess]`
- `ProcessManager.cancel_scope(scope_key) -> None` kills a run scope synchronously (shutdown paths only); `cancel_scope_async(scope_key)` is the event-loop variant used by Run cancel callbacks so the Windows tree-kill never blocks the loop.

## Constraints & Gotchas

- Access is isolated by `ToolContext.agent_id`; missing and cross-agent processes use not-found semantics.
- `cancel_scope(run.id)` kills all active processes started by tools in that Run.
- Combined in-memory output buffers are capped. Detailed `status` returns the newest output tail without advancing Bash's internal polling cursor and points to `log_file` for the complete retained record when available.
- Core Tool subprocesses must use `subprocess_creation_flags`: the managed command, Bash environment probe, ripgrep search, and Windows `taskkill` helpers stay windowless when vBot runs without a parent console, while managed commands retain their separate process group for tree cancellation.
- Runtime shutdown remains the primary owner and explicitly kills every tracked process tree. Job close, guardian pipe EOF, and the systemd cgroup are crash-only backstops that prevent an obsolete Runtime's children from becoming untracked; they do not create durable or reattachable handles.
- The in-memory finished-process TTL and the complete-output file retention are separate: evicting a `TrackedProcess` does not remove its 72-hour temporary log.
- The TTL sweeper removes old terminal processes automatically; there is no public clear action.
- A terminal detailed `status` and a successful `kill` register their manual completion acknowledgement through `ToolContext.after_result_persisted`; merely returning from the handler is not delivery. This persistence boundary preserves automatic completion if Chat fails before the manual Tool Result reaches Session history, while cancelling a pending coalesced notice when persistence succeeds.
- `waiting_for_input` is a best-effort hint only.
- Raw stdin support is not interactive-terminal support: there is no PTY/ConPTY, terminal resize, host prompt, or TUI contract. The separate `terminal` Tool owns that capability (see `tools/terminal.md`); do not weaken the non-interactive Bash default or imply terminal semantics through `process.input`.
- All surfaced output is ANSI-stripped at the single decode boundary (`_decode` in `core/tools/process_manager.py`, via `core/utils/ansi.strip_ansi`): raw bytes stay in the buffer so byte offsets and the cap stay accurate, but the text the Agent and UI see has terminal escape/color sequences removed. This stops an Agent from copying escape codes into file writes and keeps output clean. Consequence: an Agent cannot inspect literal terminal escape codes through Process output - `read` the file directly if that is ever needed.
