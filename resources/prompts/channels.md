## Channels

You can send messages and files through these active channels:
{generated:channel_list}

Rules:
- Use `channel_send` for proactive outbound messages and whenever you send a file through a channel.
- Do not use `channel_send` for normal text-only replies to channel-originated turns; those replies are routed automatically.
- Put every file path in `file_paths`; never send file Markdown to a channel.
- If a channel says `default target available`, you can omit `platform_target` when calling `channel_send` for that channel.
- If a channel says `explicit target required`, provide `platform_target` when calling `channel_send` unless the current session already provides a reply target.
