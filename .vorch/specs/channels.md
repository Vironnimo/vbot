# Channels

Bidirectional messaging-platform integrations for vBot. Owns channel configuration, adapter lifecycle, outbound delivery, and platform-specific inbound routing into normal agent Sessions.

## Overview

`core/channels/` bridges external messaging platforms such as Telegram into the existing agentic loop. A Channel is an accessor-side transport abstraction, not a model provider: it receives inbound platform messages, resolves them into an Agent and Session, triggers a normal Run, and routes the final assistant text back to the platform. `ChannelService` owns persisted channel configs under `<data_dir>/channels/`, starts and stops one adapter task per enabled channel, and exposes proactive outbound send support for the `channel_send` tool. Channels reuse normal chat Sessions and the shared `TriggerService`; they do not own a separate conversation store.

## Data Model

- `ChannelConfig`
  - `id: str`
  - `platform: str` (`telegram` first)
  - `agent_id: str`
  - `dm_scope: str` (`per_conversation` | `main` | `per_peer` | `per_account_channel_peer`)
  - `allowed_chat_ids: list[int]`
  - `token_env_var: str`
  - `enabled: bool`
- Channel config files live at `<data_dir>/channels/<channel-id>/channel.json`.
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

## Interfaces

- `ChannelService(trigger_service, chat_sessions, runtime)`
  - `start() -> None` — loads enabled channels and starts adapter tasks
  - `stop() -> None` — stops all running adapters
  - `start_channel(channel_id: str) -> None`
  - `stop_channel(channel_id: str) -> None`
  - `send(channel_id: str, message: str, platform_target: str) -> None`
  - `list_channels() -> list[ChannelConfig]`
  - `create_channel(config: ChannelConfig) -> None`
  - `update_channel(channel_id: str, **fields) -> None`
  - `delete_channel(channel_id: str) -> None`
  - `enable_channel(channel_id: str) -> None`
  - `disable_channel(channel_id: str) -> None`
  - `has_active_channels() -> bool`
- `ChannelAdapter`
  - `platform: str`
  - `async start() -> None`
  - `async stop() -> None`
  - platform-specific send/dispatch helpers used by `ChannelService`
- Telegram adapter responsibilities:
  - long polling only in the first implementation
  - per-chat sequencing / batching in the adapter, not in `TriggerService`
  - `run.subscribe()` reply delivery: only the final assistant text is forwarded
  - meaningful error reply on run failure or cancellation
- Session ID derivation for DMs depends on `dm_scope`; groups always isolate by platform conversation ID.
- `channel_send` is registered via `register_channel_send_tool(registry, channel_service, chat_sessions)` and resolves `last_reply_target` from session metadata when `platform_target` is omitted.

## Conventions

- Closed architectural decisions D1-D8 from `stuff/channels.md` are binding for this domain.
- Final assistant replies for inbound platform turns are automatic. Agents do not call `channel_send` for normal replies.
- `channel_send` is for proactive outbound only.
- Missing or empty `allowed_chat_ids` means deny-all for DM-capable channels.
- Session history remains the single source of truth. Channels add metadata and System Reminder notes; they do not fork chat history.
- Runtime registers `channel_send` only when at least one channel is active and re-evaluates registration when channels are enabled or disabled.

## External Dependencies

- `python-telegram-bot` — async Telegram Bot API client used for long polling and outbound sends in the first platform adapter.

## Constraints & Gotchas

- Only the final assistant text from a completed Run is forwarded to the platform. Tool results, reasoning, and intermediate events stay in the JSONL/SSE flow.
- Sidecar metadata is owned by `ChatSessionManager`; channel code consumes it but does not define a separate storage path or format.
- Adapter restart on failure should use bounded retry with backoff; a broken adapter must not silently disappear.
- WebUI session browsing and retroactive linking are required for meaningful docking but may land after the kernel/server/CLI slice if phased separately.