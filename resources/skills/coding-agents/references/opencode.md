# OpenCode interactive reference

Launch `opencode` as a real TUI through `terminal_beta`. Keep the returned `terminal_id` and send every follow-up or menu interaction to that same live Terminal Session.

## Start

Use `command: "opencode"`, set the repository as `workdir`, and provide the task as `text` after confirming the CLI is configured. Optional interactive flags include `--model` and `--agent`. Preserve configured defaults unless the user or task requires a specific choice.

If authentication, provider selection, workspace setup, or a session picker may appear, start without `text`, inspect `status`, and interact with the actual screen before sending the task.

## Work with the live TUI

Use `wait` for short activity boundaries, then call `status` for the current rendered screen. Send ordinary instructions with text plus Enter and use named keys for selections, dialogs, and interruption. Reread the screen and use `expected_screen_revision` for approvals or other choices that must not land on a changed prompt.

Do not infer completion from quiet output. Require OpenCode's explicit final response, verify its claimed changes, and preserve the Terminal Session for follow-up work.

## Continue after exit

While OpenCode remains alive, continue through the same `terminal_id`. After it exits, start a new interactive Terminal Session with `opencode --continue` for the most recent relevant session or `opencode --session <session-id>` when an exact OpenCode session id is known. Verify that the intended session was restored before sending new instructions.

The vBot `terminal_id` is the stable control handle for the live PTY. An OpenCode session id is separate and is only needed to restore an exited CLI process.
