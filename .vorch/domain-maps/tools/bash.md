# Bash Tool

Runs host shell commands and streams foreground stdout/stderr into the Run timeline.

## Interfaces

- Tool name: `bash`
- Registration: `register_bash_tool(registry, process_manager, trigger_service=None)`
- Schema: required `command`; optional `workdir`, `env`, `yield_after`, `background`, and `timeout`; `additionalProperties: false`.
- Foreground success returns `{ status, exit_code, output, truncated }`, where `output` is the combined stdout/stderr process log. The final tool result does not include separate `stdout` or `stderr` fields; live stdout/stderr remain SSE-only Run events.
- Background runs return a `session_id` for the `process` tool plus combined `output` captured before backgrounding.
- Per-call cancellation: after spawn, bash registers a cancel callback that kills the process. If the user cancels the call mid-run, the foreground path returns a `{ ok: false, error: { code: "cancelled_by_user", message: "Command aborted by the user" } }` failure envelope instead of the normal completion/timeout result, and the Run continues. A background bash session killed by the user-cancel callback is reported by the completion watcher with "aborted by the user" wording (no exit code, no "completed" wording).
- Display: summary field `command`.

## Conventions

- Relative `workdir` resolves from `ToolContext.workspace`; absolute working directories are allowed.
- Uses the platform-native shell: `pwsh` on Windows, `bash -c` elsewhere.
- Non-zero exits are successful tool results with an exit code.

## Constraints & Gotchas

- Combined `output` and the streamed stdout/stderr Run events are ANSI-stripped — terminal color/escape sequences are removed before the text reaches the model or UI. Stripping happens once in `ProcessManager` (shared `core/utils/ansi.strip_ansi`); see `process.md`.
- Sensitive environment overrides such as `PATH`, loader hooks, and shell startup hooks are blocked.
- A login shell environment is probed once per process and falls back to `os.environ` on failure or timeout.
- Spawn failures and tool-enforced timeouts are failure envelopes. A `process_timeout` is reported only when the timeout actually killed a still-running process (terminal status `killed`); a process that exits on its own as the deadline elapses keeps its completed/failed result instead of being masked as a timeout.
- With `trigger_service`, background completion creates a fire-and-forget follow-up trigger with command, exit code, and output, carrying the triggering run's `project_id` so a project-scoped Session wakes under its project (without it the wake-up landed project-less or was silently dropped).
- **No backgrounding inside a sub-agent.** At `nesting_depth >= 1` a sub-agent's Session ends with the run, so it cannot park a background process. Backgrounding is blocked behind the single constant `BLOCK_BACKGROUND_AT_DEPTH` in `core/tools/bash.py`: an explicit `background: true` fails before any process spawns, and a foreground command still running at the `yield_after` threshold is killed and fails (bounded message) instead of being backgrounded — neither path spawns a completion watcher. Both failures use code `background_unavailable_in_subagent`. The top level (depth 0) is unchanged. Set `BLOCK_BACKGROUND_AT_DEPTH = False` to allow background bash at depth (the single flip-back point).
- **Generous foreground window at depth.** Because that `yield_after` threshold doubles as the kill deadline inside a sub-agent, an omitted `yield_after` there defaults to `DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS` (30 min) instead of the 30 s top-level background-hand-off default, so a normal pytest/build is not killed mid-run. An explicit `yield_after` or `timeout` still overrides, and the sub-agent run timeout (`subagent_timeout_minutes`, default 60) is the outer bound.
