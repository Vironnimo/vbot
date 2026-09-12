# Claude Code interactive reference

Launch `claude` as a real TUI through `terminal`. Keep the returned `terminal_id` and use it for questions, permission prompts, user takeover, and instructions belonging to the current task.

## Start

Use `command: "claude"`, set the repository as `workdir`, and provide the initial task as `text`. Put startup overrides into the exact `args` array.

For a named model or alias with medium effort:

```json
{
  "action": "start",
  "command": "claude",
  "args": [
    "--model",
    "sonnet",
    "--effort",
    "medium",
    "--name",
    "Authentication refactor"
  ],
  "workdir": "C:/repo",
  "text": "Refactor authentication and run the relevant tests."
}
```

`--model` accepts an available alias such as `sonnet` or `opus`, or a full model name. `--effort` is direct and currently accepts `low`, `medium`, `high`, `xhigh`, or `max`; availability can still depend on the selected model and account, if Claude Code rejects the requested value, inspect local help or the TUI for supported choices.

Useful interactive start settings:

| Intent | Arguments | Guidance |
|---|---|---|
| Model | `--model <alias-or-full-name>` | Preserve the exact user choice; aliases track Anthropic's current model mapping. |
| Effort | `--effort low\|medium\|high\|xhigh\|max` | Select reasoning effort for this session. |
| Session title | `--name <name>` | Names the session and updates its terminal title, which improves WebUI identification. |
| Permission mode | `--permission-mode <mode>` | Useful modes include `manual`, `plan`, `acceptEdits`, and `auto`; never select a bypass mode without explicit authorization. |
| Tool allow/deny rules | `--allowed-tools <rules...>` / `--disallowed-tools <rules...>` | Add scoped exceptions without replacing the whole tool set. |
| Tool set | `--tools <tools...>` | Restrict available built-in tools when the task requires it. |
| Extra directory | `--add-dir <paths...>` | Add only directories the task needs. |
| Agent | `--agent <name>` | Start with a configured Claude Code Agent. |
| Settings or MCP | `--settings <file-or-json>` / `--mcp-config <configs...>` | Apply an explicit session configuration. |
| Prompt customization | `--append-system-prompt <text>` / `--system-prompt <text>` | Use only when the user explicitly wants session-level guidance beyond repository instructions. |
| Browser integration | `--chrome` / `--no-chrome` | Enable or disable Claude in Chrome for this session. |
| Worktree | `--worktree [name]` | Ask Claude Code to create and use a worktree when that isolation is requested. |

Flags documented as print-only, such as output formatting, budget limits, or fallback models, do not belong in this interactive workflow. Leave unspecified settings to Claude Code's normal user, Project, and local configuration layers.

Do not choose a broader permission mode merely to avoid interactive prompts. Preserve the configured policy unless the user has requested a different one. If login, workspace trust, or other setup appears after launch, handle the displayed prompt or tell the user what needs attention, then send the task once Claude Code is ready.

## Work with the live TUI

Use `wait` for short activity boundaries. Act from the supplied screen; use `status` when more context is needed. Answer ordinary questions or send follow-ups with `input`; use named keys for menus and confirmations. Pass the observed `screen_revision` as `expected_screen_revision` for approvals or selections; if rejected, read `status` and reconsider.

When Claude Code asks for consequential authority or information that is not inferable from the task, leave the terminal running and ask the user. Authentication should be completed by the user in the shared terminal; never send credentials as task text.

## Continue after exit

When the user requests continuation of an existing task, send input to its live `terminal_id`. If that process has exited, start `claude --continue` to reopen the most recent relevant conversation or `claude --resume <session-id-or-name>` when the exact Claude session is known. Inspect the session picker or resumed screen before sending new work.

The vBot `terminal_id` identifies the outer Terminal Session. Claude's own session id or name is separate and matters only when the CLI process must be resumed after exit.
