# Discord Channels

Discord platform-I/O adapter behind the shared channel conversation engine.

## Overview

`core/channels/discord.py` uses `discord.py`'s async Gateway client. It owns Gateway lifecycle, Discord message parsing, bounded history backfill, attachment download, typing, replies, and outbound sends. Queueing, response gating, commands, Run relay, sender attribution, observed-note formatting, and Session metadata remain in `ChannelConversationEngine`.

## Routing And Gating

- Discord configs use `platform: "discord"`. `token_env_var` resolves the bot token through the injected credential resolver; `allowed_chat_ids` contains Discord channel ids as normalized strings.
- DMs are `direct`; guild channels and threads are `group`. A thread's own id is its `chat_id`, so each thread gets its own `ch-<channel-id>-<thread-id>` Session. An allowlisted parent channel also permits its threads; an explicitly allowlisted thread works independently.
- Bot-authored inbound messages are ignored to prevent loops. Sender display name uses Discord `display_name`, then `global_name`, then account `name`.
- Native user mentions and replies whose resolved referenced message belongs to this bot populate the engine's `mentioned_bot` / `is_reply_to_bot` facts. Built-in commands remain ordinary text commands handled by the engine, not Discord application-command interactions.
- Inbound attachments are downloaded inside the engine worker and stored through `AttachmentStore`. Message content becomes a leading `TextBlock`; image/audio/video files become `MediaBlock`, text files become extracted `TextBlock`, and other files become `FileBlock`.

## History Backfill

- In group `response_mode: "mention"` with `observe_unaddressed: false`, an addressed non-command message fetches up to 50 messages immediately before the trigger, newest-first. Collection stops at the bot's latest message; retained entries are reversed to chronological order and enqueued through `observe_inbound_text` before the triggering turn.
- Backfilled attachments are context placeholders (`[media] <filename>`); they are not downloaded. The engine persists all backfill through the normal `[channel-message] <display_name> (<id>): ...` note format.
- Process-local seen ids suppress duplicate backfill while multiple triggers are pending; the set is cleared after a successful outbound bot send. Backfill failure is logged as a warning and does not block the triggering message.
- `observe_unaddressed: true` switches to the engine's live passive-observation path and disables history backfill to avoid duplicate context.

## Outbound

- Discord message content is split at 2000 characters. Files are sent in batches of at most 10; text chunks and file batches share sends by index, so the caption appears only on the first send.
- Group replies reference only the first outbound chunk using a partial-message reference with `fail_if_not_exists=False` and do not mention the replied-to author. Proactive `channel_send` output has no reply reference.
- `activity_indicator` uses `channel.typing()` for the Run/compaction scope. Indicator failures are cosmetic and do not fail the Run.
- Target ids resolve from the client cache first, then `fetch_channel()`. `ensure_outbound_session()` is synchronous and therefore uses a target already seen, cached, or resolved by the preceding send.

## External Dependency

- `discord.py>=2.7,<3` is part of the `server` and `dev` optional dependency groups.
- The privileged **Message Content Intent** must be enabled in the Discord Developer Portal and is enabled on the client intents in code. The bot also needs channel access for receiving/sending; history backfill needs **Read Message History**, and outbound files need **Attach Files**.

## Constraints & Gotchas

- `allowed_chat_ids` are channel/thread ids, not guild ids.
- Discord reply detection depends on the Gateway payload resolving or caching the referenced message. An unresolved/deleted reference does not count as `is_reply_to_bot`.
- History backfill is bounded context, not a parallel transcript. Session JSONL remains canonical, and only observed notes selected at trigger time are persisted.
