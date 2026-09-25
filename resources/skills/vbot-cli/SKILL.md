---
name: vbot-cli
description: "Configure and operate vBot: Agents, Projects, Sessions, Settings, Skill installation from links or archives, Memory, prompts, Providers and Models, Channels, scheduling, Extension authoring and management, MCP, server lifecycle, updates, storage recovery, and diagnostics. Use for changes to the application itself, investigating its current configuration or health, and extended Session search and transcript retrieval."
---

# vBot CLI

Use `vbot` through the shell Tool to inspect and configure the application. The server runs Agents and owns the state used by the CLI, WebUI, Desktop, and Channels. A saved configuration change affects that server instance.

Choose the area and action first: `vbot <area> <action> [target] [options]`. Related actions can be grouped, for example `vbot project override set`, `vbot channel token set`, and `vbot skill file write`. There is no fixed word count. Resource ids are positional; flags supply options and values. Use the commands shown by help instead of inventing action flags such as `--restart`.

Start with `vbot help` or `vbot <area>`, then narrow to `vbot <area> <group> --help`. Bare command groups show help without executing an action. Collection plurals also work; prefer the names and action paths shown in help. Older compound command spellings remain compatible. Syntax errors may suggest a correction but never execute it automatically.

`[OK]` confirms command completion; `[WARN]` calls out pending work or a reported limitation; `[ERROR]` marks failure. Read the returned state before claiming runtime readiness. Progress and completion notices for management commands go to stderr; stdout retains the data, including complete JSON and content reads. `--output plain` suppresses these extra notices and uses the stable data layout. Terminal output is readable by default; `--output human` requests that layout in a capture too. Lifecycle and Doctor commands retain their dedicated reports.

## Start with the task

For a focused question about a past conversation, use `session_search` and answer from sufficient evidence. Read `references/session-search.md` when you need more evidence, complete wording, a Session list, or a Tool Result.

1. Read the relevant reference below before writing. It explains scope, replacement rules, and verification. Use `vbot <area> <command> --help` for exact arguments; primary ids are positional.
2. Inspect the affected resource. Reuse exact ids from results. Read the complete content before replacing a Skill, prompt block, or automation prompt; a list preview is insufficient.
3. Apply the requested change, preserving unrelated fields. Omitted update flags normally leave fields unchanged; list and policy flags replace their entire value. Read the area's exceptions.
4. Check the result for saved values, warnings, pending work, and failures. A saved Agent without an effective Model cannot run; an enabled Channel may still fail to connect; a scheduled restart has not completed. Verify the user's intended behavior and report what the evidence establishes.

| Task | Read |
|---|---|
| Agents, Project membership, permissions, Sessions | `references/agents-projects.md` |
| Extended Session search, listing past conversations, full transcripts or exact Tool Results | `references/session-search.md` |
| Provider keys/OAuth/limits, Models, voices and specialized Task Models | `references/providers.md` |
| Settings, System Prompt blocks, Extension settings | `references/configuration.md` |
| Create or change an Extension, its Tools, hooks, Commands or pages | `references/extensions.md` |
| Use bundled Swarm or Computer Use, or inspect Extension connection UI | `references/extension-usage.md` |
| MCP installation, Tool access, discovery and application operations | `references/mcp.md` |
| Channel setup (Telegram, Discord, Slack, Mattermost, WhatsApp), tokens and group access | `references/channels.md` |
| First Telegram setup and chat-id discovery | `references/telegram-setup.md` |
| Recurring or one-time scheduled Runs | `references/cron.md` |
| Runs after startup, including restart continuation | `references/bootstrap.md` |
| Install a Skill from a link, archive or folder; inspect, author, share or disable Skills | `references/skills.md` |
| Pinned Memory for an Identity Agent | `references/memory.md` |
| Logs, Provider traces, Session usage statistics and server performance | `references/diagnostics.md` |
| Server start/stop/restart, update, uninstall, Autostart, Desktop, Doctor | `references/server.md` |
| Database health, data snapshots and recovery | `references/data-store.md` |
| Filesystem investigation, data location, backups or manual repair | `references/system-layout.md` |

`vbot tool list` lists registered public Tools. An Agent's actual access also depends on its Tool policy, Project ceiling and runtime conditions; inspect its configuration before changing permissions.

## Create Extensions

Read [the Extension authoring guide](references/extensions.md) before creating or changing an Extension. It covers installation, declarations, lifecycle, permissions and verification. Copy or adapt the matching example under `assets/extensions/`: `word_count.py` for a Tool, `guard_bash.py` for a decision hook, or the complete `workflow_command/` directory for a Command with a bundled Skill. These are templates; install only the requested Extension into the target server's Extension directory.

Read a template with the `skill` Tool using `name: "vbot-cli"` and its relative `file_path`, such as `assets/extensions/word_count.py`. To copy files on disk, resolve this bundled Skill under the `vbot_root` reported by `vbot home`: `resources/skills/vbot-cli/`. Preserve a directory example's manifest and nested Skill files.

## Keep the target and path roles clear

Management commands need a running server. Append `--host <host> --port <port>` after the command when selecting an explicit target, and repeat them on follow-up calls. `--data-dir <path>` selects the CLI's local instance configuration; it does not redirect RPC state on an already selected server. Read `references/server.md` for defaults and command-specific exceptions.

`home`, `server`, `desktop`, `update`, `uninstall`, `autostart`, and `doctor` operate locally; `data-store` commands have both local and RPC operations. Run lifecycle work on the server machine. Do not interpret local filesystem reports as evidence about a remote server.

`vbot home` reports `vbot_root` (code and bundled resources) and local `data_dir` (instance state). A Project's `cwd` is its external working directory. An Identity Agent's Workspace holds SOUL and Memory and may be elsewhere. A Session holds conversation history; a Run is active work inside it. These roles remain distinct even when paths happen to match. Use `agent show` and `project show` to resolve them.

## Inputs and recovery

- Prefer validated CLI writes over editing runtime files. Use `--file` for multiline content; that path belongs to the machine running the CLI. Project and Workspace paths belong to the server machine. After an unavoidable manual JSON edit, run `vbot doctor config` against that local data directory.
- Pass arrays and objects as one JSON argument. If the shell alters quoting, use a command's stdin input: `config set --stdin`, Provider key stdin, or Task Model/Extension stdin. Stdin JSON must be valid UTF-8. Never echo credentials into command arguments, logs or reports.
- On failure, use the suggested read commands to inspect the same target and reuse exact ids from results. `Next:` commands are suggestions, never automatically executed. Command, option, and allowed-value typos can get corrections; missing inputs point to command help. Recovery hints remain available on stderr with `--output plain`; stdout keeps the original result details.
- A failed RPC with `request_state: unknown` may already have changed state. `not_sent` means only that RPC was not delivered; earlier steps in the command may still be saved. `responded` means the server returned an error, not that all changes were rolled back. Inspect the same target before retrying a mutation; do not repeat completed steps.
- Use `session list` pagination and filtered `log read` for large histories. Explicit expansion is available; an omitted row in a bounded result is not evidence of absence.
- Before updating the instance hosting this Run, read `references/server.md`. Packaged installations arrange their own Session continuation; source checkouts require a saved one-shot Bootstrap. Inspect `vbot home` to distinguish them. Acceptance is not completion: verify the saved outcome before reporting success, and do not repeat an accepted update.
