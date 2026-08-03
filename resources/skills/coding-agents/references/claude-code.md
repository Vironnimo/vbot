# Claude Code interactive reference

Launch `claude` as a real TUI through `terminal_beta`. Keep the returned `terminal_id` and use the same live Terminal Session for follow-up work, questions, permission prompts, and user takeover.

## Start

Use `command: "claude"`, set the repository as `workdir`, and provide the initial task as `text` when Claude Code is already configured. Optional interactive flags include `--model`, `--effort`, `--permission-mode`, and `--name`. A meaningful `--name` may also improve the terminal title shown in the WebUI.

Do not choose a broader permission mode merely to avoid interactive prompts. Preserve the configured policy unless the user has requested a different one. If login, workspace trust, or other setup may appear, start without `text` and inspect the screen first.

## Work with the live TUI

Use `wait` for short activity boundaries and `status` to understand the current screen. Answer ordinary questions or send follow-ups with `input`; use named keys for menus and confirmations. Before approving a tool call or selecting an option, reread `status` and send `expected_screen_revision` with the input.

When Claude Code asks for consequential authority or information that is not inferable from the task, leave the terminal running and ask the user. Authentication should be completed by the user in the shared terminal; never send credentials as task text.

## Continue after exit

While the process is alive, continue through the existing `terminal_id`. After it exits, start `claude --continue` to reopen the most recent relevant conversation or `claude --resume <session-id-or-name>` when the exact Claude session is known. Inspect the session picker or resumed screen before sending new work.

The vBot `terminal_id` identifies the outer Terminal Session. Claude's own session id or name is separate and matters only when the CLI process must be resumed after exit.
