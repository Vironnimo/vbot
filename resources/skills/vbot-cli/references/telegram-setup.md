# Telegram Bot Setup

End-to-end walkthrough for connecting a Telegram bot to vBot: create the bot, store the token, create the channel, discover chat ids, and configure group behavior. Use this when the user asks to "set up Telegram" and no bot or channel exists yet. The user performs the Telegram-side steps (BotFather, sending messages); you run the vBot-side commands and guide them through the rest.

## 1. Create the bot with BotFather

The user creates the bot in their Telegram app — walk them through it:

1. Open a chat with `@BotFather` in Telegram.
2. Send `/newbot`.
3. Choose a display name (free text) and then a username (must be unique and end in `bot`, e.g. `julian_assistant_bot`).
4. BotFather replies with the **bot token** (format `123456789:AA...`). Treat it as a secret.

## 2. Create the Channel with a managed token

Pass the token to the CLI over UTF-8 stdin, never as a command-line argument:

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-stdin
```

Supply the token through the caller's stdin mechanism. The server stores it atomically under a Channel-specific managed key in the data-dir `.env`, reloads Credentials live, and starts only this Channel adapter. Never echo the token, include it in a shell argument, or edit `.env` directly.

The command result shows the saved Channel, credential source, and whether the managed value is effective. No server restart is needed. If an external process-environment value already owns the derived key, it remains authoritative and the result reports `effective_source=process_environment applied=no`.

For an externally managed deployment secret, use `--token-env <ENV_VAR>` instead of `--token-stdin`; the variable must already be present in the server process environment.

## 3. Create and verify the channel

The Channel was created in step 2. Its allowlist starts empty — an empty allowlist denies all inbound chats, which is safe and is exactly what the discovery flow in step 4 expects:

```bash
vbot channel status tg-main
```

`status` should report `running=yes`. `failed=yes` with a token-related failure reason means the token is missing, malformed, or rejected by Telegram. Rotate it without a server restart:

```bash
vbot channel set-token tg-main --stdin
vbot channel status tg-main
```

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

BotFather bots have **privacy mode on** by default: in groups the bot only receives @mentions, replies to its own messages, and `/commands`. Those addressing forms work with vBot's default group gating (`response_mode: "mention"`), but the visible Telegram name is a plain message and requires privacy mode to be disabled.

Disable privacy mode when the bot must see plain group messages — required for visible-name addressing, `observe_unaddressed: true` (passive context capture), and wake-word `mention_patterns`:

1. Send `/setprivacy` to BotFather, select the bot, choose `Disable`.
2. Remove the bot from the group and re-add it (Telegram applies the change only on re-join).

Group gating is available through `channel add`/`channel update`: `--response-mode mention|all`, list-replacing `--mention-pattern`, and `--observe-unaddressed true|false`.

After each relevant person has sent at least one message in the allowed group, establish the Channel account's own identity and inspect the saved roles:

```bash
vbot channel identity tg-main --user <your-telegram-user-id>
vbot channel access tg-main --group <telegram-group-id>
```

The own identity is an admin in every group and cannot be demoted. Add or remove other admins without replacing the group list:

```bash
vbot channel grant-admin tg-main --group <telegram-group-id> --user <user-id>
vbot channel revoke-admin tg-main --group <telegram-group-id> --user <user-id>
```

Admins retain the Agent's existing Tool access. Members may authorize only `web_search` and `web_fetch`; group Commands and reserved Run buttons require admin.

## Troubleshooting

- **Bot does not react at all in a direct chat** → chat not on the allowlist. Check `vbot channel status` for the denied entry and allow it.
- **Channel `failed=yes`** → read the `failure_reason`; use `channel set-token <id> --stdin` when the token is missing, invalid, or revoked, then check `channel status` again.
- **Bot ignores its visible name or other plain group messages** → privacy mode is still on (step 5), or the text did not match a configured wake word. @username and replies remain available with privacy mode on.
- **Group commands ignored** → group slash commands require the sender's `admin` role. Confirm own identity and group roles with `channel identity` and `channel access`, then use additive `grant-admin` if needed.
