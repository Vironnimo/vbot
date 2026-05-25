# Channel Send Tool

Sends proactive outbound messages through configured channels.

## Interfaces

- Tool name: `channel_send`
- Registration: `register_channel_send_tool(registry, channel_service, chat_sessions)`
- Schema: required `channel_id`; optional `message`, `platform_target`, and `file_paths`.
- Display: summary fields `channel_id` and `message`.

## Conventions

- The tool is proactive outbound only; automatic final replies are handled by channel adapters subscribing to Runs.
- When `platform_target` is omitted, it reads session metadata `last_reply_target.platform_target`.
- At least one of `message` or `file_paths` is required. When both are present, `message` acts as caption/accompanying text.
- The tool is registered only while runtime has at least one active channel.

## Constraints & Gotchas

- `file_paths` are local paths; the tool reads files, sniffs MIME type, and builds channel `FileData` payloads.
- Telegram-specific batching and media-group decisions stay inside the adapter layer.
- Missing channel, missing target, config errors, and send failures return failure envelopes.
