---
name: vbot-cli
description: Configure, inspect, and operate vBot through its CLI. Use for Agents, Projects, Sessions, Settings, Tools, Skills, Memory, prompts, Channels (Telegram/Discord), scheduling, Extensions/MCP, Provider credentials and limits, Models and specialized Task Models (including TTS/STT voices and options), server lifecycle, updates, file locations, stored-state recovery, logs, and usage diagnostics.
---

# vBot CLI

The `vbot` CLI is the automation surface for configuring and operating a vBot instance: run the command, verify the result, report what changed. The server runs Agents and owns application state. The CLI, WebUI, Desktop, and Channels all access that same system; changing configuration through the CLI changes the instance they use.

## MCP connections

For MCP installation, configuration, or use, read `references/mcp.md`. It covers setup from a supplied link on the vBot machine, Agent grants, targeted discovery, saved-result reading, user input, and verification through an actual application operation.

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
- Management commands need a running target server. Local commands (`home`, `server`, `desktop`, `update`, `uninstall`, `autostart`, `doctor`) do not require one already running. Session-store maintenance has both local and server-backed commands; read `references/session-store.md`. When operating the instance hosting the current Run, execute the needed command directly. Use `server status` when targeting another instance or troubleshooting connectivity, and start a stopped local target when the task requires it.
- Keep the same target throughout a task. Append `--host <host> --port <port>` to each management command for an explicit server target; use `--data-dir <path>` for the matching local instance configuration. The data directory does not select a different server once host and port are fixed. Defaults and local-command exceptions are explained in `references/server.md`; inspect the exact command help before passing target flags.
- Prefer CLI commands over direct file edits — settings, agents, channels, cron jobs, Bootstrap jobs, prompt blocks, and provider keys all have commands. If a manual JSON edit was unavoidable, validate with `vbot doctor config`.
- Before `vbot update` when it will restart the server, arm and verify a one-shot Bootstrap in the current Session so the Run resumes after startup and checks the result. Follow `references/server.md`; do not create one for `--no-restart` unless the user separately wants a later-startup check.
- Never echo secrets in output. API keys go through `provider set-key`, extension secrets through `extensions <name> set <field> --stdin`, and managed channel tokens through `channel add ... --token-stdin` or `channel set-token ... --stdin`. Channel tokens never belong in shell arguments; use `--token-env` only when an external deployment environment already owns the variable.
- Inspect only the state needed to choose a valid change: exact ids, current lists you intend to replace, and relevant constraints. Reuse observations already available for the same target. For Settings, use `vbot config list [prefix]` to discover paths and `vbot config describe <path>` for the current value, type, default, and application lifecycle. Use `config patch` when several paths must change atomically.
- Treat `vbot model show` and `vbot task-model options` as authoritative for Model capabilities, voices, and accepted Task Model values. Never infer one Model's options from another Model or from generic provider documentation.
- Read the mutation result first. Agent, Project, Channel, Cron, and Bootstrap create/update commands return saved state; this can verify a configuration change without another read. Saved state does not prove a Channel can receive messages, an Agent can complete a Run, or a scheduled job has fired. Check the corresponding runtime outcome when the task requires it. For Settings, distinguish the active value from `pending` and `restart_required`; use `config get <path> --details` when a fresh observation is needed.
- Keep Identity Agent, Project Agent, Workspace, and Project cwd separate. A generic request to create an Agent means an Identity Agent; root it in a Project when its file/shell work should run there. A Project Agent is a repo-discovered Config Agent with no Workspace, SOUL, or Memory and is created only when the user explicitly asks for a Project Team profile. See `references/agents-projects.md`.
- Read the full output and exit code. Help exits 0; invalid command syntax exits 2; management failures normally exit 1. `server status` can exit 0 while reporting a stopped server or port conflict, so its text determines readiness. A timeout or lost/malformed response may leave a mutation applied: inspect the same target before retrying. If a multi-step command reports an earlier success and a later failure, continue from the failed step. Follow candidate lists and recovery hints; report a non-vBot port occupant without killing it.
- Finish with a compact report: commands run, what changed, verification result, and any remaining user action (complete an OAuth login, send a Telegram message, ...).

## Conventions

