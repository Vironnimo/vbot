---
name: vbot-cli
description: Configure and inspect a local vBot instance through the vbot CLI. Use when the user asks an agent to start, stop, restart, or check the server, manage agents, projects, or sessions, talk to a project agent as agent@projekt, list providers models skills or tools, refresh models, connect OAuth providers, bind task models, update prompts or settings, inspect logs or debug traces, schedule cron jobs, update the vBot installation itself, or manage Telegram or Discord channels.
---

# vBot CLI

Use this skill when the user wants you to configure, inspect, or operate vBot itself from an agent session. Treat the CLI as the automation surface for app setup: use `vbot ...` commands, verify results, and report what changed.

## Rules

- Use the installed `vbot` command in examples and shell commands. Do not use `python cli/main.py` unless you are debugging the source tree entrypoint itself.
- Primary identifiers are positional: `vbot agent show assistant`, `vbot channel remove tg-main`, `vbot cron delete <job-id>`. Secondary parameters are flags. There are no `--id`-style flags for the main target.
- Prefer CLI/RPC-backed changes over direct file edits. Use direct file edits only when no CLI command exists and the user explicitly asked for that level of change.
- When the user gives you an API key and asks you to configure a provider, use `vbot provider set-key <provider-id> <api-key>`. Do not print the key back. For bot tokens, OAuth codes, passwords, and other secrets, prefer environment variable names unless a dedicated CLI command exists.
- Start or locate the target server before running RPC-backed management commands. Only `vbot server start`, `vbot server stop`, `vbot server restart`, `vbot server status`, `vbot update`, `vbot autostart`, and `vbot doctor ...` work without an already-running server.
- Keep commands non-interactive and automation-safe. Capture the result, then verify with a read/list/status command.
- Use `vbot <area> --help` or `vbot <area> <command> --help` when you need exact flags; every subcommand help text includes a usage example.
- Treat CLI output as the source of truth for the next step. If a command returns an error with available candidates or `did you mean`, use that hint before retrying.

## Workflow

1. Identify the requested change: server lifecycle, agents, projects, sessions, settings, providers, models, task-model bindings, skills, tools, prompts, logs, cron jobs, debug traces, or channels.
2. Resolve the target instance. Use defaults unless the user gives a host, port, or data directory. Add `--host`, `--port`, and `--data-dir` to every command that must target a non-default instance.
3. Check reachability:

```bash
vbot server status
```

4. If the task needs RPC-backed management and the server is not running, start it:

```bash
vbot server start
```

5. Inspect current state before changing it. Examples:

```bash
vbot config
vbot provider list
vbot model list
vbot task-model list
vbot skill list
vbot tool list
vbot prompt list
vbot log list
vbot agent list
vbot project list
vbot channel list
vbot cron list
```

6. Apply the smallest command that satisfies the request. See `references/commands.md` for command shapes and examples.
7. Verify the result with the matching status/list/get command.
8. Report the commands you ran and the outcome. Do not print secret values.

## Common Tasks

### Server

Use server lifecycle commands for local process control:

```bash
vbot server start
vbot server status
vbot server restart
vbot server stop
```

### Updating vBot

Use `vbot update` to update the installation from its git checkout and restart the server. It auto-detects whether the checkout tracks a branch (pulls and rebuilds the WebUI) or a release tag (fetches the latest release and its prebuilt WebUI), and never touches the `~/.vbot` data directory.

```bash
vbot update
vbot update --stash
vbot update --no-restart
```

If the checkout has local changes to tracked files, `update` refuses; re-run with `--discard` to drop them or `--stash` to keep them (reapplied after the update).

### Autostart

Use `vbot autostart` to make the server start automatically and bring it up now:

```bash
vbot autostart enable
vbot autostart status
vbot autostart disable
```

`enable` registers OS autostart (Windows Task Scheduler logon task, or a Linux systemd user unit) and starts the server immediately. On Windows this needs an elevated (Administrator) terminal to create the task. `disable` removes the autostart entry but leaves a running server untouched. The installers enable autostart by default (pass `--no-autostart` to opt out).

### Settings

Use `config` for raw settings keys:

