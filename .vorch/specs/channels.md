# Channels

Bidirectional messaging-platform integrations for vBot. Owns channel configuration, adapter lifecycle, outbound delivery, and platform-specific inbound routing into normal agent Sessions.

## Overview

`core/channels/` bridges external messaging platforms such as Telegram into the existing agentic loop. A Channel is an accessor-side transport abstraction, not a model provider: it receives inbound platform messages, resolves them into an Agent and Session, intercepts recognized built-in slash commands on pure-text messages, otherwise triggers a normal Run, and routes the final assistant text back to the platform. `ChannelService` owns persisted channel configs under `<data_dir>/channels/`, starts and stops one adapter task per enabled channel, and exposes proactive outbound send support for the `channel_send` tool. Channels reuse normal chat Sessions, the shared `TriggerService`, and the shared `CommandDispatcher`; they do not own a separate conversation store. Telegram now also routes inbound photos/documents into canonical attachment-aware chat content.

## Data Model

- `ChannelConfig`
  - `id: str`
  - `platform: str` (`telegram` first)
  - `agent_id: str` — must resolve to an existing Agent
  - `dm_scope: str` (`per_conversation` | `main` | `per_peer` | `per_account_channel_peer`)
  - `allowed_chat_ids: list[int]`
  - `token_env_var: str`
  - `enabled: bool`
- Channel config files live at `<data_dir>/channels/<channel-id>/channel.json`.
- Channel config reads pass `channel.json` through `core/settings/validation.py` before constructing `ChannelConfig`. Invalid JSON shape or schema errors raise `ChannelConfigError` with file/path diagnostics.
- Channel session metadata lives in `<data_dir>/agents/<agent-id>/sessions/<session-id>.meta.json` beside the JSONL transcript. Channel-owned fields are:
  - `source_channel_id`
  - `platform`
  - `platform_conv_id`
  - `last_reply_target: { channel_id, platform_target }`
- Fact types used by adapters:
  - `ConversationFacts` — origin platform/channel/chat/user/thread identifiers
  - `RouteFacts` — resolved `agent_id` and `session_id`
  - `ReplyPlanFacts` — `channel_id` and reply target for outbound delivery
  - `MessageFacts` — model-visible inbound text
- `FileData` — outbound file payload with `filename`, `media_type`, and raw `data` bytes.

## Interfaces

- `ChannelService(trigger_service, chat_sessions, runtime)`
  - `start() -> None` — loads enabled channels and starts adapter tasks
  - `stop() -> None` — stops all running adapters
  - `start_channel(channel_id: str) -> None`
  - `stop_channel(channel_id: str) -> None`
  - `send(channel_id: str, message: str | None, platform_target: str, *, files: list[FileData] | None = None) -> None`
  - `list_channels() -> list[ChannelConfig]`
  - `create_channel(config: ChannelConfig) -> None`
  - `update_channel(channel_id: str, **fields) -> None`
  - `delete_channel(channel_id: str) -> None`
  - `enable_channel(channel_id: str) -> None`
  - `disable_channel(channel_id: str) -> None`
  - `has_active_channels() -> bool`
  - `is_failed(channel_id: str) -> bool`
  - `failure_reason(channel_id: str) -> str | None`
- `ChannelAdapter`
  - `platform: str`
  - `async start() -> None`
  - `async stop() -> None`
  - `async send(message: str | None, platform_target: str, *, files: list[FileData] | None = None) -> None`
  - platform-specific send/dispatch helpers used by `ChannelService`
- Telegram adapter responsibilities:
  - long polling only in the first implementation
  - per-chat sequencing / batching in the adapter, not in `TriggerService`
  - pure-text built-in slash commands are dispatched before `trigger_run`; handled commands reply immediately and command action results are executed through channel-safe behavior
  - `/compact` uses the shared manual compaction action and replies with the compaction result
  - `/retry` retries the latest user turn through `TriggerService.retry_run()` and relays the resulting Run's final assistant text
  - `/new` currently replies that starting a new Session is unavailable from Telegram channels because deterministic channel Session routing has no persisted rotation mapping yet
  - `run.subscribe()` reply delivery: only the final assistant text is forwarded
  - meaningful error reply on run failure or cancellation
  - outbound file sends decide between `send_photo`, `send_document`, and media groups inside the adapter
  - inbound photos/documents store blobs through the runtime-owned `AttachmentStore` and materialize `MediaBlock`, `TextBlock`, or `FileBlock`
  - album buffering groups messages by `media_group_id` for 500 ms before triggering one Run
- Session ID derivation for DMs depends on `dm_scope`; groups always isolate by platform conversation ID.
- `channel_send` is registered via `register_channel_send_tool(registry, channel_service, chat_sessions)` and resolves `last_reply_target` from session metadata when `platform_target` is omitted.

## Conventions

- Closed architectural decisions D1-D8 from `stuff/channels.md` are binding for this domain.
- Final assistant replies for inbound platform turns are automatic. Agents do not call `channel_send` for normal replies.
- Recognized built-in slash commands are handled before normal `TriggerService` queueing, but unknown slash text still follows the normal inbound chat path.
- `channel_send` is for proactive outbound only.
- `channel_send` may send text only, files only, or both; at least one payload is required.
- Missing or empty `allowed_chat_ids` means deny-all for DM-capable channels.
- Session history remains the single source of truth. Channels add metadata and System Reminder notes; they do not fork chat history.
- Runtime registers `channel_send` only when at least one channel is active and re-evaluates registration when channels are enabled or disabled.
- `channel_send` reports success only after the adapter send call completes; delivery is not fire-and-forget.
- Runtime startup degrades per channel: an enabled channel with invalid runtime dependencies, such as an unknown `agent_id` or missing Telegram token, is marked failed with a diagnostic reason and does not prevent the server from starting. Public create/update/enable paths still reject unknown Agent IDs before persisting or enabling a channel.

## External Dependencies

- `python-telegram-bot` — async Telegram Bot API client used for long polling and outbound sends in the first platform adapter.
- Telegram bot tokens resolve through the runtime credential environment snapshot first, with process-environment fallback. Standard data-dir `.env` setups must work without mutating `os.environ`.

## Constraints & Gotchas

- Only the final assistant text from a completed Run is forwarded to the platform. Tool results, reasoning, and intermediate events stay in the JSONL/SSE flow.
- Sidecar metadata is owned by `ChatSessionManager`; channel code consumes it but does not define a separate storage path or format.
- Adapter restart on failure should use bounded retry with backoff; a broken adapter must not silently disappear.
- Failed channel diagnostics are runtime-local health state. Persisted `channel.json` remains the source of truth for configuration, and vBot does not automatically retarget a channel to a fallback Agent.
- Telegram caught failure paths that must preserve user-facing generic replies still log channel/session/action context with traceback detail so operators can diagnose trigger, compact, retry, media ingest, and lifecycle failures.
- WebUI session browsing and retroactive channel linking are implemented through the session drawer and `session.link_channel`; keep the sidecar metadata contract stable for those accessors.
- Text documents received from Telegram are embedded immediately as `TextBlock`s using stored `text_content`; only non-text documents become `FileBlock` references.
