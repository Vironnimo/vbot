# Codex interactive reference

Interactive executable: `codex`. Put launch options in `args`; use the Terminal Tool's `workdir` for the repository and optional `text` for the initial task.

## Launch options

| Setting | Arguments |
|---|---|
| Model | `--model <model>` |
| Reasoning | `-c model_reasoning_effort="<level>"` |
| Config profile | `--profile <name>` |
| Sandbox | `--sandbox read-only\|workspace-write\|danger-full-access` |
| Approvals | `--ask-for-approval on-request\|never` |
| Extra writable directory | `--add-dir <path>` (repeatable) |
| Web search | `--search` |
| Initial image | `--image <file>` |
| Inline terminal display | `--no-alt-screen` |
| Reject unknown config keys | `--strict-config` |

Reasoning is a TOML configuration value. Keep the quotes around the level inside the single argument. For a request specifying GPT-5.6 Terra and medium reasoning:

```json
{
  "action": "start",
  "command": "codex",
  "args": ["--model", "gpt-5.6-terra", "-c", "model_reasoning_effort=\"medium\""],
  "workdir": "C:/repo",
  "text": "Implement the requested task and verify the result."
}
```

Supported reasoning levels depend on the model. `codex debug models` supplies the catalog when model discovery or a rejected model setting needs investigation.

## Saved conversations

`codex resume <session-id>` opens a saved conversation; `codex resume --last` selects the most recent matching conversation, and `codex resume` opens the picker. These are launch arguments, distinct from sending input to a live `terminal_id`. Codex's `/status` displays details of its current conversation.
