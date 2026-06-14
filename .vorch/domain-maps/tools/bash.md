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

- Sensitive environment overrides such as `PATH`, loader hooks, and shell startup hooks are blocked.
- A login shell environment is probed once per process and falls back to `os.environ` on failure or timeout.
- Spawn failures and tool-enforced timeouts are failure envelopes.
- With `trigger_service`, background completion creates a fire-and-forget follow-up trigger with command, exit code, and output.
