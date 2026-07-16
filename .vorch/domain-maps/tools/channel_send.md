# Channel Send Tool

Sends proactive outbound messages and every file delivery through configured channels.

## Interfaces

- Tool name: `channel_send`
- Registration: `register_channel_send_tool(registry, channel_service, chat_sessions, *, max_attachment_size_bytes)` — the runtime passes the active `AttachmentStore.max_size_bytes` (the `attachment_max_size_bytes` setting).
- Schema: required `channel_id`; optional `message`, `platform_target`, `thread_id`, `file_paths`, and `buttons`.
- Display: summary fields `channel_id` and `message`.
- `buttons` is `list[list[{label, data}]]` — inline-keyboard rows. `data` is the callback payload `"<prefix>:<payload>"` (max 64 bytes; the extension registered for `<prefix>` handles taps — see `.vorch/domain-maps/extensions/capabilities.md` → Channel interaction handlers). The reserved prefix `run` is the exception: a tap on `run:<payload>` wakes the agent with the tap context (the tapped button + the message's current button state) instead of being handled by an extension (see `.vorch/domain-maps/channels.md` → Interactive messages). The handler parses `buttons` into `list[list[InteractionButton]]`, rejecting a malformed structure with `invalid_arguments`; the 64-byte cap and platform support are enforced downstream (`ChannelService.send` / the adapter). Telegram only; `buttons` cannot be combined with `file_paths` (the adapter rejects it). No message id is returned.

## Conventions

- The tool handles proactive outbound messages and every channel file delivery, including files sent while replying to a channel-originated turn. Final text-only replies remain automatic through channel adapters subscribing to Runs.
- `platform_target` resolution order: explicit argument → session metadata `last_reply_target.platform_target` (only when its `channel_id` matches the requested channel) → the channel config's sole `allowed_chat_ids` entry → otherwise `invalid_arguments`.
- At least one of `message` or `file_paths` is required. When both are present, `message` acts as caption/accompanying text.
- The tool is registered only while the runtime has at least one active channel, and is re-synced (registered/unregistered) when channel configs change — so it can appear or disappear mid-session.
- Success returns `{ channel_id, platform_target }` with the resolved target.
- After a successful send, the tool records the outbound content as a system-reminder note in the *target* chat's Session (resolved via `ChannelService.ensure_outbound_session`, created with channel context if missing), so a later inbound reply in that chat has context for what was sent. The note names the sending agent (`by agent "<agent_id>"`, the calling `context.agent_id`) and includes the message text and/or attached file names. This recording is best-effort: a resolution/persistence failure is logged (`warn`) and never downgrades the already-completed send to a tool failure.

## Constraints & Gotchas

- The target channel must belong to the calling Agent; a channel owned by another Agent returns `invalid_arguments` (`ChannelConfigError`).
- `file_paths` are local paths (relative paths resolve from `ToolContext.effective_cwd`, the working directory — like the other file-taking tools); the tool reads files, sniffs MIME type, and builds channel `FileData` payloads.
- Each `file_paths` entry is size-checked against `max_attachment_size_bytes` via its on-disk size *before* the bytes are read, so an oversize file is rejected (`invalid_arguments`) without being loaded into memory. This is the outbound counterpart to the same limit enforced inbound by `AttachmentStore` and the upload endpoints.
- Telegram-specific batching and media-group decisions stay inside the adapter layer.
- Missing channel, missing target, config errors, and send failures return failure envelopes.
