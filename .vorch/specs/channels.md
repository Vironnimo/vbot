# Channels

Messaging-platform accessors for vBot. Owns channel configuration, adapter lifecycle, outbound delivery, and routing platform inbound messages into normal agent Sessions.

## Overview

`core/channels/` bridges external messaging platforms into the existing agentic loop. A Channel is an accessor-side transport abstraction, not a model provider: it receives inbound platform messages, resolves them into an Agent and Session, triggers normal Runs, and routes assistant replies back through the platform adapter. `ChannelService` owns persisted channel configs under `<data_dir>/channels/`, starts and stops adapter tasks for enabled channels, and exposes proactive outbound send support for the `channel_send` tool. Channels reuse normal Sessions, `TriggerService`, `CommandDispatcher`, attachment storage, and prompt/tool registration; they do not own a separate conversation store.

## Data Model

- `ChannelConfig`: `id`, `platform`, `agent_id`, `dm_scope`, `allowed_chat_ids`, `token_env_var`, `enabled`.
- Channel config files live at `<data_dir>/channels/<channel-id>/channel.json`.
- Config reads pass `channel.json` through `core/settings/validation.py` before `ChannelConfig.from_dict()`. Schema errors raise `ChannelConfigError` with file/path diagnostics; runtime-only dependency failures such as missing credentials do not mutate the persisted config.
- Channel session metadata lives in `<data_dir>/agents/<agent-id>/sessions/<session-id>.meta.json` beside the JSONL transcript and is owned by `ChatSessionManager`. Channel-owned keys are `source_channel_id`, `platform`, `platform_conv_id`, and `last_reply_target: { channel_id, platform_target }`.
- Adapter fact dataclasses in `core/channels/adapter.py`: `ConversationFacts` for inbound platform origin, `RouteFacts` for resolved `agent_id`/`session_id`, `ReplyPlanFacts` for outbound targets, and `MessageFacts` for model-visible inbound content.
- `MessageFacts.content` may be plain text or canonical chat `ContentBlock` values; platform adapters own conversion from platform payloads into those blocks.
- `FileData` is the generic outbound file payload passed from tools/service code to adapters: `filename`, `media_type`, and raw `data` bytes.

## Interfaces

- `ChannelService(trigger_service, chat_sessions, *, agent_store, data_root, credential_resolver, attachment_store=None, command_dispatcher)` — constructor injection only, no runtime handle. `credential_resolver` is a `Callable[[str], str]`; Runtime wires `resolve_environment_credential`, which already prefers process environment over the data-dir `.env` fallback, so adapters never read `os.environ` themselves.
- `ChannelService` is the domain facade for config CRUD, lifecycle (`start`, `stop`, per-channel start/stop), active/failed health checks, `send(channel_id, message, platform_target, files=...)`, and `ensure_outbound_session(channel_id, platform_target)` (delegates to the active adapter; raises `ChannelNotFoundError` when the channel is not active).
- `ChannelAdapter` exposes `platform`, `start()`, `stop()`, `send(message, platform_target, files=None)`, and `ensure_outbound_session(platform_target) -> RouteFacts` (resolves and ensures the Session mirroring an outbound target chat, creating it with channel context when missing). Platform-specific receiving, command dispatch, routing, and file-send details belong in child specs.
- Server RPCs live in `server/rpc/channel_methods.py`: `channel.list`, `channel.create`, `channel.update`, `channel.delete`, `channel.enable`, `channel.disable`, and `channel.status`. Mutation RPCs call `runtime.reload_channel_tool()` after changing channel state.
- Retroactive channel linking lives in `session.link_channel` in `server/rpc/agent_methods.py`. It verifies the channel belongs to the Agent, writes the same session metadata keys used by adapters, and adds a channel system-reminder note to the Session.
- `register_channel_send_tool(registry, channel_service, chat_sessions)` registers the proactive outbound `channel_send` tool when runtime has at least one active channel adapter.

## Specific Specs

- `channels/telegram.md` - Telegram long-polling adapter, routing, slash commands, media handling, and Telegram send behavior.

## Conventions

- Channels are accessors. Do not put provider behavior, model fallback, or alternate chat-loop semantics in this domain.
- Final assistant replies for inbound platform turns are automatic. Agents should not call `channel_send` for normal replies to a message that came from a channel.
- `channel_send` is proactive outbound only. It may send text only, files only, or both; at least one payload is required.
- `channel_send` target resolution order is explicit `platform_target`, then matching `last_reply_target` session metadata, then a single configured `allowed_chat_ids` value. Metadata for a different `channel_id` is ignored.
- `channel_send` is registered only while at least one channel adapter is active; runtime re-evaluates registration when adapters start, stop, enable, disable, or fail.
- Public create/update/enable paths reject unknown Agent IDs before persisting or enabling a channel. Runtime startup degrades per channel: an enabled channel with invalid runtime dependencies is marked failed with a diagnostic reason and does not prevent the server from starting.
- Missing or empty `allowed_chat_ids` means deny-all for inbound allowlisted platforms. It is not "allow everyone".
- Session history remains the single source of truth. Channels add metadata and system-reminder notes; they do not fork chat history or maintain a parallel transcript.
- Proactive outbound sends (`channel_send`) record the sent content as a system-reminder note in the target chat's Session via `ensure_outbound_session`, so inbound replies in that chat keep context. This is the only place outbound (non-reply) content enters a Session; it reuses the same get-or-create + channel-reminder path as inbound routing rather than forking history.

## Constraints & Gotchas

- Only final assistant text from a completed Run is forwarded to the platform by current adapters. Tool results, reasoning, and intermediate Run events stay in the JSONL/SSE flow.
- Adapter failures use bounded restart with exponential backoff (`1s`, `2s`, `4s`, max 3 retries). Exhausted retries mark the channel failed in runtime-local health state; `channel.json` remains the source of truth for configuration.
- Create/update paths preflight adapter construction when possible and roll back persisted config if starting the enabled adapter fails.
- `channel_send` reports success only after `ChannelService.send()` and the adapter send call complete. It is not fire-and-forget.
- Prompt rendering lists only active, running channels for the current Agent. A channel with exactly one `allowed_chat_ids` value is shown as having a default target; otherwise agents must provide an explicit target.
- WebUI session browsing and retroactive channel linking depend on the sidecar metadata contract. Keep `source_channel_id`, `platform`, `platform_conv_id`, and `last_reply_target` stable when changing channel routing.