- Model references are `<provider>/<model-id>`, optionally pinned to a connection and credential account with `::<connection>[:<account>]` (e.g. `openai/gpt-5.2::api-key:work`).
- Project agents are addressed `agent@projekt` (e.g. `orchestrator@vbot`) in session, cron, and prompt-preview commands; a bare id means an identity agent.
- Public Settings paths use dots for fixed segments and bracketed JSON strings for dynamic keys: `web_search.provider` and `'local_models.context_windows["ollama/qwen2.5:7b"]'`. Quote the whole path when it contains brackets. JSON values (arrays, objects, booleans) are passed as one shell argument: `vbot config set skills.directories '["C:/skills"]'`.
- List-replacing flags replace the full list: pass every value that should remain. Agent Tool access uses `--tool-access-mode` with `--tool-allow`/`--tool-deny`; Projects use `--allowed-tools`. Skills, delegation, auto-loaded files, and Channel allowlists have their own list flags. Read the area reference for empty-list and omission behavior; do not transfer wildcard or clearing rules between them.

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
| `provider` | `list` `status` `usage` `usage-history` `usage-history-clear` `enable` `disable` `set-key` `unset-key` `connect` `connect-status` `disconnect` `custom-list` `custom-save` `custom-delete` | `references/providers.md` |
| `model` | `list` `show` `refresh [<provider>]` | `references/providers.md` |
| `task-model` | `list` `status` `targets` `options` `set` `set-option` `unset-option` `clear` | `references/providers.md` |
| `agent` | `list` `show` `create` `update` `rename` `reorder` `delete` | `references/agents-projects.md` |
| `project` | `add` `list` `show` `set` `set-override` `clear-override` `detect` `rm` | `references/agents-projects.md` |
| `memory` | `list` `add` `replace` `remove` — pinned Memory entries per Agent | `references/memory.md` |
| `session` | `list` `create` `fork` `rename` `set-compaction-policy` `delete` `link-channel` | `references/agents-projects.md` |
| `session-store` | `status` `snapshot list` `snapshot create` `snapshot verify` `snapshot restore` `incident acknowledge` | `references/session-store.md` |
| `channel` | `add` `list` `status` `update` `set-token` `enable` `disable` `remove` `identity` `access` `grant-admin` `revoke-admin` | `references/channels.md` |
| `cron` | `list` `create` `update` `delete` `enable` `disable` | `references/cron.md` |
| `bootstrap` | `list` `create` `update` `delete` `enable` `disable` | `references/bootstrap.md` |
| `config` | `list` `describe` `effective` `raw` `get` `set` `unset` `patch` | `references/configuration.md` |
| `prompt` | `list` `update` `reset` `create` `remove` `set-layout` `reset-layout` `preview` | `references/configuration.md` |
| `extensions` | `list` `reload` `<name>` `<name> set` `<name> operations` `<name> <operation>` `enable` `disable` | `references/configuration.md` |
| `log` | `list` `read` | `references/diagnostics.md` |
| `debug` | `status` `probe` `traces` `trace` `clear` | `references/diagnostics.md` |
| `statistics` | `overview` `usage` `runs` `compactions` `errors` `tools` `skills` | `references/diagnostics.md` |
| `skill` | `list` `inventory` `read` `create` `update` `delete` `write-file` `remove-file` `disable` `enable` `share` `unshare` | `references/skills.md` |
| `tool` | `list` — public tools exposed to agents | — |

First-time Telegram bot setup (BotFather, token, chat-id discovery, privacy mode): follow `references/telegram-setup.md`.

## Quick reference

The most common single commands:

```bash
vbot provider set-key <provider-id> <api-key> --refresh-models  # activate a provider with a user-supplied key
vbot model list --task chat                                    # discover exact chat Model ids; inspect reachability
vbot model show <provider>/<model-id>                           # complete Model capabilities and metadata
vbot task-model options text_to_speech                          # current TTS target, valid voices, saved/effective options
vbot agent update <agent-id> --model <provider>/<model-id>      # switch an agent's model
vbot config set <path> <value>                                  # change one cataloged Settings path
vbot channel status <channel-id>                                # channel health + denied inbound chats
vbot bootstrap create --current-session --name "Verify restart" --prompt "Check status and logs, then report" --mode once
vbot server restart                                             # apply code or unavoidable manual config edits
```
