---
name: coding-agents
description: Operate Codex, Claude Code, OpenCode, and other interactive coding-agent CLIs as persistent shared TUI sessions through terminal_beta, including explicit model, reasoning or effort, Agent, profile, permission, sandbox, and other startup settings. Use when delegating coding work, selecting how a coding agent should start, supervising it, answering its questions or approvals, checking progress, or continuing an existing coding-agent session. Do not use to operate vBot itself; that is the vbot-cli skill.
---

# Coding Agents

Use a real interactive Terminal Session for the coding agent's entire lifecycle. The external agent, the vBot agent, and the user share the same live terminal. Terminal Sessions survive individual Runs, so an external agent may keep working after the Run that started it ends.

## Non-negotiable rules

1. Use `terminal_beta` and launch the CLI's normal interactive command. Do not convert the task into a one-shot or machine-output invocation.
2. Keep the exact `terminal_id` returned by `start`. Use that same live Terminal Session for questions, approvals, follow-up instructions, and later work instead of starting another process.
3. Treat quiet output as an activity boundary, never as proof of completion. Use `wait`, then inspect `status` and the rendered screen.
4. Do not disable approvals, permission checks, or sandboxes merely to avoid interaction. Answer safe, unambiguous prompts through the TUI; ask the user when the choice carries meaningful authority, risk, cost, or product intent.
5. The user may take control of the same terminal in the WebUI. Before sending sensitive input such as an approval or menu selection, reread `status` and use `expected_screen_revision` so stale input is rejected.
6. Never put credentials or secrets in task text. If login or trust setup appears, leave the Terminal Session available for the user and explain what needs attention.

## Start or reuse

First call `list`. Reuse the intended live Terminal Session when one already exists. Its program-announced `title`, command, working directory, state, and recent screen help identify it.

For a configured CLI, call `start` with the interactive command, the intended `workdir`, any explicitly chosen model or safety arguments, and the initial instruction in `text`. `start` sends the text followed by Enter after the PTY is ready.

Translate user-selected launch settings into the CLI's exact `args`. Model, reasoning or effort, named agent, profile, permission mode, sandbox, extra directories, and other startup choices are independent: pass every value the user specifies and preserve the CLI or project default for every value they omit. Never silently substitute a different model or reasoning level. When support is model-, provider-, account-, or version-dependent, inspect the installed CLI's help or model catalog; if the requested combination is unavailable, surface the actual choices instead of guessing.

When the CLI may show first-run setup, login, workspace trust, or a session picker, start it without `text`, inspect `status`, then interact with the actual screen. This prevents the task instruction from being consumed by onboarding UI.

Read the matching reference before starting a known CLI:

- Codex: `references/codex.md`
- Claude Code: `references/claude-code.md`
- OpenCode: `references/opencode.md`

For another coding-agent CLI, launch its ordinary interactive command in `terminal_beta`, inspect its TUI and local help, and operate it through normal terminal input. Do not invent a headless mode. This workflow is program-agnostic and also applies to future CLIs.

## Monitor without polling blindly

Keep the last returned `attention_revision`. Call `wait` with that revision and a timeout of at most 10 seconds when a short same-Run pause is useful. A timeout only means no new activity boundary arrived during that interval; the Terminal Session continues independently.

After a wakeup, call `status`. Read the current screen first, then use paginated scrollback when the visible screen lacks needed context. Request at most 100 lines. When `scrollback.next_request` is non-null, pass that complete object unchanged to `terminal_beta`; repeat until `next_request` is null. Each continuation is older than the page before it, so prepend older pages when reconstructing chronological output, then append the current `screen`. Do not add fields to the returned continuation or replace its cursor. Use the raw `log_file` only for VT-level diagnostics or an expired scrollback cursor, not as the normal way to recover a final response, because it contains unrendered control sequences and redraws.

Report meaningful progress rather than every output fragment. It is valid to end the current Run while the coding agent keeps working; a later Run can recover the Terminal Session with `list`.

## Interact with the TUI

Use `input` with `text` and `enter` for ordinary instructions, `key` for named keys such as arrows, Tab, Escape, or Ctrl+C, and `data` only for exact terminal sequences. Menus, editors, confirmations, and question dialogs must be handled as terminal UI, not parsed as a line-oriented protocol.

If the screen presents a consequential question, summarize the options and ask the user. Do not kill the terminal while waiting. Once the user answers, reread `status`, confirm the prompt is still current, and send the answer with `expected_screen_revision`.

## Completion and continuity

Completion requires an explicit final response from the coding agent and a screen state consistent with waiting for the next instruction or an exited process. A prompt, quiet screen, or `ready` state alone is not semantic completion. Verify the claimed changes and tests in proportion to the task before reporting success.

If more work follows while the process is alive, send the next instruction into the same `terminal_id`. If the process has exited, use the CLI's documented interactive resume command from its reference. Kill a Terminal Session only when the user asks, the process must be aborted, or the session is definitively no longer wanted.

## Output contract

Report which CLI and Terminal Session you used, whether it is still running, what the coding agent concluded or changed, what you independently verified, and any question or approval still awaiting the user. Preserve the `terminal_id` for continuation.
