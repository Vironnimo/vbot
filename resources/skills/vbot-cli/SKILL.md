---
name: vbot-cli
description: Configure, inspect, and operate vBot through the vbot CLI, including locating the vBot root and runtime data. Use when asked to start, stop, restart, update, or uninstall vBot, set up provider credentials (API key or OAuth), find vBot files or diagnose stored state, list/add/edit/remove agents, projects, sessions, channels (Telegram, Discord), cron jobs, prompts, skills, extensions, or settings, inspect complete Model data, or configure specialized Task Models such as TTS/STT including model-specific voices and options — as well as to inspect tools, logs, debug traces, Provider subscription usage, and Session usage statistics (tokens, runs, errors, tool and skill usage).
---

# vBot CLI

The `vbot` CLI is the automation surface for configuring and operating a vBot instance: run the command, verify the result, report what changed. One async Runtime lives behind the server; the CLI, WebUI, Desktop, and Channels are Accessors to that same system rather than separate stores.

## System and paths

Keep these boundaries separate:

- `vbot_root` is the checkout or installation containing vBot code and bundled resources.
- `data_dir` is the server instance's local runtime state and credentials. `~/.vbot` is only the product default, not a path to assume.
- A Project `cwd` is the external working directory where file and shell Tools operate for that Project; it is referenced by Project state but is not contained by the data directory.
- An Identity Agent's Workspace is its identity and Memory home. It defaults inside the Agent's data directory but may be configured as an external absolute path.
- A Session is persisted conversation history owned by one Agent; a Run is one active execution inside it and is not a separate top-level data directory.

Run `vbot home` before local filesystem investigation to resolve `vbot_root` and `data_dir`. The command reports the machine where it runs and does not query a server selected with `--host`; when targeting a remote vBot server, use its CLI/RPC diagnostics unless you separately have filesystem access to that server host.

Read `references/system-layout.md` before searching runtime files, diagnosing missing or corrupt stored state, making an unavoidable manual edit, or planning a data backup, move, or reset. Use the owning CLI area first and inspect files only when the semantic command cannot answer the question.

## Rules

- Primary identifiers are positional (`vbot agent show assistant`); secondary parameters are flags. `vbot <area> <command> --help` shows exact flags plus a usage example.
- Only `server start|stop|restart|status`, `desktop`, `update`, `uninstall`, `autostart`, and `doctor` work without a running target server. Everything else needs one. When operating the vBot instance hosting the current Run, execute the requested command directly — the Run already proves that server is available. Check `vbot server status` and start with `vbot server start` only when targeting another instance or troubleshooting connectivity.
- Non-default instance: add `--host`, `--port`, `--data-dir` to every command.
- Prefer CLI commands over direct file edits — settings, agents, channels, cron jobs, prompt blocks, and provider keys all have commands. If a manual JSON edit was unavoidable, validate with `vbot doctor config`.
- Never echo secrets in output. API keys go through `provider set-key`, extension secrets through `extensions <name> set <field> --stdin`, and managed channel tokens through `channel add ... --token-stdin` or `channel set-token ... --stdin`. Channel tokens never belong in shell arguments; use `--token-env` only when an external deployment environment already owns the variable.
- Inspect before changing; verify after with the matching list/show/status command. For Settings, discover paths with `vbot config list [prefix]`, inspect type/default/lifecycle with `vbot config describe <path>`, then verify the effective result with `vbot config get <path> --details`.
- Treat `vbot model show` and `vbot task-model options` as authoritative for Model capabilities, voices, and accepted Task Model values. Never infer one Model's options from another Model or from generic provider documentation.
- Mutation output is a verification result, not merely an acknowledgement: Agent, Project, Channel, and Cron create/update commands print the saved resource; Project removal prints affected rooted Agents and file-copy/backup effects. Read it before issuing a separate verification call, then use `show`/`list` when the requested outcome depends on live discovery or runtime health.
- Keep Identity Agent, Project Agent, Workspace, and Project cwd separate. A generic request to create an Agent means an Identity Agent; root it in a Project when its file/shell work should run there. A Project Agent is a repo-discovered Config Agent with no Workspace, SOUL, or Memory and is created only when the user explicitly asks for a Project Team profile. See `references/agents-projects.md`.
- Follow CLI error hints (`did you mean`, candidate lists) before retrying. If another process occupies the port, report it — don't kill it.
- Finish with a compact report: commands run, what changed, verification result, and any remaining user action (complete an OAuth login, send a Telegram message, ...).

## Conventions

- Model references are `<provider>/<model-id>`, optionally pinned to a connection and credential account with `::<connection>[:<account>]` (e.g. `openai/gpt-5.2::api-key:work`).
- Project agents are addressed `agent@projekt` (e.g. `orchestrator@vbot`) in session, cron, and prompt-preview commands; a bare id means an identity agent.
- Public Settings paths use dots for fixed segments and bracketed JSON strings for dynamic keys: `web_search.provider` and `'local_models.context_windows["ollama/qwen2.5:7b"]'`. Quote the whole path when it contains brackets. JSON values (arrays, objects, booleans) are passed as one shell argument: `vbot config set skills.directories '["C:/skills"]'`.
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
| `model` | `list` `show` `refresh [<provider>]` | `references/providers.md` |
| `task-model` | `list` `targets` `options` `set` `set-option` `unset-option` `clear` | `references/providers.md` |
| `agent` | `list` `show` `create` `update` `rename` `delete` | `references/agents-projects.md` |
| `project` | `add` `list` `show` `set` `set-override` `clear-override` `rm` | `references/agents-projects.md` |
| `session` | `list` `create` `fork` `rename` `set-compaction-policy` `delete` `link-channel` | `references/agents-projects.md` |
| `channel` | `add` `list` `status` `update` `set-token` `enable` `disable` `remove` | `references/channels.md` |
| `cron` | `list` `create` `update` `delete` `enable` `disable` | `references/cron.md` |
| `config` | `list` `describe` `effective` `raw` `get` `set` `unset` `patch` | `references/configuration.md` |
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
vbot model show <provider>/<model-id>                           # complete Model capabilities and metadata
vbot task-model options text_to_speech                          # current TTS target, valid voices, saved/effective options
vbot agent update <agent-id> --model <provider>/<model-id>      # switch an agent's model
vbot config set <path> <value>                                  # change one cataloged Settings path
vbot channel status <channel-id>                                # channel health + denied inbound chats
vbot server restart                                             # apply code or unavoidable manual config edits
```
