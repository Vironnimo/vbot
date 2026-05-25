# Bash Tool

Runs host shell commands and streams foreground stdout/stderr into the Run timeline.

## Interfaces

- Tool name: `bash`
- Registration: `register_bash_tool(registry, process_manager, trigger_service=None)`
- Schema: required `command`; optional `workdir`, `env`, `yield_after`, `background`, and `timeout`; `additionalProperties: false`.
- Foreground success returns completion data; background runs return a `session_id` for the `process` tool.
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
