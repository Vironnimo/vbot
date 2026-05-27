# vBot CLI Command Reference

Use `vbot` as the command name after the package is installed with `pip install -e .` or `pip install -e ".[dev]"`.

## Targeting

Most commands accept these options:

```bash
--host 127.0.0.1
--port 8420
--data-dir ~/.vbot
```

Use them consistently when the user is working with a non-default instance:

```bash
vbot server status --host 127.0.0.1 --port 9000 --data-dir ./dev-data
vbot model list --host 127.0.0.1 --port 9000 --data-dir ./dev-data
```

## Server Lifecycle

```bash
vbot server start
vbot server stop
vbot server restart
vbot server status
```

Only server lifecycle commands can operate without an already-running vBot server. `start` refuses to launch over a non-vBot process on the target port.

## Config

```bash
vbot config
vbot config get <key>
vbot config set <key> <json-or-string-value>
vbot doctor settings [--data-dir <path>]
vbot doctor config [--data-dir <path>]
```

Examples:

```bash
vbot config get server_port
vbot config set server_port 9000
vbot config set skill_directories '["C:/Users/Viro/skills"]'
vbot config set extension_directories '["C:/Users/Viro/vbot-extensions"]'
vbot config set defaults '{"agent":{"temperature":0.4}}'
vbot doctor settings
vbot doctor config
```

`config set` parses JSON values first and falls back to plain strings. Quote JSON as one shell argument.
`doctor settings` validates the target data-dir `settings.json` locally and prints file/path diagnostics; it does not require a running server. `doctor config` validates the full user-editable runtime JSON bundle: settings, agents, channels, and cron jobs.

## Providers

```bash
vbot provider list
vbot provider status --provider <provider-id> [--connection <provider:connection-id>]
vbot provider set-key --provider <provider-id> [--connection <provider:connection-id>] --value <api-key> [--refresh-models]
```

Use `provider list` before model or agent configuration work to see configured provider connections and whether they are usable. Use `provider status` for one provider or connection. Use `provider set-key` to activate an API-key provider through the server: vBot resolves the configured provider credential key, writes it to the target data-dir `.env`, reloads provider credentials, and prints only the provider connection and credential key name. Add `--refresh-models` to refresh that provider's model catalog immediately after setting the key.

Examples:

```bash
vbot provider status --provider openrouter
vbot provider set-key --provider openrouter --value <api-key> --refresh-models
vbot provider set-key --provider openai --connection openai:api-key --value <api-key>
vbot provider list
vbot model refresh --provider openrouter
```

OAuth/browser login flows are not configured by `provider set-key`.

## Agents

```bash
vbot agent list
vbot agent show --id <agent-id>
vbot agent create --id <agent-id> --name <display-name> [--model <provider/model-id>] [--fallback-model <provider/model-id>] [--temperature <0..2>] [--thinking-effort <effort>] [--allowed-tools <tool> ...] [--allowed-skills <skill> ...]
vbot agent update --id <agent-id> [--name <display-name>] [--model <provider/model-id>] [--fallback-model <provider/model-id>] [--temperature <0..2>] [--clear-temperature] [--thinking-effort <effort>] [--clear-thinking-effort] [--allowed-tools <tool> ...] [--allowed-skills <skill> ...] [--current-session-id <session-id>]
vbot agent delete --id <agent-id>
```

Examples:

```bash
vbot agent list
vbot agent show --id assistant
vbot agent create --id coder --name Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update --id coder --temperature 0.4 --thinking-effort high
vbot agent update --id coder --allowed-tools read_file edit_file --allowed-skills debugging vbot-cli
vbot agent update --id coder --clear-temperature --clear-thinking-effort
vbot agent delete --id old-agent
```

Supported `--thinking-effort` values:

```text
none
minimal
low
medium
high
xhigh
max
```

`--clear-temperature` and `--clear-thinking-effort` send JSON `null` so the agent inherits current defaults. `--thinking-effort none` is the literal no-reasoning value, not a clear operation. `--allowed-tools` and `--allowed-skills` replace the full allowlist; pass the flag with no values to set an empty list.

## Models

```bash
vbot model list
vbot model refresh
vbot model refresh --provider <provider-id>
```

Examples:

```bash
vbot model refresh
vbot model refresh --provider openrouter
vbot model list
```

## Skills

```bash
vbot skill list
```

The output includes loadable skills and an `invalid skills:` section when diagnostics exist.

## Tools

```bash
vbot tool list
```

Use this to inspect public registered tools. Internal system-managed tools are omitted by the server.

## Prompts

```bash
vbot prompt list
vbot prompt update --name <fragment-name> --content <text>
vbot prompt update --name <fragment-name> --file <path>
vbot prompt reset --name <fragment-name>
vbot prompt preview --agent <agent-id>
```

Examples:

```bash
vbot prompt list
vbot prompt update --name tools.md --file ./tools.md
vbot prompt reset --name skills.md
vbot prompt preview --agent assistant
```

`prompt list` shows editable fragments, modified state, and variable placeholders. `prompt update` sends replacement content through server RPC; use `--file` for multi-line content. `prompt preview` prints token metadata and the rendered System Prompt for one agent.

## Logs

```bash
vbot log list
vbot log read --file <daily-log-name>
```

Examples:

```bash
vbot log list
vbot log read --file 2026-05-11
```

`log list` shows daily log files newest-first. `log read` returns parsed entries and a cursor for live-tail handoff.

## Telegram Channels

```bash
vbot channel add --id <channel-id> --platform telegram --agent <agent-id> --token-env <ENV_VAR> [--dm-scope <scope>] [--allow <chat-id> ...]
vbot channel update --id <channel-id> [--platform telegram] [--agent <agent-id>] [--token-env <ENV_VAR>] [--dm-scope <scope>] [--allow <chat-id> ...] [--enabled true|false]
vbot channel list
vbot channel status --id <channel-id>
vbot channel enable --id <channel-id>
vbot channel disable --id <channel-id>
vbot channel remove --id <channel-id>
```

Examples:

```bash
vbot channel add --id tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN --allow 12345
vbot channel add --id tg-work --platform telegram --agent assistant --token-env TELEGRAM_WORK_BOT_TOKEN --dm-scope per_peer --allow 12345 67890
vbot channel update --id tg-work --agent coder --allow 12345 67890 24680
vbot channel enable --id tg-main
vbot channel status --id tg-main
```

`channel update` is a partial update: omitted fields remain unchanged. Passing `--allow` replaces the full allowed chat-id list. Use `--enabled true` or `--enabled false` for config-level enabled state; use `channel enable` and `channel disable` for the common on/off operation.

Supported `--dm-scope` values:

```text
per_conversation
main
per_peer
per_account_channel_peer
```

## Verification Pattern

After every change, run a read command from the same area:

```bash
vbot config get <key>
vbot doctor settings
vbot doctor config
vbot agent show --id <agent-id>
vbot agent list
vbot channel status --id <channel-id>
vbot channel list
vbot provider list
vbot provider status --provider <provider-id>
vbot model list
vbot skill list
vbot tool list
vbot prompt list
vbot log list
vbot server status
```
