# Telegram Channels

Telegram adapter for vBot channels. Owns Telegram long polling, Telegram chat routing, Telegram-specific command handling, media ingestion, and Telegram outbound send behavior.

## Overview

`core/channels/telegram.py` implements the first concrete `ChannelAdapter`. It uses `python-telegram-bot` async long polling, receives Telegram text/photos/documents, maps each allowed Telegram chat into a normal Agent Session, and relays only final assistant text back to Telegram. It stores inbound files through the runtime `AttachmentStore` before triggering Runs with canonical chat content blocks. It does not own channel config storage, RPC methods, or the `channel_send` tool registration lifecycle; those stay in the parent channels domain.

## Data Model

- Telegram channels use the generic `ChannelConfig` with `platform: "telegram"`. `token_env_var` names the Telegram bot token credential; `allowed_chat_ids` is the inbound allowlist; `dm_scope` controls direct-message session derivation.
- `platform_target` is a Telegram chat id rendered as a string. Adapter sends parse it back to an integer and raise `ChannelConfigError` when it is not an integer chat id.
- Telegram group chats are identified by negative chat ids and always route to `ch-<channel-id>-<chat-id>`, ignoring `dm_scope`.
- Telegram direct-message session IDs depend on `dm_scope`: `per_conversation` -> `ch-<channel-id>-<chat-id>`, `main` -> `ch-<channel-id>-main`, `per_peer` -> `ch-<channel-id>-u<user-id>`, and `per_account_channel_peer` -> `ch-<channel-id>-<chat-id>-u<user-id>`.
- New channel Sessions receive one system-reminder note telling the model the session is receiving Telegram messages. Existing channel Sessions reuse the same note and only update metadata.

## Interfaces

- `TelegramChannelAdapter(config, trigger_service, chat_sessions, runtime, attachment_store=None, command_dispatcher=...)`
- `start()` builds a Telegram application, registers text and photo/document handlers, deletes the webhook with `drop_pending_updates=False`, starts polling, and waits until stopped.
- `stop()` sets the stop event, cancels per-chat workers and album flush tasks, then stops the updater/application and shuts it down.
- `send(message, platform_target, files=None)` sends text, files, or both to one Telegram chat id. Text is split with `split_telegram_message(message, TELEGRAM_MESSAGE_LIMIT)`.
- `ensure_outbound_session(platform_target)` resolves the Session for a proactive send target and returns its `RouteFacts`, creating the Session (with the one-time channel reminder note) when it does not exist. It derives the session id from the chat id alone: Telegram private chats use `chat_id == user_id`, and group chats (negative ids) ignore `dm_scope`, so no separately-supplied user id is needed. Inbound routing and outbound resolution share the same `_ensure_channel_session` helper.
- Inbound text uses `TriggerService.trigger_run(agent_id, text, session_id)` after command dispatch. Inbound media uses `TriggerService.trigger_run(agent_id, list[ContentBlock], session_id)`.

## Conventions

- Telegram tokens resolve through `runtime.resolve_environment_credential()` when available, which currently prefers process environment over the data-dir `.env` fallback. Without that runtime hook, the adapter reads `os.environ` directly.
- Empty `allowed_chat_ids` denies all inbound Telegram chats.
- Pure text messages are command-dispatched before entering the per-chat queue. Recognized commands reply immediately and do not trigger a Run; unknown slash text follows the normal inbound chat path.
- Command actions are channel-safe: `/compact` calls `TriggerService.compact_session()` and replies with its result, `/retry` calls `TriggerService.retry_run()` and relays that Run's final reply, and `/new` replies that starting a new Session is unavailable from Telegram channels until routing has persisted rotation state. Any other recognized command action (e.g. `/handoff`) replies that the command is not available from Telegram channels — recognized commands never fall through silently.
- Per-chat queues serialize normal inbound messages for a Telegram chat. Eager text command dispatch still lets recognized commands such as cancellation be handled while a previous queued message is waiting on a Run.
- Non-text content is never slash-command-dispatched, even if a `TextBlock` contains slash-looking text.
- Run relay keeps the latest assistant-output event text and sends it only after `run_completed`. Failed runs and trigger/command exceptions send generic user-facing failure text; cancelled runs send a generic cancellation reply; completed runs with no assistant text send a generic empty-output reply.
- While a Run is relayed (and while a `/compact` action runs) the adapter shows Telegram's `typing` chat action, refreshed every `_TYPING_REFRESH_SECONDS` (4 s, since Telegram expires the action after ~5 s) by a background task scoped to an async context manager. The indicator is best-effort and cosmetic: `send_chat_action` failures stop it quietly (debug log) without affecting the reply, and it is always cancelled when the relay/compact block exits.

## External Dependencies

- `python-telegram-bot` - async Telegram Bot API client used for long polling, inbound handlers, outbound messages, media groups, and file download.

## Constraints & Gotchas

- Telegram message text is capped at `TELEGRAM_MESSAGE_LIMIT` (`4096`) characters per Bot API message; long outbound text is split into fixed-size chunks.
- Outbound image files use `send_photo`; non-image files use `send_document`. Multiple files are partitioned into image and document batches, sent in groups of at most 10 homogeneous media items, and the caption is attached only to the first item of the first batch.
- Inbound captions become a leading `TextBlock`. Photos store the largest photo variant and become `MediaBlock`; image documents become `MediaBlock`; text documents become immediate `TextBlock` values using stored `text_content`; other documents become `FileBlock`.
- Inbound media requires the runtime-owned `AttachmentStore`. Without it, media processing logs a warning and does not trigger a Run for that payload.
- Telegram albums are grouped by `media_group_id` for 500 ms before triggering one Run with the accumulated blocks.
- User-facing Telegram failure replies must not leak internal exception text. Trigger, compact, retry, media-ingest, and lifecycle failures are logged with channel/session/target/action context and traceback detail for operators.
- Lifecycle stop tolerates Telegram `RuntimeError` from already-stopped components; other lifecycle failures are warnings, not hard shutdown failures.
