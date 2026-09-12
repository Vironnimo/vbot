# Codex interactive reference

Launch `codex` as a real TUI through `terminal`. Keep the returned `terminal_id`; use it for questions, approvals, and instructions belonging to the current task.

## Start

Use `command: "codex"`, set `workdir` to the repository, and provide the task as `text`. Put startup overrides into the exact `args` array; do not embed them in the task text.

For “run this with Codex, GPT-5.6 Terra, medium reasoning,” start the interactive TUI with:

```json
{
  "action": "start",
  "command": "codex",
  "args": [
    "--model",
    "gpt-5.6-terra",
    "-c",
    "model_reasoning_effort=\"medium\""
  ],
  "workdir": "C:/repo",
  "text": "Implement the requested task and verify the result."
}
```

`--model` selects the model for this session. Codex currently exposes reasoning as the configuration key `model_reasoning_effort`, so pass it with `-c`/`--config`. Supported efforts are model-specific. If Codex rejects the requested model or effort, inspect `codex debug models`; a current catalog may expose levels such as `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`, but never assume every model supports every level.

Useful interactive start settings:

| Intent | Arguments | Guidance |
|---|---|---|
| Model | `--model <model>` | Use the exact catalog slug, such as `gpt-5.6-terra`. |
| Reasoning | `-c model_reasoning_effort="<level>"` | Quote the TOML string inside the single argument. |
| Config profile | `--profile <name>` | Layer a named Codex profile over base configuration. |
| Sandbox | `--sandbox read-only\|workspace-write\|danger-full-access` | Preserve the configured sandbox unless the user requests an override. |
| Approvals | `--ask-for-approval untrusted\|on-request\|never` | Do not choose `never` merely to avoid interaction. |
| Extra writable directory | `--add-dir <path>` | Repeat for each explicitly authorized directory. |
| Web search | `--search` | Enable only when requested or required by the task. |
| Initial image | `--image <file>` | Repeat for image inputs supplied for the task. |
| Inline TUI | `--no-alt-screen` | Keep terminal scrollback inline when specifically useful; the normal alternate-screen TUI is supported. |
| Strict config | `--strict-config` | Fail early when unknown configuration keys should be treated as errors. |

Prefer `terminal`'s `workdir` over also passing Codex `--cd`; using both creates two sources of truth. Leave unspecified settings to the user's global, Project, or profile configuration.

If authentication, workspace trust, or first-run setup appears after launch, complete or hand off the displayed setup, then send the task once Codex is ready.

Codex may announce a program or task title through the terminal protocol. When the user requests an existing Terminal Session, its title from `list` or `status` can help identify it, but rely on the screen—not the title—to decide what input is needed.

## Work with the live TUI

Use `wait` for short activity boundaries. Act from the supplied screen; use `status` when more context is needed. Send ordinary follow-ups with `input` text plus Enter. Use named keys for menus and confirmations. Pass the observed `screen_revision` as `expected_screen_revision` for approvals or other stale-sensitive choices; if rejected, read `status` and reconsider.

Codex can accept new instructions while it is working and may queue them depending on its current UI. Only inject a new instruction when that is actually intended. Escape and Ctrl+C can interrupt active work, so use them only when interruption is deliberate.

Leave approvals enabled unless the user explicitly authorizes a different policy. A permission question is a normal interactive event, not a reason to restart Codex or widen its sandbox automatically.

## Continue after exit

When the user requests continuation of an existing task, send input to its live Terminal Session. If that process has exited, start a new interactive Terminal Session with `codex resume --last` for the most recent relevant session or `codex resume <session-id>` when an exact Codex session id is known. Inspect the resumed screen before sending the next instruction.

Use `/status` inside Codex when its own session details are needed. The vBot `terminal_id` remains the control handle for the outer Terminal Session and is distinct from any Codex-internal session id.
