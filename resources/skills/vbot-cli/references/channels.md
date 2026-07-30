# Messaging Channels (Telegram, Discord)

For first-time Telegram setup — BotFather, token storage, chat-id discovery, group privacy mode — follow `telegram-setup.md` instead of improvising.

```bash
vbot channel add <channel-id> --platform telegram|discord --agent <agent-id> (--token-stdin | --token-env <ENV_VAR>) [--dm-scope <scope>] [--allow <chat-id> ...] [group-policy flags]
vbot channel list
vbot channel status <channel-id>
vbot channel update <channel-id> [--platform ...] [--agent ...] [--token-env ...] [--dm-scope ...] [--allow <chat-id> ...] [--enabled true|false] [group-policy flags]
vbot channel set-token <channel-id> --stdin
vbot channel identity <channel-id> [--user <platform-user-id>]
vbot channel access <channel-id> --group <group-id>
vbot channel grant-admin <channel-id> --group <group-id> --user <platform-user-id>
vbot channel revoke-admin <channel-id> --group <group-id> --user <platform-user-id>
vbot channel enable <channel-id>
vbot channel disable <channel-id>
vbot channel remove <channel-id>
```

Rules and gotchas:

- Prefer `--token-stdin` on `add`: the server stores the token under a collision-free managed key in the data-dir `.env`, reloads Credentials live, and starts the Channel without a server restart. Never put the token itself in a CLI argument.
- `channel set-token <id> --stdin` rotates the configured Channel credential, reloads Credentials live, and restarts only that Channel adapter when its effective token changed. Its result reports the effective source plus immediate enabled/running/failed state; follow with `channel status` because an upstream token rejection may arrive asynchronously.
- `--token-env` is the advanced path for an externally managed process environment variable. Process environment takes precedence over the data-dir `.env`; `set-token` reports `applied=no` and does not restart the adapter while such an override is active.
- `--allow` empty (or omitted on `add`) denies all inbound chats — a safe starting point that the discovery flow expects. On `update`, `--allow` replaces the whole list: pass every id that should stay allowed. All other `update` fields are partial (omitted = unchanged).
- Chat-id discovery: have the user message the bot, then read the denied inbound chats (chat id, kind, name, last seen) from `channel status`, and allow the id via `update --allow`. The denied list is in-memory — cleared on channel restart (a `channel update` restarts the channel too) and capped at the most recent 20 chats.
- `channel status` reports enabled/running/failed with a failure reason. A token-related failure means the credential is missing or the platform rejected it; use `channel set-token --stdin` instead of editing `.env` manually.
- `--dm-scope`: `per_conversation` (default), `main`, `per_peer`, `per_account_channel_peer`.
- Telegram allowlist entries are chat ids; groups have negative ids (e.g. `-100123456789`). Discord entries are channel/thread ids, not guild ids, and the bot needs the Message Content Intent enabled in the Discord Developer Portal.
- Group-policy flags are `--response-mode mention|all`, `--mention-pattern <pattern> ...`, and `--observe-unaddressed true|false`. Mention patterns replace their complete list on `update`; pass the flag with no values to clear it. `response_mode=mention` answers addressed group messages, while `all` answers every allowed message; `observe_unaddressed` lets the Agent receive unaddressed group context without answering it.
- Group access uses exactly `admin` and `member`. Set the Channel account's own identity from a previously seen participant with `channel identity <id> --user <user-id>`; it is an admin in every group and cannot be demoted. `channel access` lists one group's durable participants and roles. `grant-admin` and `revoke-admin` are additive, idempotent one-user actions; neither replaces the group list or restarts the adapter.
- Members may authorize only `web_search` and `web_fetch`; admins retain the Agent's existing Tool access. Group Commands and reserved Run buttons require admin. A grant affects new messages only, while a revoke blocks non-web Tools before the next Tool call of an active admin Run.
- `add`, `update`, `enable`, and `disable` return the saved Channel config so the caller can verify routing and group policy immediately. Use `status` separately for listener health and denied-chat discovery.

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-stdin
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel set-token tg-main --stdin
vbot channel update tg-main --allow 12345 67890
vbot channel identity tg-main --user 50
vbot channel grant-admin tg-main --group -100123456789 --user 51
```
