---
name: vbot-cli
description: Configure and operate vBot through the vbot CLI. Use when asked to start, stop, restart, update, or uninstall vBot, set up provider credentials (API key or OAuth), or list/add/edit/remove agents, projects, sessions, channels (Telegram, Discord), cron jobs, task-model bindings, prompts, skills, extensions, or settings — or to inspect models, tools, logs, debug traces, Provider subscription usage, and Session usage statistics (tokens, runs, errors, tool and skill usage).
---

# vBot CLI

The `vbot` CLI is the automation surface for configuring and operating a vBot instance: run the command, verify the result, report what changed.

## Rules

- Primary identifiers are positional (`vbot agent show assistant`); secondary parameters are flags. `vbot <area> <command> --help` shows exact flags plus a usage example.
- Only `server start|stop|restart|status`, `desktop`, `update`, `uninstall`, `autostart`, and `doctor` work without a running target server. Everything else needs one. When operating the vBot instance hosting the current Run, execute the requested command directly — the Run already proves that server is available. Check `vbot server status` and start with `vbot server start` only when targeting another instance or troubleshooting connectivity.
- Non-default instance: add `--host`, `--port`, `--data-dir` to every command.
- Prefer CLI commands over direct file edits — settings, agents, channels, cron jobs, prompt blocks, and provider keys all have commands. If a manual JSON edit was unavoidable, validate with `vbot doctor config`.
- Never echo secrets in output. API keys go through `provider set-key`, extension secrets through `extensions <name> set <field> --stdin`, and managed channel tokens through `channel add ... --token-stdin` or `channel set-token ... --stdin`. Channel tokens never belong in shell arguments; use `--token-env` only when an external deployment environment already owns the variable.
- Inspect before changing; verify after with the matching list/show/status command.
- Mutation output is a verification result, not merely an acknowledgement: Agent, Project, Channel, and Cron create/update commands print the saved resource; Project removal prints affected rooted Agents and file-copy/backup effects. Read it before issuing a separate verification call, then use `show`/`list` when the requested outcome depends on live discovery or runtime health.
- Keep Identity Agent, Project Agent, Workspace, and Project cwd separate. A generic request to create an Agent means an Identity Agent; root it in a Project when its file/shell work should run there. A Project Agent is a repo-discovered Config Agent with no Workspace, SOUL, or Memory and is created only when the user explicitly asks for a Project Team profile. See `references/agents-projects.md`.
- Follow CLI error hints (`did you mean`, candidate lists) before retrying. If another process occupies the port, report it — don't kill it.
- Finish with a compact report: commands run, what changed, verification result, and any remaining user action (complete an OAuth login, send a Telegram message, ...).

## Conventions

- Model references are `<provider>/<model-id>`, optionally pinned to a connection and credential account with `::<connection>[:<account>]` (e.g. `openai/gpt-5.2::api-key:work`).
- Project agents are addressed `agent@projekt` (e.g. `orchestrator@vbot`) in session, cron, and prompt-preview commands; a bare id means an identity agent.
- JSON values (arrays, objects, booleans) are passed as one shell argument: `vbot config set skill_directories '["C:/skills"]'`.
- List-replacing flags (`--allow`, `--allowed-tools`, `--allowed-skills`, `--auto-load`, Project Skill policy flags, Channel mention/owner flags, and `--subagent-allow`) replace the full list — pass every value that should remain.

## Areas

Read the reference file before using an area's write commands — it has the exact flags and the gotchas.

| Area | Commands | Reference |
|---|---|---|
| `server` | `start` `stop` `restart` `status` | `references/server.md` |
| `update` | update the install from git, restart | `references/server.md` |
| `uninstall` | remove the application, reset its data, or both | `references/server.md` |
| `autostart` | `enable` `disable` `status` | `references/server.md` |
| `desktop` | open the desktop window | `references/server.md` |
| `home` | show resolved application and data directories | `references/server.md` |
| `doctor` | `settings` `config` — validate config files locally | `references/server.md` |
| `provider` | `list` `status` `usage` `enable` `disable` `set-key` `unset-key` `connect` `connect-status` `disconnect` | `references/providers.md` |
| `model` | `list` `refresh [<provider>]` | `references/providers.md` |
| `task-model` | `list` `targets` `options` `set` `clear` | `references/providers.md` |
| `agent` | `list` `show` `create` `update` `rename` `delete` | `references/agents-projects.md` |
| `project` | `add` `list` `show` `set` `set-override` `clear-override` `rm` | `references/agents-projects.md` |
| `session` | `list` `create` `fork` `rename` `set-compaction-policy` `delete` `link-channel` | `references/agents-projects.md` |
| `channel` | `add` `list` `status` `update` `set-token` `enable` `disable` `remove` | `references/channels.md` |
| `cron` | `list` `create` `update` `delete` `enable` `disable` | `references/cron.md` |
| `config` | show raw settings, `effective`, `get`, `set` | `references/configuration.md` |
| `prompt` | `list` `update` `reset` `create` `remove` `set-layout` `reset-layout` `preview` | `references/configuration.md` |
| `extensions` | `list` `reload` `<name>` `<name> set` `enable` `disable` | `references/configuration.md` |
| `log` | `list` `read` | `references/diagnostics.md` |
| `debug` | `status` `probe` `traces` `trace` `clear` | `references/diagnostics.md` |
| `statistics` | `overview` `usage` `runs` `errors` `tools` `skills` | `references/diagnostics.md` |
| `skill` | `list` `read` `create` `update` `delete` `write-file` `remove-file` | `references/skills.md` |
| `tool` | `list` — public tools exposed to agents | — |

First-time Telegram bot setup (BotFather, token, chat-id discovery, privacy mode): follow `references/telegram-setup.md`.

## Quick reference

The most common single commands:

```bash
vbot provider set-key <provider-id> <api-key> --refresh-models  # activate a provider with a user-supplied key
vbot model list --task chat                                    # exact runnable Model ids for an Agent
vbot agent update <agent-id> --model <provider>/<model-id>      # switch an agent's model
vbot config set <key> <value>                                   # change a settings key (JSON or string)
vbot channel status <channel-id>                                # channel health + denied inbound chats
vbot server restart                                             # apply code or unavoidable manual config edits
```
