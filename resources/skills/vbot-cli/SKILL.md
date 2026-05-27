---
name: vbot-cli
description: Configure and inspect a local vBot instance through the vbot CLI. Use when the user asks an agent to start, stop, restart, or check the server, manage agents, list providers models skills or tools, refresh models, update prompts or settings, inspect logs, or manage Telegram channels.
---

# vBot CLI

Use this skill when the user wants you to configure, inspect, or operate vBot itself from an agent session. Treat the CLI as the automation surface for app setup: use `vbot ...` commands, verify results, and report what changed.

## Rules

- Use the installed `vbot` command in examples and shell commands. Do not use `python cli/main.py` unless you are debugging the source tree entrypoint itself.
- Prefer CLI/RPC-backed changes over direct file edits. Use direct file edits only when no CLI command exists and the user explicitly asked for that level of change.
- When the user gives you an API key and asks you to configure a provider, use `vbot provider set-key --provider <id> --value <api-key>`. Do not print the key back. For bot tokens, OAuth codes, passwords, and other secrets, prefer environment variable names unless a dedicated CLI command exists.
- Start or locate the target server before running RPC-backed management commands. Only `vbot server start`, `vbot server stop`, `vbot server restart`, and `vbot server status` work without an already-running server.
- Keep commands non-interactive and automation-safe. Capture the result, then verify with a read/list/status command.
- Use `vbot <area> --help` or `vbot <area> <command> --help` when you need exact flags; every CLI area and subcommand is expected to describe its purpose.
- Treat CLI output as the source of truth for the next step. If a command returns an error with available candidates or `did you mean`, use that hint before retrying.

## Workflow

1. Identify the requested change: server lifecycle, agents, settings, providers, models, skills, tools, prompts, logs, or channels.
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
vbot skill list
vbot tool list
vbot prompt list
vbot log list
vbot agent list
vbot channel list
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

### Settings

Use `config` for raw settings keys:

```bash
vbot config
vbot config get server_port
vbot config set server_port 9000
vbot config set skill_directories '["C:/skills"]'
vbot doctor settings
```

Pass JSON values as one shell argument when setting arrays, objects, booleans, or numbers.
Use `doctor settings` before or after manual settings edits; it runs locally and does not require a running server.

### Providers And Models

Use these commands to inspect configured provider connections and model catalogs:

```bash
vbot provider list
vbot provider status --provider openrouter
vbot provider set-key --provider openrouter --value <api-key> --refresh-models
vbot model list
vbot model refresh
vbot model refresh --provider openrouter
```

Use `provider set-key` when the user gives you an API key and asks you to activate a provider. Add `--refresh-models` when the provider has a refreshable model catalog and the user wants it usable right away. Verify with `provider status --provider <id>` and `model list`. OAuth/browser login setup is not part of this CLI flow yet.

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
vbot prompt update --name tools.md --content "# Custom tools"
vbot prompt update --name tools.md --file ./tools.md
vbot prompt reset --name tools.md
vbot prompt preview --agent assistant
```

Prefer `--file` for multi-line prompt content. Do not edit prompt fragment files directly when `vbot prompt update` or `vbot prompt reset` can express the change.

### Logs

Use log commands to inspect server logs through the RPC log viewer:

```bash
vbot log list
vbot log read --file 2026-05-11
```

### Agents

Use agent commands to inspect and manage agent configuration through server RPC:

```bash
vbot agent list
vbot agent show --id assistant
vbot agent create --id coder --name Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update --id coder --model openai/gpt-5.2 --temperature 0.4 --thinking-effort high
vbot agent update --id coder --clear-temperature --clear-thinking-effort
vbot agent delete --id old-agent
```

Use `--allowed-tools` and `--allowed-skills` with zero or more values to replace the full allowlist. Quote `*` in shells that expand it.

### Telegram Channels

Use channel commands to create and operate channel configurations. Pass token environment variable names, not token values:

```bash
vbot channel add --id tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN --allow 12345
vbot channel list
vbot channel status --id tg-main
vbot channel update --id tg-main --agent assistant --allow 12345 67890
vbot channel enable --id tg-main
vbot channel disable --id tg-main
vbot channel remove --id tg-main
```

Use `channel update` for partial config changes. Omitted fields remain unchanged; `--allow` replaces the full allowlist.

## Pitfalls

- Do not assume the server is running. Check `vbot server status` first for management tasks.
- Do not edit `settings.json` directly when `vbot config set` can express the change.
- Do not edit prompt fragments directly when `vbot prompt update` or `vbot prompt reset` can express the change.
- Do not hand-edit `.env` for provider API keys when `vbot provider set-key` can express the change.
- Do not configure channels with token literals. Store the token in the environment or data-dir `.env`, then pass the variable name with `--token-env`.
- Do not edit agent JSON directly when `vbot agent create`, `vbot agent update`, or `vbot agent delete` can express the change. Workspace paths are not mutable through public agent CLI commands.
- If a command fails because another process occupies the port, do not kill it manually. Report the conflict or target a different port/data directory.

## Output Contract

When you use this skill, finish with a compact report:

- what the user asked you to configure
- which `vbot ...` commands you ran
- what changed or what state you found
- verification command and result
- any remaining user action, such as placing a secret in `.env`
