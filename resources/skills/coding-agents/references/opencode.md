# OpenCode interactive reference

Launch `opencode` as a real TUI through `terminal`. Keep the returned `terminal_id` and use it for menu interactions and instructions belonging to the current task.

## Start

Use `command: "opencode"`, set the repository as `workdir`, and provide the task as `text`. Put supported startup overrides into the exact `args` array.

For a selected model and a configured Agent that already carries the desired provider-specific reasoning settings:

```json
{
  "action": "start",
  "command": "opencode",
  "args": [
    "--model",
    "openai/gpt-5",
    "--agent",
    "deep-thinker"
  ],
  "workdir": "C:/repo",
  "text": "Implement the requested task and verify the result."
}
```

Useful interactive start settings:

| Intent | Arguments | Guidance |
|---|---|---|
| Model | `--model <provider/model>` | Use the requested provider-qualified model id; consult `opencode models` if it is rejected or the user asks for available models. |
| Agent | `--agent <name>` | Select a configured Agent; its configuration may set model, permissions, `reasoningEffort`, `textVerbosity`, temperature, and other provider options. |
| Continue latest | `--continue` | Use for a requested continuation after the original CLI process exited. |
| Continue exact | `--session <session-id>` | Use for a requested continuation of an exact OpenCode session after process exit. |
| Fork resumed work | `--fork` with `--continue` or `--session` | Branch intentionally instead of mutating the resumed session. |
| Minimal customization | `--pure` | Disable external plugins for troubleshooting when explicitly desired. |

Reasoning in OpenCode is model- and provider-specific. Current versions expose named variants in the TUI through `/variants` or the configured `variant_cycle` keybinding, commonly Ctrl+T. Variants may map to values such as `reasoningEffort: "low"` or `reasoningEffort: "high"`, but their names and availability depend on the selected model. A configured Agent can set `reasoningEffort` directly and is the cleanest repeatable way to request a fixed level.

Do not pass `--variant` to the default TUI unless the installed `opencode --help` actually exposes it for that command. When the user requests a reasoning level and no matching configured Agent or supported start flag exists, start OpenCode with the requested `--model` but without task `text`, select the actual advertised variant in the TUI, and only then send the task. Do not assume that a variant named `medium`, `high`, or `max` exists.

If authentication, provider selection, workspace setup, or a session picker appears after launch, handle the displayed prompt or tell the user what needs attention, then send the task once OpenCode is ready.

## Work with the live TUI

Use `wait` for short activity boundaries. Act from the supplied screen; use `status` when more context is needed. Send ordinary instructions with text plus Enter and use named keys for selections, dialogs, and interruption. Pass the observed `screen_revision` as `expected_screen_revision` for approvals or other choices tied to the screen; if rejected, read `status` and reconsider.

Do not infer completion from quiet output. Require OpenCode's explicit final response, verify its claimed changes, and preserve the Terminal Session for follow-up work.

## Continue after exit

When the user requests continuation of an existing task, send input to its live `terminal_id`. If that process has exited, start a new interactive Terminal Session with `opencode --continue` for the most recent relevant session or `opencode --session <session-id>` when an exact OpenCode session id is known. Verify that the intended session was restored before sending new instructions.

The vBot `terminal_id` is the stable control handle for the live PTY. An OpenCode session id is separate and is only needed to restore an exited CLI process.
