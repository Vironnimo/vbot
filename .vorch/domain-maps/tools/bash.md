# Bash Tool

Runs host shell commands and streams foreground stdout/stderr into the Run timeline.

## Interfaces

- Tool name: `bash`
- Registration: `register_bash_tool(registry, process_manager, trigger_service=None)`
- Schema: required `command` and `mode: "foreground" | "auto" | "background"`; optional `workdir`, `yield_after`, and `timeout`; `additionalProperties: false`. `yield_after` is valid only in `auto`. Per-call Environment overrides are intentionally not a Tool parameter; when needed, the Agent expresses them in the one-shot shell command using the host shell's syntax.
- Model-facing execution guidance makes the execution contract explicit: `foreground` waits until exit, timeout, or Run cancellation and never hands off; `auto` waits `yield_after` (default 30 seconds) and hands a still-running top-level process to vBot; `background` hands off immediately for long-lived work such as servers. `timeout` remains an independent hard kill deadline and never extends `yield_after`.
- Foreground success returns `{ status, mode, exit_code, output, truncated }` plus `log_file` when truncated, where `output` is the combined stdout/stderr process log. The final tool result does not include separate `stdout` or `stderr` fields; live stdout/stderr remain SSE-only Run events.
- **Model-facing output cap:** `output` keeps only the newest `BASH_MODEL_OUTPUT_CAP_CHARS` (30k) characters. `truncated: true` covers both cut causes (model cap here, process buffer cap upstream); either way the *beginning* is what's missing, and a leading marker line says so and names the complete per-process log file under `<data_dir>/artifacts/temp/bash/` (see `process.md`) so the agent can grep/read it directly. If temporary-file allocation failed, the marker carries no path.
- Handed-off results return `{ status: "running", mode, delivery: "automatic", session_id, handoff_note, ... }` plus capped combined `output` captured before handoff, and always include `log_file` when temporary-file logging is available. The Agent-facing note says that vBot monitors the command, that independent work may continue or the current Run may end, that polling/duplicate starts are unnecessary, and that dependent work should inspect explicitly or use `foreground` next time.
- The completion watcher applies the same output cap/marker and submits one uniquely identified result to `TriggerService.submit_completion(...)`. Bash and Sub-Agent results that finish during the same active Run are combined in one automatic follow-up Run at that Run's end; results finishing later stay pending for later delivery. A terminal detailed `process` status or successful kill cancels a still-pending automatic notice only after that manual Tool Result is durably persisted, so the same completion cannot be delivered twice.
- Per-call cancellation: after spawn, Bash registers a cancel callback that kills the process. User cancellation returns `{ ok: false, error: { code: "cancelled_by_user", message: "Command aborted by the user" } }`; another owning-Run cancellation returns `run_cancelled`. Neither `foreground` nor `auto` hands off because of cancellation. A handed-off Bash process killed by the user-cancel callback uses "aborted by the user" wording, while the completion coordinator suppresses a new follow-up Run for a user-cancelled origin.
- Display: summary field `command`.

## Conventions

- Relative `workdir` resolves from `ToolContext.effective_cwd` (the working directory); absolute working directories are allowed.
- Uses the platform-native shell: `pwsh -NonInteractive -Command` on Windows, `bash -c` elsewhere. PowerShell remains connected to the ProcessManager stdin pipe for native child-process input, but its own host prompts fail instead of waiting indefinitely after an invalid command. The tool description names the actual shell (built per host at import from the same platform check, plus PowerShell syntax pitfalls on Windows), so the model does not guess cmd/bash syntax from the tool name.
- On Windows, command shells, the one-time environment probe, and probe-cleanup `taskkill` use the shared windowless creation flags from `process_manager.py`; none may create a visible console window in Desktop or background-server use.
- Non-zero exits are successful tool results with an exit code.

## Constraints & Gotchas

- Combined `output` and the streamed stdout/stderr Run events are ANSI-stripped — terminal color/escape sequences are removed before the text reaches the model or UI. Stripping happens once in `ProcessManager` (shared `core/utils/ansi.strip_ansi`); see `process.md`.
- A login shell environment is probed once per process and falls back to `os.environ` on failure or timeout.
- Spawn failures and tool-enforced timeouts are failure envelopes. A `process_timeout` is reported only when the timeout actually killed a still-running process (terminal status `killed`); a process that exits on its own as the deadline elapses keeps its completed/failed result instead of being masked as a timeout.
- Timeout-style failures (`process_timeout` and the sub-agent yield_after kill) append the output tail (`FAILURE_OUTPUT_TAIL_CHARS`, 10k) and the complete-log path to the error message, so diagnostics printed before the kill reach the model. A spawn `FileNotFoundError` names the missing shell, with an explicit PowerShell 7 hint when `pwsh` is absent on Windows.
- Completion submission carries the originating Run id and `project_id`, so Project Sessions deliver under their correct Project and user-cancelled origins can be persisted without recursively waking the Agent.
- **No process handoff inside a Sub-Agent.** At `nesting_depth >= 1` a Sub-Agent's Session ends with the Run, so it cannot park a process. `background` fails before spawn. `auto` remains available for bounded independent work, but reaching `yield_after` kills the process and returns `background_unavailable_in_subagent` instead of handing off; neither path spawns a watcher. `foreground` waits normally. The policy is gated by `BLOCK_BACKGROUND_AT_DEPTH`.
- **Generous auto window at depth.** Because `yield_after` becomes the kill deadline inside a Sub-Agent, omitted `yield_after` defaults to `DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS` (30 minutes) instead of the top-level 30-second handoff. Explicit `yield_after` or `timeout` still overrides, and the Sub-Agent Run timeout (`subagent_timeout_minutes`, default 60) is the outer bound.
