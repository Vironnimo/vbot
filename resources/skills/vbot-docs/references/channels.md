# Messaging Channels

A Channel connects an Agent to Telegram, Discord, Slack, Mattermost, WhatsApp or an email account. For a first Telegram setup, follow "Set up Telegram" below.

```bash
vbot channel add <channel-id> --platform telegram|discord|slack|mattermost|whatsapp|email --agent <agent-id> (--token-stdin | --token-env <ENV_VAR>) [--dm-scope <scope>] [--allow <chat-id>...] [--disabled] [group flags]
vbot channel list
vbot channel status <channel-id>
vbot channel update <channel-id> [--platform ...] [--agent ...] [--token-env ...] [--dm-scope ...] [--allow <chat-id>...] [--enabled true|false] [group flags]
vbot channel token set <channel-id> --stdin [--slot bot|app]
vbot channel identity <channel-id> [--user <platform-user-id>]
vbot channel access <channel-id> --group <group-id>
vbot channel admin grant <channel-id> --group <group-id> --user <platform-user-id>
vbot channel admin revoke <channel-id> --group <group-id> --user <platform-user-id>
vbot channel enable <channel-id>
vbot channel disable <channel-id>
vbot channel remove <channel-id>
```

Read `channel list` for the saved configuration and `channel status <channel-id>` for the connection before you change a Channel, and use its exact id.

## Tokens

- Create a Channel with `--token-stdin` and pipe the token in: vBot saves it in the Secret Store and starts the Channel without a server restart. `channel token set <channel-id> --stdin` replaces it later and restarts only that Channel when the token changed. Never pass the token as an argument.
- `--token-env <ENV_VAR>` instead reads the token from a variable that already exists in the server's process environment. Such a variable wins over the stored token: `token set` then reports `applied=no` and restarts nothing.
- `add`, `update`, `enable`, `disable` and `token set` print the saved state. The platform can reject a token after the command returned, so check `channel status`: `failed=yes` with a token failure means the token is missing, malformed or rejected; replace it with `token set`.

## Allowed chats

- An empty `--allow`, also an omitted one on `add`, denies every inbound chat. On `update`, `--allow` replaces the whole list: pass every id that stays allowed. All other `update` flags change only their own field.
- To find a chat's id, have the user send the bot a message, then read the denied chats (id, kind, name, last seen) from `channel status` and allow the id with `update --allow`. The denied list holds the last 20 chats and is cleared when the Channel restarts, which every `channel update` does.
- Telegram entries are chat ids: direct chats positive, groups negative (`-100123456789`). Discord entries are channel or thread ids, not server ids, and the bot needs the Message Content Intent in the Discord Developer Portal. Slack and Mattermost entries are conversation ids, not user, team or workspace ids. Email entries are sender addresses. Keep ids as strings.
- `--dm-scope` decides which direct messages share a Session: `per_conversation` (default), `main`, `per_peer` or `per_account_channel_peer`.

## Groups

- `--response-mode mention` answers group messages addressed to the bot, `all` every allowed message. `--mention-pattern <pattern>...` replaces the list of extra wake words; the flag without values clears it. `--observe-unaddressed true` lets the Agent read group messages it does not answer.
- Group members are `admin` or `member`. Members use the Channel’s member Tool selection unless their group overrides it. Every selection is limited by the Agent’s own Tool access. Configure these selections under Settings > Integrations > Channels > Group access: None, Selected Tools or All Agent Tools; the initial default is `web_search` and `web_fetch`. Removing a group override restores the Channel default.
- Admins keep the Agent's full Tool access; group Commands and Run buttons require an admin. Added access applies to new messages; removed access applies before the next Tool call of queued or active Runs.
- `channel identity <channel-id> --user <user-id>` names the user's own account on the platform, from a participant vBot has seen; it is an admin in every group and cannot be demoted. `channel access` lists one group's participants and roles. `admin grant` and `admin revoke` change one user without restarting the Channel.

```bash
vbot channel add dc-main --platform discord --agent assistant --token-env DISCORD_BOT_TOKEN --allow 123456789012345678
vbot channel update tg-main --allow 12345 67890
vbot channel admin grant tg-main --group -100123456789 --user 51
```

## Set up Telegram

The user does the steps in Telegram; you run the vBot commands and guide them. Each Agent the user wants to reach through Telegram needs its own Channel with its own bot: a bot token serves one Channel, because Telegram delivers a bot's messages to one receiver only.

1. The user opens a chat with `@BotFather`, sends `/newbot`, chooses a display name and a username ending in `bot`, and receives the bot token (`123456789:AA...`). Done when the user has the token.
2. Create the Channel with the token on stdin: `vbot channel add tg-main --platform telegram --agent <agent-id> --token-stdin`. When the user has not given you the token, give them that command to run in their terminal. Done when `vbot channel status tg-main` reports `running=yes`.
3. Find and allow the chat: the user opens the bot and presses Start or sends a message; in a group, they add the bot and send a command addressed to it, such as `/start@<bot_username>`. Read the denied chat's id from `channel status` and allow it with `vbot channel update tg-main --allow <chat-id>`, listing every id that stays allowed. Done when the user's next message gets an answer.
4. For a group where the bot reacts to its name, wake words or `--observe-unaddressed true`, the bot must receive plain group messages: the user sends `/setprivacy` to BotFather, chooses the bot and `Disable`, then removes the bot from the group and adds it again. Without that, Telegram delivers only commands addressed to the bot and replies to it. A bot that is a group administrator receives all messages anyway.
5. Once the relevant people have written in the group, set the user's own account with `vbot channel identity tg-main --user <telegram-user-id>` and check the roles with `vbot channel access tg-main --group <group-id>`.

