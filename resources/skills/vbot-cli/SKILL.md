---
name: vbot-cli
description: "Configure and operate vBot: Agents, Projects, Sessions, Settings, Skills, Memory, prompts, Providers and Models, Channels, scheduling, Extensions and MCP, server lifecycle, updates, storage recovery, and diagnostics. Use for changes to the application itself and for investigating its current configuration or health."
---

# vBot CLI

Use `vbot` through Bash to inspect and configure the application. The server runs Agents and owns the state used by the CLI, WebUI, Desktop, and Channels. A saved configuration change affects that server instance.

## Start with the task

1. Read the relevant reference below before writing. It explains scope, replacement rules, and verification. Use `vbot <area> <command> --help` for exact arguments; primary ids are positional.
2. Inspect the affected resource. Reuse exact ids from results. Read the complete content before replacing a Skill, prompt block, or automation prompt; a list preview is insufficient.
3. Apply the requested change, preserving unrelated fields. Omitted update flags normally leave fields unchanged; list and policy flags replace their entire value. Read the area's exceptions.
4. Check the result for saved values, warnings, pending work, and failures. A saved Agent without an effective Model cannot run; an enabled Channel may still fail to connect; a scheduled restart has not completed. Verify the user's intended behavior and report what the evidence establishes.

| Task | Read |
|---|---|
| Agents, Project membership, permissions, Sessions | `references/agents-projects.md` |
| Provider keys/OAuth/limits, Models, voices and specialized Task Models | `references/providers.md` |
| Settings, System Prompt blocks, Extension settings | `references/configuration.md` |
| MCP installation, grants, discovery and application operations | `references/mcp.md` |
| Telegram or Discord routing, tokens and group access | `references/channels.md` |
| First Telegram setup and chat-id discovery | `references/telegram-setup.md` |
| Recurring or one-time scheduled Runs | `references/cron.md` |
| Runs after startup, including restart continuation | `references/bootstrap.md` |
| Skill inspection, authoring, sharing and disable policy | `references/skills.md` |
| Pinned Memory for an Identity Agent | `references/memory.md` |
| Logs, Provider traces and Session usage statistics | `references/diagnostics.md` |
| Server start/stop/restart, update, uninstall, Autostart, Desktop, Doctor | `references/server.md` |
| Session-store health, snapshots and recovery | `references/session-store.md` |
| Filesystem investigation, data location, backups or manual repair | `references/system-layout.md` |

`vbot tool list` lists registered public Tools. An Agent's actual access also depends on its Tool policy, Project ceiling and runtime conditions; inspect its configuration before changing permissions.

## Keep the target and path roles clear

Management commands need a running server. Append `--host <host> --port <port>` after the command when selecting an explicit target, and repeat them on follow-up calls. `--data-dir <path>` selects the CLI's local instance configuration; it does not redirect RPC state on an already selected server. Read `references/server.md` for defaults and command-specific exceptions.

`home`, `server`, `desktop`, `update`, `uninstall`, `autostart`, and `doctor` operate locally; Session-store commands have both local and RPC operations. Run lifecycle work on the server machine. Do not interpret local filesystem reports as evidence about a remote server.

`vbot home` reports `vbot_root` (code and bundled resources) and local `data_dir` (instance state). A Project's `cwd` is its external working directory. An Identity Agent's Workspace holds SOUL and Memory and may be elsewhere. A Session holds conversation history; a Run is active work inside it. These roles remain distinct even when paths happen to match. Use `agent show` and `project show` to resolve them.

## Inputs and recovery

- Prefer validated CLI writes over editing runtime files. Use `--file` for multiline content; that path belongs to the machine running the CLI. Project and Workspace paths belong to the server machine. After an unavoidable manual JSON edit, run `vbot doctor config` against that local data directory.
- Pass arrays and objects as one JSON argument. If the shell alters quoting, use a command's stdin input: `config set --stdin`, Provider key stdin, or Task Model/Extension stdin. Stdin JSON must be valid UTF-8. Never echo credentials into command arguments, logs or reports.
- A failed RPC with `request_state: unknown` may already have changed state. Inspect the same target before retrying; do not repeat completed steps. `not_sent` means that RPC was not delivered.
- Use `session list` pagination and filtered `log read` for large histories. Explicit expansion is available; an omitted row in a bounded result is not evidence of absence.
- Before updating the instance hosting this Run, follow `references/server.md`: save and verify a one-shot Bootstrap in this Session, then update. Its prompt must verify the result without repeating the update. `--no-restart` needs no continuation job unless a later startup check is wanted.
