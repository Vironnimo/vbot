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
```

Examples:

```bash
vbot config get server_port
vbot config set server_port 9000
vbot config set skill_directories '["C:/Users/Viro/skills"]'
vbot config set extension_directories '["C:/Users/Viro/vbot-extensions"]'
vbot config set defaults '{"agent":{"temperature":0.4}}'
```

`config set` parses JSON values first and falls back to plain strings. Quote JSON as one shell argument.

## Providers

```bash
vbot provider list
```

Use this before model or agent configuration work to see configured provider connections and whether they are usable.

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

## Telegram Channels

```bash
vbot channel add --id <channel-id> --platform telegram --agent <agent-id> --token-env <ENV_VAR> [--dm-scope <scope>] [--allow <chat-id> ...]
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
vbot channel enable --id tg-main
vbot channel status --id tg-main
```

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
vbot channel status --id <channel-id>
vbot provider list
vbot model list
vbot skill list
vbot server status
```