When a group becomes a supergroup, its chat id changes; vBot moves the allowlist entry itself.

- No reply in a direct chat: check `channel status` for a failure or a denied chat; when neither explains it, check the Agent's effective Model and the logs.
- The bot ignores its name or plain group messages: privacy mode is still on, or the text matched no wake word. Test with a command addressed to the bot.
- Group commands are ignored: they need the sender's `admin` role; check `channel identity` and `channel access`.

## WhatsApp: the user's account, self chat only

WhatsApp links vBot as a device of the user's existing account and needs Node.js 22 or newer and npm on the vBot server. Create it without a token and with `--allow self`; it is saved disabled. Only the user's own chat with themself, `self`, can start Runs and receive replies. WhatsApp can restrict such unofficial connections; do not call it a Business API bot or promise that an account qualifies.

```bash
vbot channel add wa-main --platform whatsapp --agent assistant --allow self
vbot channel whatsapp setup wa-main
vbot channel whatsapp status wa-main
vbot channel whatsapp pair wa-main
```

Setup runs in the background: repeat `whatsapp status` until it reports `installed: True`, and on a failure follow its error before retrying. Then pair: the user opens Settings > Channels in vBot and scans the QR code in WhatsApp under Settings > Linked devices > Link a device. Never copy the QR code or device credentials into the chat or logs. `state: connected` confirms the link; only a message in the self chat and its answer prove delivery. After `logged_out`, `whatsapp pair <channel-id> --reset` requests a new QR code. Pairing again does not remove the old device in WhatsApp; the user removes it there.

## Slack and Mattermost

Slack needs a bot token and a separate Socket Mode app token. Without saved tokens, create the Channel disabled, save each token from stdin, then enable it:

```bash
vbot channel add slack-main --platform slack --agent assistant --token-env SLACK_BOT_TOKEN --app-token-env SLACK_APP_TOKEN --disabled
vbot channel token set slack-main --stdin
vbot channel token set slack-main --slot app --stdin
vbot channel enable slack-main
```

The Slack app needs Socket Mode with an app-level token with `connections:write`; the bot scopes `chat:write`, `files:read`, `files:write` and the `read` and `history` scopes for `channels`, `groups`, `im` and `mpim`; the events `message.channels`, `message.groups`, `message.im` and `message.mpim`; and the App Home Messages tab. An `app_mention` subscription alone is not enough. The user reinstalls the app after a scope change and invites the bot to each channel.

Mattermost needs `--server-url <https://server[/subpath]>`, without `/api/v4`, credentials, query or fragment, and a bot token through `--token-stdin` or `--token-env`. When the bot cannot connect or sees no messages, the server administrator enables bot accounts and adds the bot to the teams and channels.

For both, `channel status` also reports `connected`; `running=yes` alone means only that the listener started. In groups set to `mention`, also in threads, the bot answers only when mentioned or woken by a pattern. A thread shares its parent conversation's Session. They support text and files, but no buttons, typing indicators, missed-message history or quoted-message content. Disable a running Channel and wait until it stopped before you remove it.

## Email

An email Channel reads one mail account over IMAP and sends over SMTP; the password is its token. Most providers need an app password, which the user creates in the account's security settings: Gmail requires 2-step verification for it; GMX and web.de also need IMAP access switched on in the mailbox settings. Outlook.com and Microsoft 365 accept only OAuth2 sign-in, which vBot does not support.

```bash
vbot channel add mail-main --platform email --agent assistant --email-address me@gmx.de --imap-host imap.gmx.net --smtp-host mail.gmx.net --smtp-port 587 --allow user@example.com --token-stdin
```

Gmail uses `imap.gmail.com` and `smtp.gmail.com` (port 465), web.de `imap.web.de` and `smtp.web.de` (port 587). SMTP security defaults to SSL on port 465 and STARTTLS otherwise.

Each allowed address gets its own conversation. vBot answers a mail only when the receiving server verified the sender's domain (DKIM or DMARC); an allowed sender whose mail was not verified appears among the denied chats in `channel status`. `--require-sender-authentication false` turns this check off for a server that does not verify senders. Mails from other senders, automatic replies and mailing lists start no conversation; Cron jobs with an email trigger can act on them. vBot never marks, moves or deletes mails and starts with the mails that arrive after the Channel first connects. `--email-folder INBOX <folder>...` watches further folders. Replies thread under the mail they answer, and the Agent can write to any address. On `update`, the email flags change only the account fields they name.
