# Codex interactive reference

Launch `codex` as a real TUI through `terminal_beta`. Keep the returned `terminal_id`; while that process is alive, all follow-up instructions and approvals belong in that same Terminal Session.

## Start

Use `command: "codex"`, set `workdir` to the repository, and provide the task as `text` when Codex is already configured. Optional interactive flags include `--model`, `--sandbox`, and `--ask-for-approval`. Preserve the user's configured defaults unless the task requires an explicit override.

If authentication, workspace trust, or first-run setup is uncertain, start without `text`, inspect `status`, and complete or hand off setup before sending the task.

Codex may announce a program or task title through the terminal protocol. Use the title from `list` or `status` to identify the Terminal Session, but rely on the screen—not the title—to decide what input is needed.

## Work with the live TUI

Use `wait` for short activity boundaries and `status` for the current screen and scrollback. Send ordinary follow-ups with `input` text plus Enter. Use named keys for menus and confirmations. Reread the screen and pass `expected_screen_revision` before approvals or other stale-sensitive choices.

Codex can accept new instructions while it is working and may queue them depending on its current UI. Only inject a new instruction when that is actually intended. Escape and Ctrl+C can interrupt active work, so use them only when interruption is deliberate.

Leave approvals enabled unless the user explicitly authorizes a different policy. A permission question is a normal interactive event, not a reason to restart Codex or widen its sandbox automatically.

## Continue after exit

Do not resume while the original Terminal Session is alive; continue through its input. After the process exits, start a new interactive Terminal Session with `codex resume --last` for the most recent relevant session or `codex resume <session-id>` when an exact Codex session id is known. Inspect the resumed screen before sending the next instruction.

Use `/status` inside Codex when its own session details are needed. The vBot `terminal_id` remains the control handle for the outer Terminal Session and is distinct from any Codex-internal session id.
