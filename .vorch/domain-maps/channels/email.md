# Email Channel

The `email` platform: one mail account read over IMAP and written over SMTP (`core/channels/email.py`), with mail parsing and building in `_email_messages.py`, blocking IMAP/SMTP connections in `_email_transport.py` and its `channels.db` tables in `_email_state.py` (`ChannelStateStore.email`). Standard library only (`imaplib`, `smtplib`, `email` with `policy.default`); HTML bodies become Markdown through `markdownify`.

## Configuration

- `ChannelConfig.email` (`EmailSettings`, `None` on every other platform) holds `address`, `username` (default: the address), `imap_host`, `imap_port` (993), `smtp_host`, `smtp_port` (465), `smtp_security` (`ssl` | `starttls`; default `ssl` on 465, else `starttls`), `folders` (default `["INBOX"]`) and `require_sender_authentication` (default true). The password is the Channel credential behind `token_env_var`; app tokens and `server_url` are refused. No OAuth2: Outlook.com and Microsoft 365 cannot be used.
- `allowed_chat_ids` holds sender addresses, validated as addresses and lowercased on validation.
- `ChannelService.update_channel(email={...})` merges into the stored settings (`EmailSettings.updated`): only named fields change, and a username equal to the address or a security equal to the port default follows a changed address or port. Leaving the platform drops the account. RPC `channel.create`/`channel.update` pass `email` through; the CLI has `--email-address`, `--imap-host`, ... flags; the WebUI edits it as flat form fields and sends the whole object.

## Inbound

- Connection: `IMAP4_SSL` with `shared_ssl_context()`, 30 s timeout. A refused login is `ChannelConfigError`; network failures are retryable `ChannelError`s for the supervisor. `_report_connected()` runs after the first pass over every folder, which also proves the folders exist. Folder names go out in modified UTF-7.
- Waiting: IDLE (Python 3.14 `IMAP4.idle`) on the first folder for 9 minutes when it is the only folder, else 60 s; without IDLE support a 60 s poll. All blocking IMAP/SMTP work runs on the `channel-email-io` worker pool. A cancelled wait calls `MailboxConnection.interrupt()` (socket shutdown), because the supervisor cancels `start()` before `stop()` and the pool defers cancellation until its worker returns.
- The Channel never changes the mailbox: folders are `SELECT`ed read-only and mails fetched with `BODY.PEEK[]` (only `BODY.PEEK[HEADER]` above 50 MB). Progress is a per-folder watermark in `channel_email_mailboxes(channel_id, folder, account, uid_validity, last_uid)`; `account` is `<imap_host>/<username>`. No watermark, a changed account or a changed UIDVALIDITY starts at UIDNEXT without a backlog (the change logs one WARNING). The watermark advances after each handled mail; a mail the sink did not take stops the pass, and the next pass retries it after 30, 60, 120, 300, then 600 s. A conversation mail the Queue rejected counts as handled: the engine already sent its busy reply, and a retry would mail that reply again.
- Routing per mail: a mail this Channel sent (recorded `outbound`) is skipped. It enters the conversation when the sender is allowed, authenticated (or `require_sender_authentication` is off) and not automatic; every other mail goes to the bound `InboundMailSink` (`core/channels/mail.py`, bound by `ChannelService.set_mail_sink`, Cron's email triggers) as `InboundMail`, or is passed over when no sink is bound. An allowed but unauthenticated sender is recorded in the denied-chat log; other senders are not.
- Authenticated: the topmost `Authentication-Results` header has `dmarc=pass` (for the From domain when `header.from` is given) or `dkim=pass` whose `header.d`/`header.i` domain equals the From domain or a parent of it. Automatic: `Auto-Submitted` other than `no`, `Precedence` bulk/junk/list, or a `mailer-daemon`/`postmaster` sender.
- Conversation facts: `chat_id`, `user_id` and the routing target are the lowercased sender address, kind `direct`; `message_id` and `thread_id` are the Message-ID (a missing one gets `<uidvalidity.uid.folderhash@channel.vbot.invalid>`). Text is `Subject: <subject>` and a blank line before the body, or the body alone when it starts with `/` so a Command such as `/new` runs; cut at `MAIL_TEXT_MAX_CHARS`; attachments go through the shared media path. Receipts use the Message-ID.

## Outbound

- `send` validates the target as an address (also read from `mailto:` and `Name <address>` forms), builds one `text/plain` UTF-8 mail with files as attachments and a Message-ID on the address's domain. With a `thread_id` of a recorded mail it answers it: `In-Reply-To`, `References` and `Re: <subject>` without stacking prefixes; otherwise the subject is the first text line without Markdown, at most 78 characters. Outbound is unrestricted. Buttons raise `ChannelError`; there is no activity indicator, no message deletion and no quoted content.
- Failure classes: before the message data (connect, login, `MAIL`, `RCPT`) nothing was sent - retryable on network errors and 4xx; a refused login is not retryable. A failure while handing over the data without the server's answer is `possibly_delivered`; a `DATA` refusal is retryable only on 4xx.
- `channel_email_messages(channel_id, message_id, subject, reference_ids, outbound)` keeps the newest 4096 mails per Channel for threading and own-mail recognition; recording a sent mail after delivery is best-effort.

## Constraints & Gotchas

- Both email tables cascade from the registry, so the `bind_platform` reset and Channel deletion clear them.
- Sent mails are not copied into a Sent folder.
- Live IMAP/SMTP servers were not exercised; tests use fake connections (`tests/core/channels/test_email.py`).