```bash
vbot config
vbot config get server_port
vbot config set server_port 9000
vbot config set skill_directories '["C:/skills"]'
vbot doctor settings
vbot doctor config
```

Pass JSON values as one shell argument when setting arrays, objects, booleans, or numbers.
Use `doctor settings` before or after manual settings edits; it runs locally and does not require a running server. Use `doctor config` after manual edits to any user-editable runtime JSON such as settings, agents, channels, or cron jobs.

### Providers And Models

Use these commands to inspect configured provider connections and model catalogs:

```bash
vbot provider list
vbot provider status openrouter
vbot provider set-key openrouter <api-key> --refresh-models
vbot model list
vbot model refresh
vbot model refresh openrouter
```

Use `provider set-key` when the user gives you an API key and asks you to activate a provider. Add `--refresh-models` when the provider has a refreshable model catalog and the user wants it usable right away. Verify with `provider status <provider-id>` and `model list`.

A connection can hold multiple credential **accounts** (named slots; default slot is `default`). Add `--account <id>` to `set-key`, `unset-key`, `connect`, `disconnect`, and `connect-status` to target a named slot (lowercase letters, digits, underscores, e.g. `work`). `provider list` and `provider status` show each connection's accounts with their usable state and source. Models can pin an account via the suffix `<provider>/<model>::<connection>:<account>`:

```bash
vbot provider set-key openrouter <api-key> --account work
vbot provider status openrouter
```

For OAuth/subscription connections, use the device flow (add `--account <id>` for an additional login on the same connection):

```bash
vbot provider connect openai --connection openai:subscription
vbot provider connect-status openai --connection openai:subscription
vbot provider disconnect openai --connection openai:subscription
```

`connect` prints a user code and verification URL for the user to open; the server polls in the background. Relay the code and URL to the user, then check `connect-status` until it reports `connected=yes`.

### Task Models

Use task-model commands to bind specialized tasks (speech-to-text, text-to-speech, image generation, text embedding, video generation) to a model target:

```bash
vbot task-model list
vbot task-model targets speech_to_text
vbot task-model set text_to_speech openai/gpt-4o-mini-tts::api-key --options '{"voice": "alloy"}'
vbot task-model clear image_generation
```

Run `task-model targets <task-type>` first to see valid target ids, and `task-model options <task-type> <target-id>` for the option schema before passing `--options`.

### Skills

Use this to list loadable skills and invalid skill diagnostics:

```bash
vbot skill list
```

### Tools

Use this to inspect public tools exposed to agents:

```bash
vbot tool list
```

### Prompts

Use prompt commands to inspect and update editable System Prompt fragments through server RPC:

```bash
vbot prompt list
vbot prompt update tools.md --content "# Custom tools"
vbot prompt update tools.md --file ./tools.md
vbot prompt reset tools.md
vbot prompt preview assistant
```

Prefer `--file` for multi-line prompt content. Do not edit prompt fragment files directly when `vbot prompt update` or `vbot prompt reset` can express the change.

### Logs

Use log commands to inspect server logs through the RPC log viewer:

```bash
vbot log list
vbot log read 2026-05-11.log
```

### Agents

Use agent commands to inspect and manage agent configuration through server RPC:

```bash
vbot agent list
vbot agent show assistant
vbot agent create coder Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update coder --model openai/gpt-5.2 --temperature 0.4 --thinking-effort high
vbot agent update coder --memory-prompt-mode agent_user --custom-system-prompt true
vbot agent update coder --clear-temperature --clear-thinking-effort
vbot agent delete old-agent
```

Use `--allowed-tools` and `--allowed-skills` with zero or more values to replace the full allowlist. Quote `*` in shells that expand it.

### Projects

A **project** points vBot at a repo directory (its `cwd`) and exposes the agents discovered in that repo (its **team**). Use project commands to add, inspect, configure, and remove projects:

```bash
vbot project add ./my-repo --name vbot --default-agent orchestrator --auto-load AGENTS.md
vbot project list
vbot project show vbot
vbot project set vbot --default-agent builder
vbot project rm vbot
```

