---
name: coding-agents
description: Operate Codex, Claude Code, OpenCode, and other coding-agent CLIs through interactive terminals.
---

# Coding Agents

The `terminal` Tool and the user share the same live terminal. A `terminal_id` addresses that process across Runs; a CLI's saved conversation id is separate.

## Launch settings

For an interactive launch, use the CLI executable as `command`, separate argument tokens in `args`, and the repository as `workdir`. The Tool also accepts `project:<project-id>` as the working directory.

`start.text` queues an initial instruction followed by Enter. The start result can arrive before that input is sent. Omit `text` when no initial input should be sent.

Pass the requested model, reasoning, profile, and other launch settings as separate CLI options. Omitted options retain the CLI's configuration. If the CLI rejects a requested setting, its error, help, or model catalog can identify supported values.

CLI-specific syntax and saved-conversation resume:

- Codex: `references/codex.md`
- Claude Code: `references/claude-code.md`
- OpenCode: `references/opencode.md`

## Observe and interact

Tool results and activity notifications provide rendered terminal text. Use `status` for additional screen or history context. An attached terminal can notify across Runs; starting without `text` does not notify for the initial startup output.

`wait` provides a short pause for activity. Pass a returned `attention_revision` as `after_revision` to wait beyond it. A timeout leaves the process running. Quiet output and the `ready` state describe terminal activity, not whether the coding task succeeded; assess the CLI's output and task results.

Use `input.text` with `key: "enter"` to submit instructions, named keys for menus, and `data` for control sequences. For input tied to a displayed prompt, pass its `screen_revision` as `expected_screen_revision`. A `stale_screen` error means the input was not sent: read the current screen and reconsider the response.

## Read longer output

Default `status` returns the current screen and recent scrollback. For the complete retained buffer, start with `start_line: 0` and follow `scrollback.next_request` until null. Explicit pages run forward and contain the current screen only when they reach it. Append pages in order; indices shift if old output is evicted.

Rendered text represents terminal cells, not exact file contents. `log_file`, when present, contains raw terminal output with control sequences and redraws.

## Launch problems

If the executable is not found, tell the user that the requested CLI could not be started. If login or setup blocks progress, report what the CLI is asking for.
