# Messaging Channels

For first-time Telegram setup — BotFather, token storage, chat-id discovery, group privacy mode — follow `telegram-setup.md` instead of improvising.

```bash
vbot channel add <channel-id> --platform telegram|discord|slack|mattermost --agent <agent-id> (--token-stdin | --token-env <ENV_VAR>) [--dm-scope <scope>] [--allow <chat-id> ...] [group-policy flags]
vbot channel list
vbot channel status <channel-id>
vbot channel update <channel-id> [--platform ...] [--agent ...] [--token-env ...] [--dm-scope ...] [--allow <chat-id> ...] [--enabled true|false] [group-policy flags]
vbot channel token set <channel-id> --stdin [--slot bot|app]
vbot channel identity <channel-id> [--user <platform-user-id>]
vbot channel access <channel-id> --group <group-id>
vbot channel admin grant <channel-id> --group <group-id> --user <platform-user-id>
vbot channel admin revoke <channel-id> --group <group-id> --user <platform-user-id>
vbot channel enable <channel-id>
vbot channel disable <channel-id>
vbot channel remove <channel-id>
```

Start by reading `channel list` for the saved configuration and `channel status <id>` for listener health. Use the exact existing id when updating.

Rules and gotchas:

- Prefer `--token-stdin` on `add`: the server stores the token under a collision-free managed key in the data-dir `.env`, reloads Credentials live, and starts the Channel without a server restart. Never put the token itself in a CLI argument.
- `channel token set <id> --stdin` rotates the configured Channel credential, reloads Credentials live, and restarts only that Channel adapter when its effective token changed. Its result reports the effective source plus immediate enabled/running/failed state; follow with `channel status` because an upstream token rejection may arrive asynchronously.
- `--token-env` is the advanced path for an externally managed process environment variable. Process environment takes precedence over the data-dir `.env`; `token set` reports `applied=no` and does not restart the adapter while such an override is active.
- `--allow` empty (or omitted on `add`) denies all inbound chats — a safe starting point that the discovery flow expects. On `update`, `--allow` replaces the whole list: pass every id that should stay allowed. All other `update` fields are partial (omitted = unchanged).
- Chat-id discovery: have the user message the bot, then read the denied inbound chats (chat id, kind, name, last seen) from `channel status`, and allow the id via `update --allow`. The denied list is in-memory — cleared on channel restart (a `channel update` restarts the channel too) and capped at the most recent 20 chats.
- `channel status` reports enabled/running/failed with a failure reason. A token-related failure means the credential is missing or the platform rejected it; use `channel token set --stdin` instead of editing `.env` manually.
- `--dm-scope`: `per_conversation` (default), `main`, `per_peer`, `per_account_channel_peer`.
- Telegram allowlist entries are chat ids; groups have negative ids (e.g. `-100123456789`). Discord entries are channel/thread ids, not guild ids, and the bot needs the Message Content Intent enabled in the Discord Developer Portal.
- Group-policy flags are `--response-mode mention|all`, `--mention-pattern <pattern> ...`, and `--observe-unaddressed true|false`. Mention patterns replace their complete list on `update`; pass the flag with no values to clear it. `response_mode=mention` answers addressed group messages, while `all` answers every allowed message; `observe_unaddressed` lets the Agent receive unaddressed group context without answering it.
- Group access uses exactly `admin` and `member`. Set the Channel account's own identity from a previously seen participant with `channel identity <id> --user <user-id>`; it is an admin in every group and cannot be demoted. `channel access` lists one group's durable participants and roles. `admin grant` and `admin revoke` are additive, idempotent one-user actions; neither replaces the group list or restarts the adapter.
- Members may authorize only `web_search` and `web_fetch`; admins retain the Agent's existing Tool access. Group Commands and reserved Run buttons require admin. A grant affects new messages only, while a revoke blocks non-web Tools before the next Tool call of an active admin Run.
- `add`, `update`, `enable`, and `disable` return the saved Channel config so the caller can verify routing and group policy immediately. Use `status` separately for listener health and denied-chat discovery.

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-stdin
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel token set tg-main --stdin
vbot channel update tg-main --allow 12345 67890
vbot channel identity tg-main --user 50
vbot channel admin grant tg-main --group -100123456789 --user 51
```


## WhatsApp: existing account, self chat only

WhatsApp uses linked-device pairing with the user's existing account. It needs Node.js 22 or newer and npm on the vBot server. Create it without a token and with `--allow self`; creation leaves it disabled. The only supported inbound and outbound target is literal `self`. Other chats cannot trigger Runs. This unofficial connection may be restricted by WhatsApp; do not describe it as a Business API bot or promise account eligibility.

```bash
vbot channel add wa-main --platform whatsapp --agent assistant --allow self
vbot channel whatsapp setup wa-main
vbot channel whatsapp status wa-main
vbot channel whatsapp pair wa-main
```

Setup runs asynchronously. Read `whatsapp status` until `installed: True`; on failure follow the returned error before retrying setup. Pair only after installation. Ask the user to open Settings > Channels and scan the QR from WhatsApp's Settings > Linked devices > Link a device. QR images and linked-device credentials are private; never copy them into chat or logs. `state: connected` confirms the connection, but only a live self-chat exchange establishes end-to-end delivery. After `logged_out`, use `whatsapp pair <id> --reset` to request a new QR. Ordinary restarts reuse the saved device. Re-pairing does not revoke the old device remotely; the user removes it in WhatsApp.

## Slack and Mattermost

Slack requires a bot token and a separate Socket Mode app token. Use distinct variable names with `--token-env` and `--app-token-env`. If credentials are not configured yet, create with `--disabled`, save each token from stdin, then enable:

```bash
vbot channel add slack-main --platform slack --agent assistant --token-env SLACK_BOT_TOKEN --app-token-env SLACK_APP_TOKEN --disabled
vbot channel token set slack-main --stdin
vbot channel token set slack-main --slot app --stdin
vbot channel enable slack-main
```

The Slack app must enable Socket Mode; its app-level token needs `connections:write`. Bot scopes: `chat:write`, `files:read`, `files:write`, and the `read`/`history` scopes for `channels`, `groups`, `im` and `mpim`. Subscribe to `message.channels`, `message.groups`, `message.im` and `message.mpim`; enable the App Home Messages tab and invite the bot to each target channel. Reinstall after scope changes. An `app_mention` subscription alone is insufficient.

Mattermost requires `--server-url <https://server[/subpath]>` and a bot token via `--token-stdin` or `--token-env`. Use the server root without `/api/v4`, credentials, query or fragment. The server administrator may need to enable bot accounts and add the bot to teams/channels.

Both platforms use conversation IDs as allowlist entries, not user/team/workspace IDs. Use denied-chat discovery and preserve IDs as strings. `channel status` additionally reports `connected`; `running=yes` alone means the listener task exists. Mention-only groups require a bot mention or configured wake regex even in threads. Threads share their parent conversation Session. These adapters support text and files, but not buttons, typing indicators, offline history backfill or fetched quoted-message content. Disable a running Channel and wait for shutdown before removing it. Never infer live delivery from mocked tests or saved configuration.