`project add` and `project show` print the **scan preview**: the team (callable agents found in the repo) plus a report of anything unclean under what exists (bad or unconfigured model, slug collision, unslugifiable name). An empty folder is a valid project with an empty team and a clean report — not an error. `project add` only needs the repo path; everything else is optional. `project rm` archives the project's runtime anchor (never the repo) and prints the archive path; it is blocked while a project agent has an active or queued run (`project_busy`) or a cron job points at a project agent (`project_in_use`) — clear those first.

Talk to a project's agents with the address form `agent@projekt` (see Sessions and Cron Jobs below). vBot reads the repo to discover the team but never writes it.

### Sessions

Use session commands to inspect and manage an agent's chat sessions. The positional agent argument accepts either a bare identity agent (`assistant`) or a project agent in the address form `agent@projekt` (`orchestrator@vbot`):

```bash
vbot session list orchestrator@vbot
vbot session create orchestrator@vbot --make-current
vbot session list assistant
vbot session link-channel assistant <session-id> --channel tg-main --conversation 12345
```

A bare agent (no `@`) behaves exactly as before (identity agent). `agent@projekt` opens the session under that project, against the project's scanned team. `link-channel` routes a session's outbound replies to a channel conversation, such as a Telegram chat.

### Cron Jobs

Use cron commands to schedule recurring or one-time agent prompts:

```bash
vbot cron list
vbot cron create assistant --prompt "Check the news" --cron "0 9 * * *" --timezone Europe/Berlin
vbot cron create builder@vbot --prompt "Nightly build" --cron "0 2 * * *"
vbot cron create assistant --prompt "Remind me" --at 2026-07-01T09:00:00
vbot cron update <job-id> --status paused
vbot cron enable <job-id>
vbot cron disable <job-id>
vbot cron delete <job-id>
```

`create` requires exactly one of `--cron <expression>` (recurring) or `--at <iso-datetime>` (one-time). The agent argument takes a bare agent or the `agent@projekt` address form to target a project agent; firing such a job runs in that project. `cron list` shows the target in the same address form (`builder@vbot` for a project target, `assistant` for an identity target).

### Debug Traces

Use debug commands to inspect raw provider traffic when diagnosing provider or model problems:

```bash
vbot debug status
vbot debug probe openai --connection openai:api-key
vbot debug traces
vbot debug trace <trace-id>
vbot debug clear
```

`traces`, `trace`, and `probe` need debug mode enabled server-side (`vbot config set debug '{"enabled": true}'`); `status` and `clear` always work.

### Messaging Channels

Use channel commands to create and operate channel configurations. Pass token environment variable names, not token values:

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN --allow 12345
vbot channel list
vbot channel status tg-main
vbot channel update tg-main --agent assistant --allow 12345 67890
vbot channel enable tg-main
vbot channel disable tg-main
vbot channel remove tg-main
```

Use `channel update` for partial config changes. Omitted fields remain unchanged; `--allow` replaces the full allowlist.

Discord uses the same commands with Discord channel ids:

```bash
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel status dc-main
```

The Discord bot also needs the Message Content Intent enabled in the Developer Portal. `--allow` takes channel or thread ids, not guild ids.

## Pitfalls

- Do not assume the server is running. Check `vbot server status` first for management tasks.
- Do not edit `settings.json` directly when `vbot config set` can express the change.
- Do not edit prompt fragments directly when `vbot prompt update` or `vbot prompt reset` can express the change.
- Do not hand-edit `.env` for provider API keys when `vbot provider set-key` can express the change.
- Do not configure channels with token literals. Store the token in the environment or data-dir `.env`, then pass the variable name with `--token-env`.
- Do not edit agent JSON directly when `vbot agent create`, `vbot agent update`, or `vbot agent delete` can express the change. Workspace paths are not mutable through public agent CLI commands.
- Do not edit cron job JSON directly when `vbot cron create`, `vbot cron update`, or `vbot cron delete` can express the change.
- If direct JSON edits were required, run `vbot doctor config` before relying on the instance.
- If a command fails because another process occupies the port, do not kill it manually. Report the conflict or target a different port/data directory.

## Output Contract

When you use this skill, finish with a compact report:

- what the user asked you to configure
- which `vbot ...` commands you ran
- what changed or what state you found
- verification command and result
- any remaining user action, such as placing a secret in `.env` or completing an OAuth device-code login
