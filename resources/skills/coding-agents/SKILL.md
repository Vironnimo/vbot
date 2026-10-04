---
name: coding-agents
description: Operate Codex, Claude Code, OpenCode, and other coding-agent CLIs through interactive terminals.
---

# Coding Agents

The `terminal` Tool and the user share the same live terminal. A `terminal_id` addresses that program in later turns too; a CLI's saved conversation id is separate.

## Launch settings

Start the CLI's interactive interface with `start`: the CLI executable as `command`, each argument as its own item in `args`, the repository path as `workdir`, and the user's task as `text`. Omit `text` when the request has no task yet.

Pass the requested model, reasoning, profile, and other launch settings as separate CLI options. Omitted options retain the CLI's configuration. If the CLI rejects a requested setting, its error, help, or model catalog can identify supported values.

Before you start or resume one of these CLIs, read its reference for launch options and saved-conversation syntax:

- Codex: `references/codex.md`
- Claude Code: `references/claude-code.md`
- OpenCode: `references/opencode.md`

## Follow the task

When the CLI's output settles, read the screen it shows: the CLI can be asking a question, waiting for an approval, or showing its final summary. Judge whether the coding task succeeded by the CLI's output and the task's results, such as changed files or test output, not by the output going quiet.

The `log_file` of a `status` result contains raw terminal output with control sequences and redraws.

## Launch problems

If `start` reports that the CLI was not found and the steps that error names do not start it either, tell the user that the requested CLI could not be started. If login or setup blocks progress, report what the CLI is asking for.
