---
name: coding-agents
description: Drive external coding-agent CLIs — Claude Code (claude), Codex (codex), OpenCode (opencode) — non-interactively from an agent session, where every shell call is a fresh process with no persistent console and no way to type into a prompt. Use when the user asks an agent to delegate a coding task to claude/codex/opencode, run a headless coding agent, continue or resume a coding-agent session across calls, or pick a model, sub-agent, or thinking/reasoning effort for one. Covers print/exec/run modes, session resume, JSON output, permission and sandbox flags, and model/agent/effort selection. Do not use to operate vBot itself — that is the vbot-cli skill.
metadata:
  vbot:
    requirements:
      any:
        - binary: claude
        - binary: codex
        - binary: opencode
---

# Coding Agents

Use this skill to delegate real coding work to an external coding-agent CLI (`claude`, `codex`,
`opencode`) from inside an agent run. These are powerful agents in their own right — give them a
task, let them edit code and run commands, and read back the result.

The hard part is the environment: **you have no terminal.** Every command you run is a brand-new,
non-interactive process. You cannot launch a TUI and type into it, answer a permission prompt, or
keep a shell alive between calls. Everything below exists to work within that constraint.

## The five rules of headless coding agents

1. **Always use the non-interactive entrypoint, never the bare command.**
   `claude` / `codex` / `opencode` with no run subcommand open an interactive TUI that waits for
   keyboard input — your call will hang until it times out. Use the headless form:
   `claude -p "…"`, `codex exec "…"`, `opencode run "…"`.

2. **Kill every interactive prompt up front.** A coding agent that stops to ask "allow this edit?"
   blocks forever with no one to answer. Pass the flag that pre-authorizes or disables prompting
   (`--permission-mode` / `--dangerously-skip-permissions` for claude, `--ask-for-approval never`
   plus a sandbox for codex, an auto-accept agent/flags for opencode). Scope it — see rule 5.

3. **There is no memory between calls — carry the session id yourself.** Each invocation forgets the
   last unless you explicitly continue a session. For multi-step work: capture the session/thread id
   from the first call, then resume it on the next (`-r <id>` / `codex exec resume <id>` /
   `--session <id>`), or use the "continue last" shortcut (`-c` / `resume --last` / `--continue`).

4. **Capture machine-readable output.** Use JSON output so you can reliably extract the session id
   and the final result instead of scraping prose (`--output-format json`, `codex exec --json`,
   `opencode run --format json`). Where offered, also write the final message to a file
   (`--output-last-message`).

5. **Set model, agent, effort, and scope deliberately.** Choose the model and (where supported) the
   sub-agent and reasoning/thinking effort the task needs. Run in the correct working directory and
   grant file access only to the directories required — prefer the narrowest sandbox/permission mode
   that still lets the task finish.

## Pick the CLI and read its reference

Confirm which CLI is installed / which the user wants, then read the matching reference for exact
flags, JSON shapes, and copy-ready resume examples:

- **Claude Code** → `references/claude-code.md` — `claude -p`, `--output-format json`, `-c`/`-r`,
  `--session-id`, `--model`, `--effort`, `--permission-mode`, `--agent`/`--agents`.
- **Codex** → `references/codex.md` — `codex exec`, `--json`, `codex exec resume`, `--model`,
  `--sandbox`, `--ask-for-approval`, `--output-last-message`, reasoning effort via `-c`.
- **OpenCode** → `references/opencode.md` — `opencode run`, `--format json`, `--continue`/`--session`,
  `--model provider/model`, `--agent`, reasoning via `--variant`, `--attach` to a `serve` instance.

## At a glance

| | Claude Code | Codex | OpenCode |
|---|---|---|---|
| Headless run | `claude -p "task"` | `codex exec "task"` | `opencode run "task"` |
| JSON output | `--output-format json` | `--json` (JSONL events) | `--format json` |
| Continue last | `claude -c -p "…"` | `codex exec resume --last` | `opencode run -c "…"` |
| Resume by id | `claude -r <id> "…"` | `codex exec resume <id>` | `opencode run -s <id> "…"` |
| Model | `--model sonnet\|opus\|<id>` | `--model <id>` | `--model provider/model` |
| Sub-agent | `--agent <name>` | _(profiles via `-c`)_ | `--agent <name>` |
| Effort / thinking | `--effort low…max` | `-c model_reasoning_effort="high"` | `--variant <name>` |
| Skip prompts | `--permission-mode` / `--dangerously-skip-permissions` | `--ask-for-approval never --sandbox …` | auto-accept agent / flags |

## Workflow

1. **Choose the CLI.** Prefer what the user named; otherwise pick an installed one. This skill is
   only available when at least one of `claude`/`codex`/`opencode` is on `PATH`.
2. **Compose the invocation.** Headless entrypoint + the task prompt + model/agent/effort + a
   non-interactive permission/sandbox mode + the working directory and any extra allowed dirs +
   JSON output. Read the CLI's reference for exact flag names.
3. **Run it as a long job.** These can take minutes and cost credits/money. Run with a generous
   timeout or in the background; do not poll in a tight loop or fire it blindly in a retry loop.
4. **Read the result and the session id** from the JSON (and/or the final-message file). Surface
   errors instead of assuming success.
5. **Continue if multi-step.** Resume the captured session id (or the "continue last" shortcut) for
   the next instruction rather than starting cold and losing context.
6. **Report back.** Summarize what the coding agent did, what changed, the final result, and the
   **session id** so the work can be picked up again later.

## Common Pitfalls

1. **Launching the bare TUI** (`claude` / `codex` / `opencode` alone) — it waits for keystrokes and
   hangs. Always use `-p` / `exec` / `run`.
2. **Forgetting the approval flag** — the agent stalls on an unanswerable permission prompt. Pre-
   authorize a scoped mode before running.
3. **Assuming continuity** — a second call starts fresh unless you resume the session id. Capture and
   pass it.
4. **Scraping prose for the session id or result** — brittle. Use JSON output (and a final-message
   file where available).
5. **Over-granting access** — reaching for full-access / bypass-everything when a workspace-scoped
   sandbox would do. Use the narrowest mode that completes the task.
6. **Ignoring auth** — each CLI needs its own credentials (API key or OAuth login). Logins are
   interactive, so they must be done beforehand; never paste secret keys into the task prompt.
7. **No timeout / blind loops** — these are slow and metered. Bound them and read the output before
   re-invoking.

## Output Contract

- Run the coding agent **non-interactively**, with prompts pre-authorized and output captured as JSON.
- Preserve continuity: capture the session id and resume it for follow-up steps.
- Report: which CLI and flags you used, what the agent changed/produced, the final result or error,
  and the session id for resuming.
