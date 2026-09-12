---
name: coding-agents
description: Start, supervise, and resume Codex, Claude Code, or OpenCode in interactive Terminal Sessions. Use for delegating coding work and handling CLI setup, progress, questions, or approvals.
---

# Coding Agents

Use a real interactive Terminal Session for the coding agent's entire lifecycle. The external agent, the vBot agent, and the user share the same live terminal. Terminal Sessions survive individual Runs, so an external agent may keep working after the Run that started it ends.

## Start a task

Call `start` with the interactive command, the intended `workdir`, any explicitly chosen model or safety arguments, and the initial instruction in `text`. For a registered Project, `workdir: "project:<project-id>"` resolves its current cwd by stable id without loading Project Context or changing Terminal ownership. `start` sends the text followed by Enter after the PTY is ready.

Translate user-selected launch settings into the CLI's exact `args`. Model, reasoning or effort, named agent, profile, permission mode, sandbox, extra directories, and other startup choices are independent: pass every value the user specifies and preserve the CLI or project default for every value they omit. Never silently substitute a different model or reasoning level. If the CLI rejects a requested launch setting, inspect its help or model catalog and report the supported choices.

If the program is not found, inform the user that the requested CLI could not be started. If setup, login, or workspace trust appears, handle the displayed prompt or tell the user what needs attention, then send the task once the CLI is ready.

Read the matching reference before starting a known CLI:

- Codex: `references/codex.md`
- Claude Code: `references/claude-code.md`
- OpenCode: `references/opencode.md`

For another coding-agent CLI, launch its ordinary interactive command in `terminal`, interact with its TUI, and operate it through normal terminal input. Do not invent a headless mode. This workflow is program-agnostic and also applies to future CLIs.

## Non-negotiable rules

1. Use `terminal` and launch the CLI's normal interactive command. Do not convert the task into a one-shot or machine-output invocation.
2. Keep the exact `terminal_id` returned by `start`. Use it for input belonging to the current task, including questions and approvals.
3. Treat quiet output as an activity boundary, never as proof of completion. Decide from the screen supplied by a notification or Tool result; use `status` only when more context is needed.
4. Do not disable approvals, permission checks, or sandboxes merely to avoid interaction. Answer safe, unambiguous prompts through the TUI; ask the user when the choice carries meaningful authority, risk, cost, or product intent.
5. The user may take control of the same terminal in the WebUI. For sensitive input such as an approval or menu selection, use the `screen_revision` from the screen you inspected as `expected_screen_revision`. If the screen changed, inspect `status` and reconsider the action before sending it.
6. Never put credentials or secrets in task text. If login or trust setup appears, leave the Terminal Session available for the user and explain what needs attention.

## Monitor without polling blindly

Keep the last returned `attention_revision`. Call `wait` with that revision and a timeout of at most 10 seconds when a short same-Run pause is useful. A timeout only means no new activity boundary arrived during that interval; the Terminal Session continues independently.

After a wakeup, inspect the supplied screen tail and act directly when it provides enough context. Use `status` without `start_line` for the current screen and newest scrollback. To read the complete retained buffer, request `status` with `start_line: 0` and up to 100 `lines`, then follow each `scrollback.next_request` unchanged until null. Explicit pages run forward from oldest to newest and include the screen when they reach it; no separate `screen` is returned. Append pages in order. Buffer indices shift when old lines are evicted, so this is a live view, not a frozen transcript. Use `log_file` only for raw VT diagnostics; it contains control sequences and redraws.

Report meaningful progress rather than every output fragment. It is valid to end the current Run while the coding agent keeps working; retain its `terminal_id` to continue supervising that task in a later Run.

## Interact with the TUI

Use `input` with `text` and `key: "enter"` for ordinary instructions, `key` for named keys such as arrows, Tab, Escape, or Ctrl+C, and `data` only for exact terminal sequences. Menus, editors, confirmations, and question dialogs must be handled as terminal UI, not parsed as a line-oriented protocol.

If the screen presents a consequential question, summarize the options and ask the user. Do not kill the terminal while waiting. Once the user answers, reread `status`, confirm the prompt is still current, and send the answer with `expected_screen_revision`.

## Completion and continuity

Completion requires an explicit final response from the coding agent and a screen state consistent with waiting for the next instruction or an exited process. A prompt, quiet screen, or `ready` state alone is not semantic completion. Verify the claimed changes and tests in proportion to the task before reporting success.

For a user-requested continuation, send input to the selected live `terminal_id`; if that process has exited, use the CLI's documented interactive resume command from its reference. Kill a Terminal Session only when the user asks, the process must be aborted, or the session is definitively no longer wanted.

## Output contract

Report which CLI and Terminal Session you used, whether it is still running, what the coding agent concluded or changed, what you independently verified, and any question or approval still awaiting the user. Preserve the `terminal_id` for continuation.
