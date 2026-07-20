# Messaging Channels (Telegram, Discord)

For first-time Telegram setup — BotFather, token storage, chat-id discovery, group privacy mode — follow `telegram-setup.md` instead of improvising.

```bash
vbot channel add <channel-id> --platform telegram|discord --agent <agent-id> --token-env <ENV_VAR> [--dm-scope <scope>] [--allow <chat-id> ...] [group-policy flags]
vbot channel list
vbot channel status <channel-id>
vbot channel update <channel-id> [--platform ...] [--agent ...] [--token-env ...] [--dm-scope ...] [--allow <chat-id> ...] [--enabled true|false] [group-policy flags]
vbot channel enable <channel-id>
vbot channel disable <channel-id>
vbot channel remove <channel-id>
```

Rules and gotchas:

- `--token-env` takes the environment variable name, never the token value. A token newly added to the data-dir `.env` needs `vbot server restart` before the channel can use it.
- `--allow` empty (or omitted on `add`) denies all inbound chats — a safe starting point that the discovery flow expects. On `update`, `--allow` replaces the whole list: pass every id that should stay allowed. All other `update` fields are partial (omitted = unchanged).
- Chat-id discovery: have the user message the bot, then read the denied inbound chats (chat id, kind, name, last seen) from `channel status`, and allow the id via `update --allow`. The denied list is in-memory — cleared on channel restart (a `channel update` restarts the channel too) and capped at the most recent 20 chats.
- `channel status` reports enabled/running/failed with a failure reason. A token-related failure usually means the env var is missing or the server was not restarted after editing `.env`.
- `--dm-scope`: `per_conversation` (default), `main`, `per_peer`, `per_account_channel_peer`.
- Telegram allowlist entries are chat ids; groups have negative ids (e.g. `-100123456789`). Discord entries are channel/thread ids, not guild ids, and the bot needs the Message Content Intent enabled in the Discord Developer Portal.
- Group-policy flags are `--response-mode mention|all`, `--mention-pattern <pattern> ...`, `--owner-user <platform-user-id> ...`, and `--observe-unaddressed true|false`. Mention and owner flags replace their complete lists on `update`; pass them with no values to clear. `response_mode=mention` answers addressed group messages, while `all` answers every allowed message; `observe_unaddressed` lets the Agent receive unaddressed group context without answering it.
- `add`, `update`, `enable`, and `disable` return the saved Channel config so the caller can verify routing and group policy immediately. Use `status` separately for listener health and denied-chat discovery.

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel update tg-main --allow 12345 67890
```
