# Claude Code interactive reference

Interactive executable: `claude`.

## Launch options

| Setting | Arguments |
|---|---|
| Model | `--model <alias-or-full-name>` |
| Reasoning effort | `--effort <level>` |
| CLI conversation name | `--name <name>` |
| Permission mode | `--permission-mode <mode>` |
| Tool allow/deny rules | `--allowed-tools <rules...>` / `--disallowed-tools <rules...>` |
| Available tools | `--tools <tools...>` |
| Extra directories | `--add-dir <paths...>` |
| Configured agent | `--agent <name>` |
| Settings or MCP configuration | `--settings <file-or-json>` / `--mcp-config <configs...>` |
| Prompt customization | `--append-system-prompt <text>` / `--system-prompt <text>` |
| Browser integration | `--chrome` / `--no-chrome` |
| Worktree | `--worktree [name]` |

For a request specifying Sonnet and medium effort, with the values in angle brackets replaced:

```json
{
  "action": "start",
  "command": "claude",
  "args": ["--model", "sonnet", "--effort", "medium"],
  "workdir": "<repository path>",
  "text": "<the user's task>"
}
```

Model aliases resolve through Claude Code. Supported effort values depend on the installed version, model, and account. Its error or help identifies accepted values if a setting is rejected. Options marked print-only in CLI help do not apply to the interactive TUI.

## Saved conversations

`claude --continue` opens the most recent conversation in the working directory. `claude --resume <session-id>` targets a saved conversation; `claude --resume` opens the picker. Claude's conversation id is separate from the live `terminal_id`.
