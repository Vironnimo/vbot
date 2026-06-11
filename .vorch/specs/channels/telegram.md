# Telegram Channels

Telegram adapter for vBot channels. Owns Telegram long polling, Telegram chat routing, Telegram-specific command handling, media ingestion, and Telegram outbound send behavior.

## Overview

`core/channels/telegram.py` implements the first concrete `ChannelAdapter`. It uses `python-telegram-bot` async long polling, receives Telegram text, photos, documents, voice messages, audio, video, and video notes, maps each allowed Telegram chat into a normal Agent Session, and relays only final assistant text back to Telegram. It stores inbound files through the runtime `AttachmentStore` (downloaded inside the per-chat worker, not the update handler) before triggering Runs with canonical chat content blocks. It does not own channel config storage, RPC methods, or the `channel_send` tool registration lifecycle; those stay in the parent channels domain.

## Data Model

- Telegram channels use the generic `ChannelConfig` with `platform: "telegram"`. `token_env_var` names the Telegram bot token credential; `allowed_chat_ids` is the inbound allowlist; `dm_scope` controls direct-message session derivation.
- `platform_target` is a Telegram chat id rendered as a string. Adapter sends parse it back to an integer and raise `ChannelConfigError` when it is not an integer chat id.
- Telegram group chats are identified by negative chat ids and always route to `ch-<channel-id>-<chat-id>`, ignoring `dm_scope`.
- Telegram direct-message session IDs depend on `dm_scope`: `per_conversation` -> `ch-<channel-id>-<chat-id>`, `main` -> `ch-<channel-id>-main`, `per_peer` -> `ch-<channel-id>-u<user-id>`, and `per_account_channel_peer` -> `ch-<channel-id>-<chat-id>-u<user-id>`.
- New channel Sessions receive one system-reminder note telling the model the session is receiving Telegram messages. Existing channel Sessions reuse the same note and only update metadata.

## Interfaces

- `TelegramChannelAdapter(config, trigger_service, chat_sessions, credential_resolver, attachment_store=None, *, command_dispatcher)` — constructor injection only, no runtime handle; `credential_resolver` is the `Callable[[str], str]` passed down from `ChannelService`.
- `start()` builds a Telegram application, registers text, media (photo/document/voice/audio/video/video-note), and unsupported-message-type handlers, deletes the webhook with `drop_pending_updates=False`, starts polling, and waits until stopped. Handlers are restricted with `filters.UpdateType.MESSAGE` to new messages only: edited messages and channel posts are ignored (edits must not trigger new Runs; `filters.UpdateType.MESSAGES` would still match `edited_message` and is deliberately not used).
- Animation and sticker messages from allowed chats get an eager "this message type isn't supported yet" reply directly from the handler; no Run is triggered and no Session is created. Animations usually carry a backward-compat `document` field and therefore hit the media handler first (where the allowlist rejects them with the unsupported-file reply); the ANIMATION filter is a fallback in case Telegram stops setting that field.
- `stop()` sets the stop event, cancels per-chat workers and album flush tasks, then stops the updater/application and shuts it down.
- `send(message, platform_target, files=None)` sends text, files, or both to one Telegram chat id. Text is split with `split_telegram_message(message, TELEGRAM_MESSAGE_LIMIT)`.
- `ensure_outbound_session(platform_target)` resolves the Session for a proactive send target and returns its `RouteFacts`, creating the Session (with the one-time channel reminder note) when it does not exist. It derives the session id from the chat id alone: Telegram private chats use `chat_id == user_id`, and group chats (negative ids) ignore `dm_scope`, so no separately-supplied user id is needed. Inbound routing and outbound resolution share the same `_ensure_channel_session` helper.
- Inbound text uses `TriggerService.trigger_run(agent_id, text, session_id)` after command dispatch. Inbound media uses `TriggerService.trigger_run(agent_id, list[ContentBlock], session_id)`.

## Conventions

