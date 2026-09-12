# OpenCode interactive reference

Interactive executable: `opencode`. Put launch options in `args`; use the Terminal Tool's `workdir` for the repository and optional `text` for the initial task.

## Launch options

| Setting | Arguments |
|---|---|
| Model | `--model <provider/model>` |
| Configured agent | `--agent <name>` |
| Continue latest conversation | `--continue` |
| Continue exact conversation | `--session <session-id>` |
| Fork a saved conversation | `--fork` with `--continue` or `--session` |
| Disable external plugins | `--pure` |

Example with a specified model:

```json
{
  "action": "start",
  "command": "opencode",
  "args": ["--model", "openai/gpt-5"],
  "workdir": "C:/repo",
  "text": "Implement the requested task and verify the result."
}
```

`opencode models` lists provider-qualified model ids when model discovery or a rejected model setting needs investigation.

## Reasoning variants

OpenCode exposes model-specific variants through its `variant_cycle` keybinding (Ctrl+T by default). A configured agent can also supply provider options such as `reasoningEffort`. The model and configuration determine available variants; the interactive launch command does not expose a universal reasoning-level flag.

When selecting a requested variant in the TUI before the task begins, omit initial `text`, select the displayed variant, then submit the task. Variant configuration is described in the [OpenCode model documentation](https://opencode.ai/docs/models/).

## Saved conversations

`--continue` selects the last conversation; `--session <session-id>` targets an exact one. The saved OpenCode id is separate from the live `terminal_id`.
