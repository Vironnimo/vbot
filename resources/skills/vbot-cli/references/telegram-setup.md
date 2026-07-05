# Telegram Bot Setup

End-to-end walkthrough for connecting a Telegram bot to vBot: create the bot, store the token, create the channel, discover chat ids, and configure group behavior. Use this when the user asks to "set up Telegram" and no bot or channel exists yet. The user performs the Telegram-side steps (BotFather, sending messages); you run the vBot-side commands and guide them through the rest.

## 1. Create the bot with BotFather

The user creates the bot in their Telegram app — walk them through it:

1. Open a chat with `@BotFather` in Telegram.
2. Send `/newbot`.
3. Choose a display name (free text) and then a username (must be unique and end in `bot`, e.g. `julian_assistant_bot`).
4. BotFather replies with the **bot token** (format `123456789:AA...`). Treat it as a secret.

## 2. Store the token

Put the token into the data-dir `.env` (default `~/.vbot/.env`) under a descriptive variable name:

```text
TELEGRAM_BOT_TOKEN=123456789:AA...
```

The user can paste it there themselves, or hand it to you to write. Never echo the token back into chat, and never pass the token value on the command line — channels reference the **variable name** via `--token-env`.

The data-dir `.env` is read at server startup, so a newly added token needs a restart before the channel can use it:

```bash
vbot server restart
```

(A variable set in the process environment before server start works too and takes precedence over `.env`.)

## 3. Create and verify the channel

Create the channel with the env-var name. The allowlist can start empty — an empty allowlist denies all inbound chats, which is safe and is exactly what the discovery flow in step 4 expects:

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN
vbot channel status tg-main
```

`status` should report `running=yes`. `failed=yes` with a token-related failure reason usually means the env var is missing or the server was not restarted after editing `.env`.

## 4. Discover and allow chat ids

Inbound messages are only accepted from chats on the channel's allowlist. You rarely know a chat's id up front — use the built-in discovery flow instead of third-party id bots:

1. The user sends the bot any message (for a direct chat: open the bot, press Start or send text; for a group: add the bot to the group and send a message there).
2. The message is rejected (not on the allowlist) but recorded. Read it from status:

```bash
vbot channel status tg-main
```

The output lists denied inbound chats with their chat id, kind (direct/group), sender or group name, and last-seen time. Denied chats are also logged at info level in the server log.

3. Allow the chat. `--allow` replaces the whole list, so pass all ids that should stay allowed:

```bash
vbot channel update tg-main --allow 123456789
```

4. Have the user message the bot again to confirm it now responds.

Notes:

- Direct chats have positive ids; groups have negative ids (e.g. `-100123456789`).
- The denied-chat list lives in adapter memory: it is cleared on channel restart (including the restart a `channel update` triggers) and capped at the most recent 20 chats.
- When a Telegram group is upgraded to a supergroup, its chat id changes; vBot migrates the allowlist entry automatically.

## 5. Groups and privacy mode

BotFather bots have **privacy mode on** by default: in groups the bot only receives @mentions, replies to its own messages, and `/commands`. That matches vBot's default group gating (`response_mode: "mention"`), so for a normal "answer when addressed" group bot no BotFather change is needed.

Disable privacy mode only when the bot must see every group message — required for `observe_unaddressed: true` (passive context capture) and for wake-word `mention_patterns` to match plain messages:

1. Send `/setprivacy` to BotFather, select the bot, choose `Disable`.
2. Remove the bot from the group and re-add it (Telegram applies the change only on re-join).

Group gating fields (`response_mode`, `mention_patterns`, `owner_user_ids`, `observe_unaddressed`) are not exposed as CLI flags; configure them through the WebUI channel editor or the `channel.update` RPC.

## Troubleshooting

- **Bot does not react at all in a direct chat** → chat not on the allowlist. Check `vbot channel status` for the denied entry and allow it.
- **Channel `failed=yes`** → read the `failure_reason`; token env var missing, `.env` not reloaded (restart), or the token is invalid/revoked.
- **Bot ignores plain group messages** → expected in `mention` mode; address it with @username or a reply. If a wake word or passive observation is configured and still nothing arrives, privacy mode is still on (step 5).
- **Group commands ignored** → group slash commands require the sender to be in `owner_user_ids`; an empty list means nobody may use them.