- Telegram tokens resolve through the injected `credential_resolver` (Runtime wires `resolve_environment_credential`, which prefers process environment over the data-dir `.env` fallback). The adapter never reads `os.environ` itself; a missing or empty token raises `ChannelConfigError` at construction.
- Empty `allowed_chat_ids` denies all inbound Telegram chats.
- Pure text messages are command-dispatched before entering the per-chat queue. Directly handled commands (e.g. `/stop`, `/help`) reply eagerly from the update handler and do not trigger a Run; unknown slash text follows the normal inbound chat path.
- Command actions are channel-safe: `/compact` calls `TriggerService.compact_session()` and replies with its result, `/retry` calls `TriggerService.retry_run()` and relays that Run's final reply, and `/new` replies that starting a new Session is unavailable from Telegram channels until routing has persisted rotation state. Any other recognized command action (e.g. `/handoff`) replies that the command is not available from Telegram channels — recognized commands never fall through silently.
- PTB processes this adapter's updates sequentially (`concurrent_updates` stays off; the per-chat queue owns ordering), so the update handler must never await slow work. Command actions (compact = model call, retry = full Run relay) and media downloads are enqueued into the per-chat queue and executed by its worker; only routing, eager command replies, and album buffering happen in the handler.
- Per-chat queues serialize inbound messages, command actions, and media work for a Telegram chat. Eager text command dispatch still lets directly handled commands such as `/stop` cancel a Run that a queued message — or a queued `/retry`/`/compact` action — is waiting on.
- Non-text content is never slash-command-dispatched, even if a `TextBlock` contains slash-looking text.
- Run relay keeps the latest assistant-output event text and sends it only after `run_completed`. Failed runs and trigger/command exceptions send generic user-facing failure text; cancelled runs send a generic cancellation reply; completed runs with no assistant text send a generic empty-output reply.
- While a Run is relayed (and while a `/compact` action runs) the adapter shows Telegram's `typing` chat action, refreshed every `_TYPING_REFRESH_SECONDS` (4 s, since Telegram expires the action after ~5 s) by a background task scoped to an async context manager. The indicator is best-effort and cosmetic: `send_chat_action` failures stop it quietly (debug log) without affecting the reply, and it is always cancelled when the relay/compact block exits.

## External Dependencies

- `python-telegram-bot` - async Telegram Bot API client used for long polling, inbound handlers, outbound messages, media groups, and file download.

## Constraints & Gotchas

- Telegram message text is capped at `TELEGRAM_MESSAGE_LIMIT` (`4096`) characters per Bot API message; long outbound text is split into fixed-size chunks.
- Outbound image files use `send_photo`; non-image files use `send_document`. Multiple files are partitioned into image and document batches, sent in groups of at most 10 homogeneous media items, and the caption is attached only to the first item of the first batch.
- Inbound captions become a leading `TextBlock`. Photos store the largest photo variant and become `MediaBlock`; voice/audio/video/video-note payloads become `MediaBlock` with the server-sniffed media type (filenames default to `telegram-voice-<unique>.ogg`, `telegram-audio-<unique>`, `telegram-video-<unique>.mp4`, `telegram-video-note-<unique>.mp4` when the platform provides none); image documents become `MediaBlock`; text documents become immediate `TextBlock` values using stored `text_content`; other documents become `FileBlock`. Whether audio goes native, gets transcribed, or fails is decided by the chat-layer resolver, not the adapter.
- Inbound media requires the runtime-owned `AttachmentStore`. Without it, media processing logs a warning, replies with the generic media-failure text, and does not trigger a Run for that payload.
- Inbound media is processed per message: one failing album item does not drop its siblings. Every media-ingest failure produces a user-facing reply — `AttachmentTypeNotAllowedError` maps to an unsupported-file reply, `AttachmentTooLargeError` to a too-large reply, anything else to a generic media-failure reply; duplicate reply texts within one batch are sent only once. Successfully ingested blocks still trigger their Run.
- Telegram albums are grouped by `media_group_id`; the 500 ms flush window (`_ALBUM_FLUSH_SECONDS`) restarts with each buffered item, so slow album delivery does not split one album into multiple Runs. The handler buffers raw messages only; downloads happen in the per-chat worker after the flush.
- User-facing Telegram failure replies must not leak internal exception text. Trigger, compact, retry, media-ingest, and lifecycle failures are logged with channel/session/target/action context and traceback detail for operators.
- Lifecycle stop tolerates Telegram `RuntimeError` from already-stopped components; other lifecycle failures are warnings, not hard shutdown failures.
